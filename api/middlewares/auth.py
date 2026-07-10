# -*- coding: utf-8 -*-
"""
Auth middleware: protect /api/v1/* when admin auth is enabled.
"""

from __future__ import annotations

import logging
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.auth import COOKIE_NAME, is_auth_enabled, verify_session
from src.services.user_service import USER_COOKIE_NAME as USER_COOKIE, verify_user_session

logger = logging.getLogger(__name__)

EXEMPT_PATHS = frozenset({
    "/api/v1/auth/login",
    "/api/v1/auth/status",
    "/api/v1/user/register",
    "/api/v1/user/login",
    "/api/v1/user/status",
    "/api/v1/user/logout",
    "/api/v1/user/google-login",
    "/api/v1/user/google-callback",
    "/api/health",
    "/api/v1/health",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
})

# User endpoints: require user OR admin session
USER_PATHS_PREFIXES = ("/api/v1/user/",)


def _path_exempt(path: str) -> bool:
    """Check if path is exempt from auth."""
    normalized = path.rstrip("/") or "/"
    return normalized in EXEMPT_PATHS


def _is_user_path(path: str) -> bool:
    """Check if path is a user endpoint."""
    return any(path.startswith(prefix) for prefix in USER_PATHS_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    """Require valid session for /api/v1/* when auth is enabled.

    Admin endpoints require admin session.
    User endpoints accept user session OR admin session.
    General API routes accept admin OR user session.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ):
        if not is_auth_enabled():
            return await call_next(request)

        path = request.url.path
        if _path_exempt(path):
            return await call_next(request)

        if not path.startswith("/api/v1/"):
            return await call_next(request)

        # Check admin session first (admin can access everything)
        admin_cookie = request.cookies.get(COOKIE_NAME)
        if admin_cookie and verify_session(admin_cookie):
            return await call_next(request)

        # Admin-only paths: require admin session
        if path.startswith("/api/v1/admin/") or path.startswith("/api/v1/system/"):
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "message": "Admin login required"},
            )

        # All other API paths: accept user session too
        user_cookie = request.cookies.get(USER_COOKIE)
        if user_cookie and verify_user_session(user_cookie):
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized", "message": "Login required"},
        )


def add_auth_middleware(app):
    """Add auth middleware to protect API routes.

    The middleware is always registered; whether auth is enforced is determined
    at request time by is_auth_enabled() so the decision stays consistent across
    any runtime configuration reload.
    """
    app.add_middleware(AuthMiddleware)
