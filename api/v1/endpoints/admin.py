# -*- coding: utf-8 -*-
"""Admin endpoints for user & subscription management."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.services.user_service import (
    get_all_users,
    get_user_by_id,
    activate_user,
    deactivate_user,
    update_user_plan,
    PLANS,
)
from api.v1.endpoints.user_auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_admin(request: Request) -> bool:
    """Check if current user is admin. For now, admin = session cookie from original auth."""
    # Admin is authenticated via the original dsa_session cookie
    from src.auth import COOKIE_NAME, verify_session
    cookie = request.cookies.get(COOKIE_NAME)
    return cookie is not None and verify_session(cookie)


# ── Request models ─────────────────────────────────────────────────

class UpdatePlanRequest(BaseModel):
    plan: str = Field(..., description="Plan name: free, pro, business")
    notes: str = Field(default="", description="Admin notes")

    model_config = {"populate_by_name": True}


# ── Endpoints ──────────────────────────────────────────────────────

@router.get("/users", summary="List all users")
async def admin_list_users(request: Request):
    """Admin: get all registered users."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized", "message": "Admin login required"})

    users = get_all_users()
    return {
        "users": [
            {
                "id": u["id"],
                "email": u["email"],
                "plan": u["plan"],
                "stocksLimit": u["stocks_limit"],
                "markets": u["markets"],
                "active": bool(u["active"]),
                "createdAt": u["created_at"],
                "lastLogin": u["last_login"],
                "notes": u["notes"],
            }
            for u in users
        ],
        "plans": {k: v for k, v in PLANS.items()},
    }


@router.get("/users/{user_id}", summary="Get user detail")
async def admin_get_user(request: Request, user_id: int):
    """Admin: get single user."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized", "message": "Admin login required"})

    user = get_user_by_id(user_id)
    if not user:
        return JSONResponse(status_code=404, content={"error": "not_found", "message": "User not found"})

    return {
        "id": user["id"],
        "email": user["email"],
        "plan": user["plan"],
        "stocksLimit": user["stocks_limit"],
        "markets": user["markets"],
        "active": bool(user["active"]),
        "createdAt": user["created_at"],
        "lastLogin": user["last_login"],
        "notes": user["notes"],
    }


@router.post("/users/{user_id}/activate", summary="Activate user")
async def admin_activate_user(request: Request, user_id: int):
    """Admin: activate user account."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized", "message": "Admin login required"})
    activate_user(user_id)
    return {"ok": True}


@router.post("/users/{user_id}/deactivate", summary="Deactivate user")
async def admin_deactivate_user(request: Request, user_id: int):
    """Admin: deactivate user account."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized", "message": "Admin login required"})
    deactivate_user(user_id)
    return {"ok": True}


@router.post("/users/{user_id}/plan", summary="Change user plan")
async def admin_update_plan(request: Request, user_id: int, body: UpdatePlanRequest):
    """Admin: change user subscription plan."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized", "message": "Admin login required"})

    if body.plan not in PLANS:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_plan", "message": f"無效的方案。可選：{', '.join(PLANS.keys())}"},
        )

    ok = update_user_plan(user_id, body.plan, body.notes)
    if not ok:
        return JSONResponse(status_code=500, content={"error": "update_failed", "message": "Failed to update plan"})
    return {"ok": True}


@router.get("/plans", summary="List available plans")
async def admin_list_plans():
    """Get all subscription plans."""
    return {"plans": {k: v for k, v in PLANS.items()}}
