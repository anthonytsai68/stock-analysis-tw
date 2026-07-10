# -*- coding: utf-8 -*-
"""User-facing auth & profile endpoints for StockGPT SaaS."""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from src.services.user_service import (
    USER_COOKIE_NAME,
    USER_SESSION_MAX_AGE,
    MIN_PASSWORD_LEN,
    register_user,
    login_user,
    get_user_by_id,
    verify_user_session,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Request models ─────────────────────────────────────────────────

class UserRegisterRequest(BaseModel):
    email: str = Field(..., description="User email")
    password: str = Field(..., min_length=MIN_PASSWORD_LEN, description="Password")
    password_confirm: str = Field(..., alias="passwordConfirm")


class UserLoginRequest(BaseModel):
    email: str = Field(..., description="User email")
    password: str = Field(..., description="Password")


# ── Cookie helpers ─────────────────────────────────────────────────

def _user_cookie_params(request: Request) -> dict:
    secure = request.url.scheme == "https"
    if os.getenv("TRUST_X_FORWARDED_FOR", "false").lower() == "true":
        proto = request.headers.get("X-Forwarded-Proto", "").lower()
        secure = proto == "https"
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": secure,
        "path": "/",
        "max_age": USER_SESSION_MAX_AGE,
    }


def _set_user_cookie(response: Response, token: str, request: Request) -> None:
    params = _user_cookie_params(request)
    response.set_cookie(
        key=USER_COOKIE_NAME,
        value=token,
        httponly=params["httponly"],
        samesite=params["samesite"],
        secure=params["secure"],
        path=params["path"],
        max_age=params["max_age"],
    )


def get_current_user(request: Request) -> Optional[dict]:
    """Extract user from session cookie. Returns None if not logged in."""
    token = request.cookies.get(USER_COOKIE_NAME)
    if not token:
        return None
    return verify_user_session(token)


# ── Endpoints ──────────────────────────────────────────────────────

@router.post("/register", summary="Register new user")
async def user_register(request: Request, body: UserRegisterRequest):
    """Register a new user account (Free plan)."""
    if body.password != body.password_confirm:
        return JSONResponse(
            status_code=400,
            content={"error": "password_mismatch", "message": "兩次輸入的密碼不一致"},
        )
    success, msg = register_user(body.email, body.password)
    if not success:
        return JSONResponse(status_code=400, content={"error": "register_failed", "message": msg})
    return {"ok": True, "message": msg}


@router.post("/login", summary="User login")
async def user_login(request: Request, body: UserLoginRequest):
    """Login and set user session cookie."""
    token, msg = login_user(body.email, body.password)
    if token is None:
        return JSONResponse(status_code=401, content={"error": "login_failed", "message": msg})
    resp = JSONResponse(content={"ok": True, "message": msg})
    _set_user_cookie(resp, token, request)
    return resp


@router.get("/profile", summary="Get user profile")
async def user_profile(request: Request):
    """Get current user profile from session."""
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized", "message": "請先登入"})
    return {
        "email": user["email"],
        "plan": user["plan"],
        "stocksLimit": user["stocks_limit"],
        "markets": user["markets"],
        "active": bool(user["active"]),
        "createdAt": user["created_at"],
        "lastLogin": user["last_login"],
    }


@router.get("/status", summary="Check user login status")
async def user_status(request: Request):
    """Check if user is logged in."""
    user = get_current_user(request)
    return {
        "loggedIn": user is not None,
        "user": {
            "email": user["email"],
            "plan": user["plan"],
            "stocksLimit": user["stocks_limit"],
        } if user else None,
    }


@router.post("/logout", summary="User logout")
async def user_logout():
    """Clear user session cookie."""
    resp = Response(status_code=204)
    resp.delete_cookie(key=USER_COOKIE_NAME, path="/")
    return resp
