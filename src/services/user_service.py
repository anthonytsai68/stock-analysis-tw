# -*- coding: utf-8 -*-
"""
Multi-user subscription service for StockGPT SaaS.

Manages users, subscriptions, and plan-based stock limits.
Uses SQLite — same database as the main analysis system.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Plan definitions ──────────────────────────────────────────────
PLANS = {
    "free": {
        "name": "Free",
        "max_stocks": 5,
        "markets": ["tw"],
        "price_ntd": 0,
    },
    "pro": {
        "name": "Pro",
        "max_stocks": 30,
        "markets": ["tw", "us"],
        "price_ntd": 99,
    },
    "business": {
        "name": "Business",
        "max_stocks": 0,  # 0 = unlimited
        "markets": ["tw", "us", "hk", "cn", "jp", "kr", "crypto"],
        "price_ntd": 299,
    },
}

# ── Auth constants ─────────────────────────────────────────────────
USER_COOKIE_NAME = "dsa_user_session"
USER_SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days
PBKDF2_ITERATIONS = 100_000
MIN_PASSWORD_LEN = 6

# ── Lazy state ─────────────────────────────────────────────────────
_user_session_secret: Optional[bytes] = None
_db_path: Optional[str] = None


def _get_db_path() -> str:
    global _db_path
    if _db_path is not None:
        return _db_path
    _db_path = os.getenv("DATABASE_PATH", "./data/stock_analysis.db")
    return _db_path


def _get_data_dir() -> Path:
    return Path(_get_db_path()).resolve().parent


def _get_conn() -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode and foreign keys."""
    db_path = _get_db_path()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _get_user_session_secret() -> bytes:
    global _user_session_secret
    if _user_session_secret is not None:
        return _user_session_secret
    data_dir = _get_data_dir()
    secret_path = data_dir / ".user_session_secret"
    if secret_path.exists():
        _user_session_secret = secret_path.read_bytes()
        if len(_user_session_secret) == 32:
            return _user_session_secret
    _user_session_secret = secrets.token_bytes(32)
    data_dir.mkdir(parents=True, exist_ok=True)
    secret_path.write_bytes(_user_session_secret)
    secret_path.chmod(0o600)
    return _user_session_secret


# ── Database init ──────────────────────────────────────────────────

def init_user_tables() -> None:
    """Create users and subscriptions tables if they don't exist."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            stocks_limit INTEGER DEFAULT 5,
            markets TEXT DEFAULT 'tw',
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_login TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            plan TEXT NOT NULL,
            paypal_txn_id TEXT,
            amount_ntd INTEGER,
            start_date TEXT NOT NULL,
            end_date TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            notes TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
        CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id);
    """)
    conn.commit()
    conn.close()
    logger.info("User tables initialized")


# ── Password hashing ───────────────────────────────────────────────

def _hash_password(password: str) -> str:
    """Hash password with PBKDF2-SHA256. Returns salt_b64:hash_b64."""
    import base64
    salt = secrets.token_bytes(32)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt=salt, iterations=PBKDF2_ITERATIONS
    )
    salt_b64 = base64.standard_b64encode(salt).decode("ascii")
    hash_b64 = base64.standard_b64encode(derived).decode("ascii")
    return f"{salt_b64}:{hash_b64}"


def _verify_user_password(password: str, stored: str) -> bool:
    """Verify password against stored PBKDF2 hash."""
    import base64
    try:
        salt_b64, hash_b64 = stored.split(":", 1)
        salt = base64.standard_b64decode(salt_b64)
        stored_hash = base64.standard_b64decode(hash_b64)
        computed = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt=salt, iterations=PBKDF2_ITERATIONS
        )
        return hmac.compare_digest(computed, stored_hash)
    except (ValueError, TypeError):
        return False


# ── Session management ─────────────────────────────────────────────

def create_user_session(user_id: int, email: str) -> str:
    """Create signed session token for user."""
    secret = _get_user_session_secret()
    nonce = secrets.token_urlsafe(32)
    ts = str(int(time.time()))
    # Use | as delimiter — safe because email/base64url never contain |
    payload = f"{user_id}|{email}|{nonce}|{ts}"
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def verify_user_session(token: str) -> Optional[dict]:
    """Verify user session token. Returns user dict or None."""
    secret = _get_user_session_secret()
    if not secret or not token:
        return None
    parts = token.split("|")
    if len(parts) != 5:
        return None
    user_id_str, email, nonce, ts_str, sig = parts
    payload = f"{user_id_str}|{email}|{nonce}|{ts_str}"
    expected = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        ts = int(ts_str)
        user_id = int(user_id_str)
    except ValueError:
        return None
    if time.time() - ts > USER_SESSION_MAX_AGE:
        return None
    # Verify user still exists and is active
    user = get_user_by_id(user_id)
    if not user or not user.get("active"):
        return None
    return user


# ── User CRUD ──────────────────────────────────────────────────────

