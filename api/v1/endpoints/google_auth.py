# -*- coding: utf-8 -*-
"""Google OAuth endpoints for StockGPT user authentication."""

from __future__ import annotations

import logging
import os

import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from src.services.user_service import (
    USER_COOKIE_NAME,
    USER_SESSION_MAX_AGE,
    get_user_by_email,
    register_user,
    create_user_session,
    init_user_tables,
)

logger = logging.getLogger(__name__)

router = APIRouter()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def _cookie_params(request: Request) -> dict:
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
    params = _cookie_params(request)
    response.set_cookie(
        key=USER_COOKIE_NAME,
        value=token,
        httponly=params["httponly"],
        samesite=params["samesite"],
        secure=params["secure"],
        path=params["path"],
        max_age=params["max_age"],
    )


@router.get("/google-login", summary="Google OAuth login redirect")
async def google_login():
    """Redirect user to Google OAuth consent screen."""
    if not GOOGLE_CLIENT_ID:
        return JSONResponse(
            status_code=400,
            content={"error": "not_configured", "message": "Google OAuth 尚未設定"},
        )
    redirect_uri = GOOGLE_REDIRECT_URI or ""
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
    }
    from urllib.parse import urlencode
    auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    return RedirectResponse(url=auth_url)


@router.get("/google-callback", summary="Google OAuth callback")
async def google_callback(request: Request, code: str = "", error: str = ""):
    """Handle Google OAuth callback. Creates or logs in user."""
    if error:
        return RedirectResponse(url="/user/login?error=google_denied")

    if not code or not GOOGLE_CLIENT_ID:
        return RedirectResponse(url="/user/login?error=oauth_failed")

    # Exchange code for tokens
    try:
        token_resp = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": GOOGLE_REDIRECT_URI,
            },
            timeout=10,
        )
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            logger.error("Google token exchange failed: %s", token_data)
            return RedirectResponse(url="/user/login?error=token_failed")
    except Exception as e:
        logger.error("Google token request error: %s", e)
        return RedirectResponse(url="/user/login?error=network_error")

    # Get user info
    try:
        user_resp = requests.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        user_info = user_resp.json()
        email = (user_info.get("email") or "").strip().lower()
        name = user_info.get("name", "")
        if not email:
            return RedirectResponse(url="/user/login?error=no_email")
    except Exception as e:
        logger.error("Google userinfo error: %s", e)
        return RedirectResponse(url="/user/login?error=userinfo_failed")

    # Find or create user
    init_user_tables()
    user = get_user_by_email(email)
    if not user:
        # Auto-register with random password (Google-auth only)
        import secrets
        random_pass = secrets.token_urlsafe(16)
        success, msg = register_user(email, random_pass)
        if not success:
            return RedirectResponse(url=f"/user/login?error={msg}")
        user = get_user_by_email(email)

    if not user:
        return RedirectResponse(url="/user/login?error=user_not_found")

    # Create session and redirect to home
    session = create_user_session(user["id"], user["email"])
    resp = RedirectResponse(url="/")
    _set_user_cookie(resp, session, request)
    return resp
