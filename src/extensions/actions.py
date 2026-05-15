# -*- coding: utf-8 -*-
"""Core Action contract for the DSA Extension Runtime."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional


def _coerce_bool(value: Any, *, field_name: str, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "":
            return default
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    raise ValueError(f"{field_name} must be a boolean")


def _coerce_float(value: Any, *, field_name: str, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc


def _coerce_int(value: Any, *, field_name: str, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


class ActionMode(str, Enum):
    SYNC = "sync"
    ASYNC = "async"


@dataclass
class ActionBudget:
    timeout_seconds: float = 0
    max_llm_calls: int = 0
    max_items: int = 10

    @classmethod
    def from_mapping(cls, value: Optional[Dict[str, Any]]) -> "ActionBudget":
        data = value if isinstance(value, dict) else {}
        return cls(
            timeout_seconds=_coerce_float(
                data.get("timeout_seconds"),
                field_name="budget.timeout_seconds",
                default=0,
            ),
            max_llm_calls=_coerce_int(
                data.get("max_llm_calls"),
                field_name="budget.max_llm_calls",
                default=0,
            ),
            max_items=_coerce_int(
                data.get("max_items"),
                field_name="budget.max_items",
                default=10,
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_llm_calls": self.max_llm_calls,
            "max_items": self.max_items,
        }


@dataclass
class ActionContext:
    caller: str = "web"
    trace_id: str = field(default_factory=lambda: f"trace_{uuid.uuid4().hex}")
    session_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    dry_run: bool = False
    budget: ActionBudget = field(default_factory=ActionBudget)
    context: Dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False
    call_depth: int = 0

    @classmethod
    def from_mapping(cls, value: Optional[Dict[str, Any]]) -> "ActionContext":
        if isinstance(value, cls):
            return value
        data = value if isinstance(value, dict) else {}
        return cls(
            caller=str(data.get("caller") or "web"),
            trace_id=str(data.get("trace_id") or f"trace_{uuid.uuid4().hex}"),
            session_id=data.get("session_id"),
            idempotency_key=data.get("idempotency_key"),
            dry_run=_coerce_bool(data.get("dry_run"), field_name="dry_run"),
            budget=ActionBudget.from_mapping(data.get("budget")),
            context=dict(data.get("context") or {}),
            requires_confirmation=_coerce_bool(
                data.get("requires_confirmation"),
                field_name="requires_confirmation",
            ),
            call_depth=_coerce_int(data.get("call_depth"), field_name="call_depth", default=0),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "caller": self.caller,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "idempotency_key": self.idempotency_key,
            "dry_run": self.dry_run,
            "budget": self.budget.to_dict(),
            "context": dict(self.context),
            "requires_confirmation": self.requires_confirmation,
            "call_depth": self.call_depth,
        }


@dataclass
class ActionError:
    code: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@dataclass
class ActionResult:
    action_id: str
    run_id: str
    ok: bool
    status: str
    data: Optional[Dict[str, Any]] = None
    error: Optional[ActionError] = None
    warnings: List[str] = field(default_factory=list)
    degradation: Optional[Dict[str, Any]] = None
    task_id: Optional[str] = None
    input_hash: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id,
            "run_id": self.run_id,
            "ok": self.ok,
            "status": self.status,
            "data": self.data,
            "error": self.error.to_dict() if self.error else None,
            "warnings": list(self.warnings),
            "degradation": self.degradation,
            "task_id": self.task_id,
            "input_hash": self.input_hash,
            "metadata": dict(self.metadata),
        }


ActionHandler = Callable[[Mapping[str, Any], ActionContext], Any]


@dataclass
class ActionDefinition:
    action_id: str
    plugin_id: str
    description: str
    handler: ActionHandler
    input_schema: Dict[str, Any] = field(default_factory=dict)
    mode: ActionMode = ActionMode.SYNC
    permissions: List[str] = field(default_factory=list)
    timeout_seconds: float = 60
    requires_confirmation: bool = False
    category: str = "general"
    subject_key: Optional[str] = None

def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


def stable_input_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