def register_user(email: str, password: str) -> tuple[bool, str]:
    """Register a new user. Returns (success, message)."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False, "請輸入有效的 Email"
    if not password or len(password) < MIN_PASSWORD_LEN:
        return False, f"密碼至少需要 {MIN_PASSWORD_LEN} 個字元"

    init_user_tables()
    conn = _get_conn()
    try:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            return False, "此 Email 已註冊"

        plan = "free"
        plan_def = PLANS[plan]
        conn.execute(
            "INSERT INTO users (email, password_hash, plan, stocks_limit, markets, active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (email, _hash_password(password), plan, plan_def["max_stocks"], ",".join(plan_def["markets"])),
        )
        conn.commit()
        user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        logger.info("New user registered: %s (id=%d)", email, user_id)
        return True, f"註冊成功！歡迎加入 StockGPT"
    except sqlite3.IntegrityError:
        return False, "此 Email 已註冊"
    finally:
        conn.close()


def login_user(email: str, password: str) -> tuple[Optional[str], str]:
    """Login user. Returns (session_token_or_None, message)."""
    email = (email or "").strip().lower()
    if not email or not password:
        return None, "請輸入 Email 和密碼"

    init_user_tables()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, email, password_hash, plan, active FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        if not row:
            return None, "Email 或密碼錯誤"
        if not row["active"]:
            return None, "帳號尚未開通或已被停用"
        if not _verify_user_password(password, row["password_hash"]):
            return None, "Email 或密碼錯誤"

        # Update last login
        conn.execute(
            "UPDATE users SET last_login = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        conn.commit()

        session = create_user_session(row["id"], row["email"])
        return session, "登入成功"
    finally:
        conn.close()


def get_user_by_id(user_id: int) -> Optional[dict]:
    """Get user by ID."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, email, plan, stocks_limit, markets, active, created_at, last_login, notes "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()


def get_user_by_email(email: str) -> Optional[dict]:
    """Get user by email."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, email, plan, stocks_limit, markets, active, created_at, last_login, notes "
            "FROM users WHERE email = ?",
            (email.strip().lower(),),
        ).fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()


def get_all_users() -> list[dict]:
    """Get all users (admin)."""
    init_user_tables()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, email, plan, stocks_limit, markets, active, created_at, last_login, notes "
            "FROM users ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_user_plan(user_id: int, plan: str, admin_notes: str = "") -> bool:
    """Change user's plan. Validates against PLANS."""
    if plan not in PLANS:
        return False
    plan_def = PLANS[plan]
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE users SET plan = ?, stocks_limit = ?, markets = ?, notes = ? WHERE id = ?",
            (plan, plan_def["max_stocks"], ",".join(plan_def["markets"]), admin_notes, user_id),
        )
        # Record subscription
        conn.execute(
            "INSERT INTO subscriptions (user_id, plan, start_date, status, notes) "
            "VALUES (?, ?, datetime('now'), 'active', ?)",
            (user_id, plan, admin_notes),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def activate_user(user_id: int) -> bool:
    """Activate user account."""
    conn = _get_conn()
    try:
        conn.execute("UPDATE users SET active = 1 WHERE id = ?", (user_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def deactivate_user(user_id: int) -> bool:
    """Deactivate user account."""
    conn = _get_conn()
    try:
        conn.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def get_user_stocks_limit(user_id: int) -> int:
    """Get max stocks a user can add (0 = unlimited)."""
    user = get_user_by_id(user_id)
    if not user:
        return 0
    return user.get("stocks_limit", 0)


def get_user_markets(user_id: int) -> list[str]:
    """Get allowed markets for user."""
    user = get_user_by_id(user_id)
    if not user:
        return []
    markets = user.get("markets", "tw")
    return [m.strip() for m in markets.split(",") if m.strip()]


def can_add_stock(user_id: int, stock_code: str, current_count: int) -> tuple[bool, str]:
    """Check if user can add a stock. Returns (allowed, reason)."""
    user = get_user_by_id(user_id)
    if not user:
        return False, "用戶不存在"
    if not user.get("active"):
        return False, "帳號未開通"

    # Check market permission
    allowed_markets = get_user_markets(user_id)
    market = _detect_market(stock_code)
    if market not in allowed_markets:
        market_names = {"tw": "台股", "us": "美股", "hk": "港股", "cn": "A股", "jp": "日股", "kr": "韓股", "crypto": "加密貨幣"}
        name = market_names.get(market, market)
        return False, f"您的方案不支援{name}市場"

    # Check stock limit (0 = unlimited)
    limit = user.get("stocks_limit", 0)
    if limit > 0 and current_count >= limit:
        return False, f"已達上限（{limit} 檔），請升級方案"

    return True, ""


def _detect_market(code: str) -> str:
    """Detect market from stock code suffix or pattern."""
    code = code.upper().strip()
    if code.endswith(".TW"):
        return "tw"
    if code.endswith(".TWO"):
        return "tw"
    if code.endswith(".HK"):
        return "hk"
    if code.endswith(".T"):
        return "jp"
    if code.endswith(".KS") or code.endswith(".KQ"):
        return "kr"
    if code.endswith("-USD") or code.endswith("USD"):
        return "crypto"
    if code.endswith(".SS") or code.endswith(".SZ"):
        return "cn"
    # US stocks: no suffix or common patterns
    if "." not in code and not code.isdigit():
        return "us"
    if re.match(r'^\d{5,6}$', code.replace('.TWO', '').replace('.TW', '')):
        return "tw"
    return "us"  # default to US


import re
