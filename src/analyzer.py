# -*- coding: utf-8 -*-
"""
===================================
A股自選股智能分析系統 - AI分析層
===================================

職責：
1. 封裝 LLM 調用邏輯（通過 LiteLLM 統一調用 Gemini/Anthropic/OpenAI 等）
2. 結合技術面和消息面生成分析報告
3. 解析 LLM 響應為結構化 AnalysisResult
"""

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple, Callable

import litellm
from json_repair import repair_json
from litellm import Router

from src.agent.llm_adapter import (
    get_thinking_extra_body,
    resolve_fallback_litellm_wire_models,
    register_fallback_model_pricing,
)
from src.agent.provider_trace import resolved_model_provider_identity
from src.agent.skills.defaults import CORE_TRADING_SKILL_POLICY_ZH
from src.config import (
    Config,
    extra_litellm_params,
    get_api_keys_for_model,
    get_config,
    get_configured_llm_models,
    resolve_news_window_days,
)
from src.llm.hermes import (
    HERMES_CHANNEL_NAME,
    build_hermes_redaction_values,
    canonicalize_hermes_model_ref,
    filter_non_hermes_deployments,
    hermes_blocked_route_candidates,
    is_masked_secret_placeholder,
    open_hermes_no_proxy_client,
    route_deployment_origins,
    route_has_hermes,
    sanitize_hermes_error_text,
)
from src.llm.generation_params import apply_litellm_generation_params
from src.llm.errors import call_litellm_with_param_recovery
from src.llm.backend_registry import (
    LOCAL_CLI_GENERATION_BACKEND_IDS,
    LITELLM_BACKEND_ID,
    resolve_generation_backend_id,
    resolve_generation_fallback_backend_id,
)
from src.llm.backend_factory import create_generation_backend
from src.llm.generation_backend import (
    GenerationBackend,
    GenerationError,
    GenerationErrorCode,
)
from src.llm.usage import (
    attach_legacy_message_stability_audit,
    attach_message_hmacs,
    extract_usage_payload,
    normalize_litellm_usage,
    should_persist_usage_telemetry,
)
from src.llm.local_cli_backend import redact_diagnostic_text
from src.llm.provider_cache import (
    apply_prompt_cache_hints,
    build_provider_cache_route_context,
    filter_prompt_cache_telemetry,
)
from src.storage import persist_llm_usage
from src.data.stock_mapping import STOCK_NAME_MAP
from src.report_language import (
    get_signal_level,
    get_no_data_text,
    get_placeholder_text,
    get_unknown_text,
    get_chip_unavailable_text,
    infer_decision_type_from_advice,
    is_chip_placeholder_value,
    localize_chip_health,
    localize_confidence_level,
    localize_operation_advice,
    localize_trend_prediction,
    normalize_report_language,
)
from src.schemas.decision_action import build_action_fields
from src.schemas.decision_scale import (
    CANONICAL_DECISION_SCALE_PROMPT_ZH,
    score_band_metadata,
)
from src.schemas.report_schema import AnalysisReportSchema
from src.market_context import detect_market, get_market_role, get_market_guidelines
from src.services.daily_market_context import format_daily_market_context_prompt_section
from src.market_phase_prompt import format_market_phase_prompt_section

logger = logging.getLogger(__name__)


def _localized_text(language: Any, *, en: str, zh: str, ko: str) -> str:
    """Pick a deterministic fallback string for the report language (zh/en/ko)."""
    normalized = normalize_report_language(language)
    if normalized == "en":
        return en
    if normalized == "ko":
        return ko
    return zh


def _normalize_risk_warning_values(value: Any) -> List[str]:
    """Normalize arbitrary risk_warning values into a flat list of text alerts."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        normalized: List[str] = []
        for item in value:
            normalized.extend(_normalize_risk_warning_values(item))
        return normalized
    if isinstance(value, dict):
        if not value:
            return []
        try:
            dumped = json.dumps(value, ensure_ascii=False)
            text = dumped.strip()
        except (TypeError, ValueError):
            text = str(value).strip()
        return [text] if text else []
    text = str(value).strip()
    return [text] if text else []


def _today_has_realtime_overlay(today: Any) -> bool:
    if not isinstance(today, dict):
        return False
    data_source = today.get("data_source") or today.get("dataSource")
    if isinstance(data_source, str) and data_source.startswith("realtime:"):
        return True
    if today.get("is_partial_bar") is True or today.get("isPartialBar") is True:
        return True
    if today.get("is_estimated") is True or today.get("isEstimated") is True:
        return True
    return bool(today.get("estimated_fields") or today.get("estimatedFields"))


def _today_looks_complete_daily_bar(
    context: Dict[str, Any],
    phase_context: Dict[str, Any],
) -> bool:
    today = context.get("today")
    if (
        not isinstance(today, dict)
        or today.get("close") in (None, "")
        or _today_has_realtime_overlay(today)
    ):
        return False

    effective_date = phase_context.get("effective_daily_bar_date")
    today_date = today.get("date") or today.get("trade_date") or context.get("date")
    if effective_date and today_date and str(today_date) != str(effective_date):
        return False
    return True


def _phase_aware_quote_labels(context: Dict[str, Any]) -> Tuple[str, str]:
    """Choose Chinese quote-table labels that do not conflict with phase context."""
    phase_context = context.get("market_phase_context")
    if not isinstance(phase_context, dict):
        return "今日行情", "收盤價"

    phase = str(phase_context.get("phase") or "").strip()
    if phase in {"premarket", "non_trading"}:
        today = context.get("today")
        if _today_looks_complete_daily_bar(context, phase_context):
            return "上一完整交易日行情", "上一完整交易日收盤價"
        if _today_has_realtime_overlay(today):
            return "最新行情", "實時估算價"
        if isinstance(today, dict) and today.get("close") not in (None, ""):
            return "最新行情", "最新價"
        return "今日行情", "收盤價"

    if (
        phase in {"intraday", "lunch_break", "closing_auction"}
        and phase_context.get("is_partial_bar") is True
    ):
        return "最新行情", "盤中估算價"

    return "今日行情", "收盤價"


def _should_hide_regular_session_ohlc(context: Dict[str, Any]) -> bool:
    phase_context = context.get("market_phase_context")
    if not isinstance(phase_context, dict):
        return False

    phase = str(phase_context.get("phase") or "").strip()
    return phase in {"premarket", "non_trading"} and not _today_looks_complete_daily_bar(
        context,
        phase_context,
    )


def _legacy_market_group(stock_code: Any) -> str:
    code = str(stock_code or "").strip()
    if not code or code.lower() == "unknown":
        return "unknown"
    market = detect_market(code)
    return market if market in {"cn", "hk", "us"} else "unknown"


def _legacy_audit_marker_specs(
    context: Dict[str, Any],
    *,
    code: str,
    stock_name: str,
    report_language: str,
    news_context: Optional[str],
    analysis_context_pack_summary: Optional[str],
) -> List[Dict[str, Any]]:
    markers: List[Dict[str, Any]] = []

    def add(marker_name: str, value: Any) -> None:
        if value is None:
            return
        text = str(value).strip()
        if not text:
            return
        markers.append(
            {
                "marker_name": marker_name,
                "message_role": "user",
                "text": text,
            }
        )

    add("stock_code", code)
    add("stock_name", stock_name)
    add("analysis_date", context.get("date"))
    add("market_phase", "## Market Phase Context" if report_language in ("en", "ko") else "## 市場階段上下文")
    add("daily_market_context", "## Daily Market Context" if report_language in ("en", "ko") else "## 大盤環境摘要")
    add("analysis_context_pack", analysis_context_pack_summary)
    add("quote", "## 📈 技術面數據")
    add("news_context", "## 📰 輿情情報" if news_context else None)
    return markers


class _LiteLLMStreamError(RuntimeError):
    """Internal error wrapper that records whether any text was streamed."""

    def __init__(self, message: str, *, partial_received: bool = False):
        super().__init__(message)
        self.partial_received = partial_received


class _AllModelsFailedError(Exception):
    """Raised when every model in the fallback chain fails.

    This includes both LLM call errors and JSON parse errors (when a
    ``response_validator`` is provided to :meth:`GeminiAnalyzer._call_litellm`).

    The ``last_response_text`` attribute holds the raw text from the last model
    that *did* return a response (but whose JSON could not be validated), so
    callers can still attempt a best-effort text fallback.

    ``last_model`` and ``last_usage`` record the model name and token usage
    from the last attempt so callers can persist usage even on fallback.
    """

    def __init__(
        self,
        message: str,
        *,
        last_response_text: Optional[str] = None,
        last_model: Optional[str] = None,
        last_usage: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.last_response_text = last_response_text
        self.last_model = last_model
        self.last_usage = last_usage or {}


from src.utils.data_processing import normalize_report_signal_attribution


def check_content_integrity(
    result: "AnalysisResult",
    *,
    require_phase_decision: bool = False,
) -> Tuple[bool, List[str]]:
    """
    Check mandatory fields for report content integrity.
    Returns (pass, missing_fields). Module-level for use by pipeline (agent weak mode).

    Note:
    - Required fields: missing → pass=False, added to missing_fields
    - Optional fields (e.g., signal_attribution): missing → pass=True and are not added to missing_fields
    """
    missing: List[str] = []

    def _is_blank_text(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        return True

    def _is_invalid_risk_alerts(value: Any) -> bool:
        return not isinstance(value, list)

    def _is_invalid_stop_loss(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, (list, tuple, dict)):
            return True
        if isinstance(value, str):
            return not value.strip()
        return False

    if result.sentiment_score is None:
        missing.append("sentiment_score")
    advice = result.operation_advice
    if not advice or not isinstance(advice, str) or _is_blank_text(advice):
        missing.append("operation_advice")
    summary = result.analysis_summary
    if not summary or not isinstance(summary, str) or _is_blank_text(summary):
        missing.append("analysis_summary")
    dash = result.dashboard if isinstance(result.dashboard, dict) else {}
    core = dash.get("core_conclusion")
    core = core if isinstance(core, dict) else {}
    if _is_blank_text(core.get("one_sentence")):
        missing.append("dashboard.core_conclusion.one_sentence")
    intel = dash.get("intelligence")
    intel = intel if isinstance(intel, dict) else None
    if intel is None or _is_invalid_risk_alerts(intel.get("risk_alerts")):
        missing.append("dashboard.intelligence.risk_alerts")
    if result.decision_type in ("buy", "hold"):
        battle = dash.get("battle_plan")
        battle = battle if isinstance(battle, dict) else {}
        sp = battle.get("sniper_points")
        sp = sp if isinstance(sp, dict) else {}
        stop_loss = sp.get("stop_loss")
        if _is_invalid_stop_loss(stop_loss):
            missing.append("dashboard.battle_plan.sniper_points.stop_loss")
    if require_phase_decision:
        phase_decision = dash.get("phase_decision")
        phase_decision = phase_decision if isinstance(phase_decision, dict) else {}
        if not isinstance(phase_decision.get("phase_context"), dict):
            missing.append("dashboard.phase_decision.phase_context")
        if _is_blank_text(phase_decision.get("action_window")):
            missing.append("dashboard.phase_decision.action_window")
        if _is_blank_text(phase_decision.get("immediate_action")):
            missing.append("dashboard.phase_decision.immediate_action")
        if not isinstance(phase_decision.get("watch_conditions"), list):
            missing.append("dashboard.phase_decision.watch_conditions")
        if _is_blank_text(phase_decision.get("next_check_time")):
            missing.append("dashboard.phase_decision.next_check_time")
        if _is_blank_text(phase_decision.get("confidence_reason")):
            missing.append("dashboard.phase_decision.confidence_reason")
        if not isinstance(phase_decision.get("data_limitations"), list):
            missing.append("dashboard.phase_decision.data_limitations")
    return len(missing) == 0, missing


def apply_placeholder_fill(result: "AnalysisResult", missing_fields: List[str]) -> None:
    """Fill missing mandatory fields with placeholders (in-place). Module-level for pipeline."""

    def _is_blank_text(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        return True

    def _is_invalid_risk_alerts(value: Any) -> bool:
        return not isinstance(value, list)

    def _is_invalid_stop_loss(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, (list, tuple, dict)):
            return True
        if isinstance(value, str):
            return not value.strip()
        return False

    report_language = normalize_report_language(getattr(result, "report_language", "zh"))
    placeholder = get_placeholder_text(report_language)
    phase_decision_placeholders = {
        "dashboard.phase_decision.action_window": _localized_text(
            report_language,
            en="Model did not provide a phase action window",
            zh="模型未提供階段化行動窗口",
            ko="모델이 단계별 행동 구간을 제공하지 않았습니다",
        ),
        "dashboard.phase_decision.immediate_action": _localized_text(
            report_language,
            en="Model did not provide a phase-aware immediate action",
            zh="模型未提供階段化即時動作",
            ko="모델이 단계 인식 즉시 동작을 제공하지 않았습니다",
        ),
        "dashboard.phase_decision.next_check_time": _localized_text(
            report_language,
            en="Model did not provide a next check point",
            zh="模型未提供下一次檢查點",
            ko="모델이 다음 점검 시점을 제공하지 않았습니다",
        ),
        "dashboard.phase_decision.confidence_reason": _localized_text(
            report_language,
            en="Model did not provide a phase confidence rationale",
            zh="模型未提供階段化置信度理由",
            ko="모델이 단계별 신뢰도 근거를 제공하지 않았습니다",
        ),
    }
    for field in missing_fields:
        if field == "sentiment_score":
            result.sentiment_score = 50
        elif field == "operation_advice":
            if _is_blank_text(result.operation_advice):
                result.operation_advice = placeholder
        elif field == "analysis_summary":
            if _is_blank_text(result.analysis_summary):
                result.analysis_summary = placeholder
        elif field == "dashboard.core_conclusion.one_sentence":
            if not result.dashboard:
                result.dashboard = {}
            core = result.dashboard.get("core_conclusion")
            if not isinstance(core, dict):
                core = {}
                result.dashboard["core_conclusion"] = core
            fallback_sentence = (
                result.analysis_summary
                or result.operation_advice
                or placeholder
            )
            if _is_blank_text(core.get("one_sentence")):
                result.dashboard["core_conclusion"]["one_sentence"] = fallback_sentence
        elif field == "dashboard.intelligence.risk_alerts":
            if not result.dashboard:
                result.dashboard = {}
            intelligence = result.dashboard.get("intelligence")
            if not isinstance(intelligence, dict):
                intelligence = {}
                result.dashboard["intelligence"] = intelligence
            if _is_invalid_risk_alerts(intelligence.get("risk_alerts")):
                risk_warning_values = _normalize_risk_warning_values(result.risk_warning)
                intelligence["risk_alerts"] = risk_warning_values
        elif field == "dashboard.battle_plan.sniper_points.stop_loss":
            if not result.dashboard:
                result.dashboard = {}
            battle_plan = result.dashboard.get("battle_plan")
            if not isinstance(battle_plan, dict):
                battle_plan = {}
                result.dashboard["battle_plan"] = battle_plan
            sniper_points = battle_plan.get("sniper_points")
            if not isinstance(sniper_points, dict):
                sniper_points = {}
                battle_plan["sniper_points"] = sniper_points
            if _is_invalid_stop_loss(sniper_points.get("stop_loss")):
                sniper_points["stop_loss"] = placeholder
        elif field.startswith("dashboard.phase_decision."):
            if not result.dashboard:
                result.dashboard = {}
            phase_decision = result.dashboard.get("phase_decision")
            if not isinstance(phase_decision, dict):
                phase_decision = {}
                result.dashboard["phase_decision"] = phase_decision
            if field == "dashboard.phase_decision.phase_context":
                if not isinstance(phase_decision.get("phase_context"), dict):
                    phase_decision["phase_context"] = {}
            elif field == "dashboard.phase_decision.watch_conditions":
                if not isinstance(phase_decision.get("watch_conditions"), list):
                    phase_decision["watch_conditions"] = []
            elif field == "dashboard.phase_decision.data_limitations":
                if not isinstance(phase_decision.get("data_limitations"), list):
                    phase_decision["data_limitations"] = []
            elif field in phase_decision_placeholders:
                if _is_blank_text(phase_decision.get(field.rsplit(".", 1)[-1])):
                    phase_decision[field.rsplit(".", 1)[-1]] = phase_decision_placeholders[field]


# ---------- chip_structure fallback (Issue #589) ----------

_CHIP_KEYS: tuple = ("profit_ratio", "avg_cost", "concentration", "chip_health")


def _is_value_placeholder(v: Any) -> bool:
    """True if value is empty or placeholder (N/A, 數據缺失, etc.)."""
    return is_chip_placeholder_value(v)


_RISK_WARNING_PLACEHOLDER_TEXTS = {
    "",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "tbd",
    "暫無",
    "待補充",
    "數據缺失",
    "未知",
    "無",
}

_STRUCTURAL_RISK_PHRASE_HINTS = (
    "重大利空",
    "重大風險",
    "關鍵風險",
    "減持",
    "高位減持",
    "退市",
    "退市風險",
    "停牌",
    "重大問詢",
    "處罰",
    "限售",
    "違規",
    "違規風險",
    "訴訟",
    "問詢",
    "監管",
    "財務",
    "審計",
    "爆雷",
    "暴雷",
    "違約",
    "違約風險",
    "流動性危機",
    "債務",
    "清算",
    "破產",
    "重大變臉",
    "major risk",
    "material adverse",
    "suspension",
    "delisting",
    "regulatory",
    "downgrade",
    "liquidity",
    "default",
)

_CAPITAL_FLOW_UNAVAILABLE_STATUS = {
    "not_supported",
    "not supported",
    "unsupported",
    "unavailable",
    "not_available",
    "not available",
    "none",
    "na",
    "n/a",
    "null",
    "missing",
}


def _is_meaningful_text(value: Any) -> bool:
    text = str(value).strip() if value is not None else ""
    if not text:
        return False
    lowered = text.strip().lower()
    return lowered not in _RISK_WARNING_PLACEHOLDER_TEXTS


def _safe_float(v: Any, default: float = 0.0) -> float:
    """Safely convert to float; return default on failure. Private helper for chip fill."""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        try:
            return default if math.isnan(float(v)) else float(v)
        except (ValueError, TypeError):
            return default
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def _coerce_chip_metric(v: Any) -> Optional[float]:
    """Convert chip metrics while preserving the distinction between missing and zero."""
    if v is None:
        return None
    try:
        numeric = float(v)
    except (TypeError, ValueError):
        try:
            numeric = float(str(v).strip())
        except (TypeError, ValueError):
            return None
    return None if math.isnan(numeric) else numeric


_BULLISH_TREND_HINTS: Tuple[str, ...] = (
    "多頭排列",
    "持續上漲",
    "趨勢向上",
    "上升趨勢",
    "向上發散",
    "bullish",
    "uptrend",
)
_WEAK_BULLISH_TREND_HINTS: Tuple[str, ...] = ("弱勢多頭",)
_BEARISH_TREND_HINTS: Tuple[str, ...] = (
    "空頭排列",
    "持續下跌",
    "趨勢向下",
    "下降趨勢",
    "向下發散",
    "bearish",
    "downtrend",
)
_WEAK_BEARISH_TREND_HINTS: Tuple[str, ...] = ("弱勢空頭",)
_NEGATION_TOKENS: Tuple[str, ...] = (
    "不是",
    "並非",
    "並未",
    "沒有",
    "尚不",
    "尚未",
    "未",
    "無",
    "不屬",
    "非",
    "not ",
    "no ",
)
_NEGATION_BREAK_CHARS: Tuple[str, ...] = (",", ".", ";", ":", "!", "?", "，", "。", "；", "：", "！", "？", "\n")
_NEGATION_LOOKBACK_CHARS = 16
_NEGATION_MAX_GAP_CHARS = 8
_NEGATION_SCOPE_BREAK_TOKENS: Tuple[str, ...] = (
    "而是",
    "但是",
    "但",
    "反而",
    "反倒",
    "轉為",
    "轉成",
    "改為",
    "改成",
    " but ",
    " instead ",
    " rather ",
)
_SINGLE_CHAR_NEGATION_GAP_PREFIXES: Tuple[str, ...] = (
    "形成",
    "出現",
    "進入",
    "轉為",
    "轉成",
    "構成",
    "呈現",
    "顯示",
    "屬於",
    "是",
    "有",
    "能",
    "見",
    "站",
    "守",
    "破",
)


def _normalize_prompt_reason_items(items: Any) -> List[str]:
    """Normalize prompt reason/risk items into a clean string list."""
    if not isinstance(items, list):
        return []
    normalized: List[str] = []
    for item in items:
        text = str(item).strip()
        if text:
            normalized.append(text)
    return normalized


def _contains_trend_hint(text: str, hints: Tuple[str, ...]) -> bool:
    """Return True when text contains a non-negated strong trend hint."""
    lowered = text.strip().lower()

    def _has_negation_scope_break(gap: str) -> bool:
        normalized_gap = gap.lower()
        for token in _NEGATION_SCOPE_BREAK_TOKENS:
            token_index = normalized_gap.find(token)
            if token_index > 0:
                return True
        return False

    def _is_valid_negation_gap(token: str, gap: str) -> bool:
        if not gap:
            return True
        if token not in {"未", "無", "非"}:
            return True
        return any(gap.startswith(prefix) for prefix in _SINGLE_CHAR_NEGATION_GAP_PREFIXES)

    def _is_negated_match(index: int) -> bool:
        prefix = lowered[max(0, index - _NEGATION_LOOKBACK_CHARS):index]
        for token in _NEGATION_TOKENS:
            token_index = prefix.rfind(token)
            if token_index < 0:
                continue
            gap = prefix[token_index + len(token):]
            if any(char in gap for char in _NEGATION_BREAK_CHARS):
                continue
            stripped_gap = gap.strip()
            if len(stripped_gap) > _NEGATION_MAX_GAP_CHARS:
                continue
            if _has_negation_scope_break(stripped_gap):
                continue
            if not _is_valid_negation_gap(token, stripped_gap):
                continue
            return True
        return False

    for hint in hints:
        keyword = hint.lower()
        start = 0
        while True:
            index = lowered.find(keyword, start)
            if index < 0:
                break
            if not _is_negated_match(index):
                return True
            start = index + len(keyword)
    return False


def _infer_trend_direction(trend: Dict[str, Any]) -> str:
    """Infer the final trend direction from trend_status and ma_alignment."""
    combined = " ".join(
        str(trend.get(key, "")).strip()
        for key in ("trend_status", "ma_alignment")
        if str(trend.get(key, "")).strip()
    )
    if not combined:
        return "neutral"
    lowered = combined.lower()
    normalized = lowered.replace(" ", "")
    has_bullish = (
        _contains_trend_hint(combined, _BULLISH_TREND_HINTS + _WEAK_BULLISH_TREND_HINTS)
        or "ma5>ma10>ma20" in normalized
        or (
            "ma5>ma10" in normalized
            and any(pattern in normalized for pattern in ("ma10≤ma20", "ma10<=ma20"))
        )
    )
    has_bearish = (
        _contains_trend_hint(combined, _BEARISH_TREND_HINTS + _WEAK_BEARISH_TREND_HINTS)
        or "ma5<ma10<ma20" in normalized
        or (
            "ma5<ma10" in normalized
            and any(pattern in normalized for pattern in ("ma10≥ma20", "ma10>=ma20"))
        )
    )
    if has_bullish and not has_bearish:
        return "bullish"
    if has_bearish and not has_bullish:
        return "bearish"
    return "neutral"


def _filter_conflicting_trend_items(items: List[str], conflict_hints: Tuple[str, ...]) -> List[str]:
    """Drop reasons that directly conflict with the final trend direction."""
    return [item for item in items if not _contains_trend_hint(item, conflict_hints)]


def _sanitize_trend_analysis_for_prompt(
    trend: Any,
    *,
    volume_change_ratio: Any = None,
) -> Dict[str, Any]:
    """Clean prompt-only trend hints on a derived copy without touching runtime/provider config."""
    trend_dict = dict(trend) if isinstance(trend, dict) else {}
    signal_reasons = _normalize_prompt_reason_items(trend_dict.get("signal_reasons"))
    risk_factors = _normalize_prompt_reason_items(trend_dict.get("risk_factors"))
    prompt_notes: List[str] = []
    trend_direction = _infer_trend_direction(trend_dict)

    if trend_direction == "bearish":
        filtered_signal_reasons = _filter_conflicting_trend_items(
            signal_reasons,
            _BULLISH_TREND_HINTS + _WEAK_BULLISH_TREND_HINTS,
        )
        if len(filtered_signal_reasons) != len(signal_reasons):
            prompt_notes.append("當前技術結構偏空，已剔除與空頭主判斷直接衝突的看多結構理由。")
        signal_reasons = filtered_signal_reasons
        prompt_notes.append(
            "若新聞、業績或政策催化偏多，只能表述為“事件先行、技術待確認”或“基本面偏多，但技術面尚未確認”，嚴禁寫成確定性買點。"
        )
    elif trend_direction == "bullish":
        filtered_signal_reasons = _filter_conflicting_trend_items(
            signal_reasons,
            _BEARISH_TREND_HINTS + _WEAK_BEARISH_TREND_HINTS,
        )
        if len(filtered_signal_reasons) != len(signal_reasons):
            prompt_notes.append("當前技術結構偏多，已剔除與多頭主判斷直接衝突的空頭結構理由。")
        signal_reasons = filtered_signal_reasons
        filtered_risk_factors = _filter_conflicting_trend_items(
            risk_factors,
            _BEARISH_TREND_HINTS + _WEAK_BEARISH_TREND_HINTS,
        )
        if len(filtered_risk_factors) != len(risk_factors):
            prompt_notes.append("當前技術結構偏多，已剔除與多頭主判斷直接衝突的空頭結構風險表述。")
        risk_factors = filtered_risk_factors

    parsed_volume_change = _safe_float(volume_change_ratio, default=math.nan)
    if math.isfinite(parsed_volume_change) and parsed_volume_change > 10:
        prompt_notes.append(
            f"成交量較昨日變化約 {parsed_volume_change:.2f} 倍，可能存在異常數據或一次性衝量；量能信號必須降權解讀，不能機械視為強確認。"
        )

    trend_dict["signal_reasons"] = signal_reasons
    trend_dict["risk_factors"] = risk_factors
    trend_dict["prompt_consistency_notes"] = prompt_notes
    trend_dict["prompt_trend_direction"] = trend_direction
    return trend_dict


def _derive_chip_health(profit_ratio: float, concentration_90: float, language: str = "zh") -> str:
    """Derive chip_health from profit_ratio and concentration_90."""
    if profit_ratio >= 0.9:
        return localize_chip_health("警惕", language)  # 獲利盤極高
    if concentration_90 >= 0.25:
        return localize_chip_health("警惕", language)  # 籌碼分散
    if concentration_90 < 0.15 and 0.3 <= profit_ratio < 0.9:
        return localize_chip_health("健康", language)  # 集中且獲利比例適中
    return localize_chip_health("一般", language)


def _build_chip_structure_from_data(chip_data: Any, language: str = "zh") -> Dict[str, Any]:
    """Build chip_structure dict from ChipDistribution or dict."""
    if hasattr(chip_data, "profit_ratio"):
        pr = _safe_float(chip_data.profit_ratio)
        ac = chip_data.avg_cost
        c90 = _safe_float(chip_data.concentration_90)
    else:
        d = chip_data if isinstance(chip_data, dict) else {}
        pr = _safe_float(d.get("profit_ratio"))
        ac = d.get("avg_cost")
        c90 = _safe_float(d.get("concentration_90"))
    chip_health = _derive_chip_health(pr, c90, language=language)
    return {
        "profit_ratio": f"{pr:.1%}",
        "avg_cost": ac if (ac is not None and _safe_float(ac) != 0.0) else "N/A",
        "concentration": f"{c90:.2%}",
        "chip_health": chip_health,
    }


def _has_meaningful_chip_data(chip_data: Any) -> bool:
    """Return True when chip data has the core metrics required for reporting."""
    if not chip_data:
        return False
    if hasattr(chip_data, "avg_cost"):
        avg_cost = _coerce_chip_metric(getattr(chip_data, "avg_cost", None))
        concentration_90 = _coerce_chip_metric(getattr(chip_data, "concentration_90", None))
        concentration_70 = _coerce_chip_metric(getattr(chip_data, "concentration_70", None))
    else:
        d = chip_data if isinstance(chip_data, dict) else {}
        avg_cost = _coerce_chip_metric(d.get("avg_cost"))
        concentration_90_value = d.get("concentration_90")
        if concentration_90_value is None:
            concentration_90_value = d.get("concentration")
        concentration_90 = _coerce_chip_metric(concentration_90_value)
        concentration_70 = _coerce_chip_metric(d.get("concentration_70"))
    return (
        avg_cost is not None
        and avg_cost > 0
        and (
            (concentration_90 is not None and concentration_90 >= 0)
            or (concentration_70 is not None and concentration_70 >= 0)
        )
    )


def _mark_chip_structure_unavailable(result: "AnalysisResult", language: str) -> None:
    if not result or not isinstance(result.dashboard, dict):
        return
    data_perspective = result.dashboard.get("data_perspective")
    if not isinstance(data_perspective, dict):
        return
    data_perspective["chip_structure"] = {}
    data_perspective["chip_unavailable_reason"] = get_chip_unavailable_text(language)


def normalize_chip_structure_availability(result: "AnalysisResult", chip_data: Any) -> None:
    """Fill valid chip metrics or collapse placeholder-only chip fields to one fallback line."""
    if not result:
        return
    language = getattr(result, "report_language", "zh")
    if _has_meaningful_chip_data(chip_data):
        fill_chip_structure_if_needed(result, chip_data)
        return
    _mark_chip_structure_unavailable(result, language)


def fill_chip_structure_if_needed(result: "AnalysisResult", chip_data: Any) -> None:
    """When chip_data exists, fill chip_structure placeholder fields from chip_data (in-place)."""
    if not result or not _has_meaningful_chip_data(chip_data):
        return
    try:
        if not result.dashboard:
            result.dashboard = {}
        dash = result.dashboard
        # Use `or {}` rather than setdefault so that an explicit `null` from LLM is also replaced
        dp = dash.get("data_perspective") or {}
        dash["data_perspective"] = dp
        cs = dp.get("chip_structure") or {}
        filled = _build_chip_structure_from_data(
            chip_data,
            language=getattr(result, "report_language", "zh"),
        )
        # Start from a copy of cs to preserve any extra keys the LLM may have added
        merged = dict(cs)
        for k in _CHIP_KEYS:
            if _is_value_placeholder(merged.get(k)):
                merged[k] = filled[k]
        if merged != cs:
            dp["chip_structure"] = merged
            logger.info("[chip_structure] Filled placeholder chip fields from data source (Issue #589)")
    except Exception as e:
        logger.warning("[chip_structure] Fill failed, skipping: %s", e)


_PRICE_POS_KEYS = ("ma5", "ma10", "ma20", "bias_ma5", "bias_status", "current_price", "support_level", "resistance_level")


def fill_price_position_if_needed(
    result: "AnalysisResult",
    trend_result: Any = None,
    realtime_quote: Any = None,
) -> None:
    """Fill missing price_position fields from trend_result / realtime data (in-place)."""
    if not result:
        return
    try:
        if not result.dashboard:
            result.dashboard = {}
        dash = result.dashboard
        dp = dash.get("data_perspective") or {}
        dash["data_perspective"] = dp
        pp = dp.get("price_position") or {}

        computed: Dict[str, Any] = {}
        if trend_result:
            tr = trend_result if isinstance(trend_result, dict) else (
                trend_result.__dict__ if hasattr(trend_result, "__dict__") else {}
            )
            computed["ma5"] = tr.get("ma5")
            computed["ma10"] = tr.get("ma10")
            computed["ma20"] = tr.get("ma20")
            computed["bias_ma5"] = tr.get("bias_ma5")
            computed["current_price"] = tr.get("current_price")
            support_levels = tr.get("support_levels") or []
            resistance_levels = tr.get("resistance_levels") or []
            if support_levels:
                computed["support_level"] = support_levels[0]
            if resistance_levels:
                computed["resistance_level"] = resistance_levels[0]
        if realtime_quote:
            rq = realtime_quote if isinstance(realtime_quote, dict) else (
                realtime_quote.to_dict() if hasattr(realtime_quote, "to_dict") else {}
            )
            if _is_value_placeholder(computed.get("current_price")):
                computed["current_price"] = rq.get("price")

        filled = False
        for k in _PRICE_POS_KEYS:
            if _is_value_placeholder(pp.get(k)) and not _is_value_placeholder(computed.get(k)):
                pp[k] = computed[k]
                filled = True
        if filled:
            dp["price_position"] = pp
            logger.info("[price_position] Filled placeholder fields from computed data")
    except Exception as e:
        logger.warning("[price_position] Fill failed, skipping: %s", e)


def stabilize_decision_with_structure(
    result: "AnalysisResult",
    trend_result: Any = None,
    fundamental_context: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Calibrate aggressive buy/sell advice with price levels and capital flow.

    The LLM can overreact to one-day price movement.  This guard keeps the
    public `decision_type` enum stable while allowing richer neutral wording
    such as 震盪/洗盤觀察 when support, resistance, and fund flow do not confirm
    an immediate buy/sell action.
    """
    if not result:
        return

    try:
        language = normalize_report_language(getattr(result, "report_language", "zh"))
        dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
        data_perspective = dashboard.get("data_perspective") if isinstance(dashboard, dict) else {}
        if not isinstance(data_perspective, dict):
            data_perspective = {}
        price_position = data_perspective.get("price_position")
        if not isinstance(price_position, dict):
            price_position = {}

        trend_dict = _as_dict_for_decision_guard(trend_result)
        current_price = _first_numeric_value(
            getattr(result, "current_price", None),
            price_position.get("current_price"),
            trend_dict.get("current_price"),
        )
        support = _first_numeric_value(
            price_position.get("support_level"),
            _first_list_value(trend_dict.get("support_levels")),
        )
        resistance = _first_numeric_value(
            price_position.get("resistance_level"),
            _first_list_value(trend_dict.get("resistance_levels")),
        )
        decision_type = infer_decision_type_from_advice(
            getattr(result, "decision_type", ""),
            default=getattr(result, "decision_type", "hold") or "hold",
        )
        decision_type = decision_type if decision_type in {"buy", "hold", "sell"} else "hold"
        advice_decision_type = infer_decision_type_from_advice(
            getattr(result, "operation_advice", ""),
            default="",
        )

        flow_bias, flow_reason = _capital_flow_bias_with_status(fundamental_context)
        if flow_bias == "unavailable":
            if isinstance(fundamental_context, dict) and "capital_flow" in fundamental_context:
                if decision_type == "buy" or advice_decision_type == "buy":
                    _downgrade_buy_without_capital_flow(
                        result,
                        language,
                        current_price=current_price,
                        support=support,
                        resistance=resistance,
                        flow_status=flow_reason,
                    )
                else:
                    _set_decision_stability_unavailable(
                        result,
                        language,
                        current_price=current_price,
                        support=support,
                        resistance=resistance,
                        flow_status=flow_reason,
                    )
            return

        if current_price is None:
            return

        broke_support = support is not None and current_price < support * 0.985
        near_support = support is not None and not broke_support and current_price <= support * 1.03
        breakout = resistance is not None and current_price > resistance * 1.01
        near_resistance = (
            resistance is not None
            and not breakout
            and current_price >= resistance * 0.97
        )
        mid_range = (
            support is not None
            and resistance is not None
            and support * 1.03 < current_price < resistance * 0.97
        )

        has_significant_risk = _has_structural_risk_alert(result)

        if decision_type == "buy":
            if near_resistance and flow_bias != "inflow":
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="range",
                    reason_key="buy_near_resistance",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif flow_bias == "outflow" and not breakout:
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="range",
                    reason_key="buy_with_outflow",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif mid_range and flow_bias == "neutral":
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="range",
                    reason_key="hold_mid_range",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
        elif decision_type == "sell":
            if near_support and (flow_bias != "outflow") and not has_significant_risk:
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="shakeout",
                    reason_key="sell_near_support",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif flow_bias == "inflow" and not broke_support and not has_significant_risk:
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="hold",
                    reason_key="sell_with_inflow",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
        elif decision_type == "hold":
            change_pct = _first_numeric_value(getattr(result, "change_pct", None))
            if change_pct is not None and change_pct < 0 and near_support and flow_bias != "outflow":
                _set_structural_hold_wording(
                    result,
                    language,
                    advice_key="shakeout",
                    reason_key="hold_shakeout",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif mid_range and flow_bias == "neutral":
                _set_structural_hold_wording(
                    result,
                    language,
                    advice_key="range",
                    reason_key="hold_mid_range",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
        _sync_stability_dashboard_fields(result)
    except Exception as exc:
        logger.warning("[decision_stability] skipped: %s", exc)


def _has_structural_risk_alert(result: "AnalysisResult") -> bool:
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}

    risk_text = getattr(result, "risk_warning", "")
    if _is_significant_structural_risk(risk_text):
        return True

    intelligence = dashboard.get("intelligence") if isinstance(dashboard, dict) else None
    if isinstance(intelligence, dict):
        risk_alerts = intelligence.get("risk_alerts")
        if isinstance(risk_alerts, str):
            if _is_significant_structural_risk(risk_alerts):
                return True
        elif isinstance(risk_alerts, (list, tuple, set)):
            if any(_is_significant_structural_risk(item) for item in risk_alerts):
                return True

    core_conclusion = dashboard.get("core_conclusion") if isinstance(dashboard, dict) else None
    if isinstance(core_conclusion, dict):
        signal_type = str(core_conclusion.get("signal_type", "")).strip()
        if _is_significant_structural_risk(signal_type):
            return True
    return False


def _is_significant_structural_risk(value: Any) -> bool:
    text = str(value or "").strip()
    if not _is_meaningful_text(text):
        return False

    normalized = text.lower()
    if any(keyword in normalized for keyword in _STRUCTURAL_RISK_PHRASE_HINTS):
        return True

    return "重大" in text and "風險" in normalized


def _sync_stability_dashboard_fields(result: "AnalysisResult") -> None:
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
    dashboard["sentiment_score"] = getattr(result, "sentiment_score", None)
    dashboard["operation_advice"] = getattr(result, "operation_advice", None)
    dashboard["decision_type"] = getattr(result, "decision_type", None)


def _as_dict_for_decision_guard(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        try:
            converted = value.to_dict()
            return converted if isinstance(converted, dict) else {}
        except Exception:
            return {}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _first_list_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    return value


def _coerce_numeric_value(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if math.isfinite(float(value)):
            return float(value)
        return None
    text = str(value).replace(",", "").replace("，", "").strip()
    if not text or text.upper() in {"N/A", "NA", "NONE", "NULL"}:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _first_numeric_value(*values: Any) -> Optional[float]:
    for value in values:
        if isinstance(value, (list, tuple)):
            nested = _first_numeric_value(*value)
            if nested is not None:
                return nested
            continue
        numeric = _coerce_numeric_value(value)
        if numeric is not None:
            return numeric
    return None


def _capital_flow_bias(fundamental_context: Optional[Dict[str, Any]]) -> str:
    return _capital_flow_bias_with_status(fundamental_context)[0]


def _capital_flow_bias_with_status(
    fundamental_context: Optional[Dict[str, Any]],
) -> tuple[str, str]:
    if not isinstance(fundamental_context, dict):
        return "unavailable", "invalid_context"
    block = fundamental_context.get("capital_flow")
    if not isinstance(block, dict):
        return "unavailable", "capital_flow_block_missing"
    status = str(block.get("status") or "").strip().lower()
    normalized_status = status.replace("-", " ").replace("_", " ").strip()
    if normalized_status in _CAPITAL_FLOW_UNAVAILABLE_STATUS or "not supported" in normalized_status:
        return "unavailable", status or "not_supported"
    data = block.get("data") if isinstance(block.get("data"), dict) else block
    stock_flow = data.get("stock_flow") if isinstance(data, dict) else None
    if not isinstance(stock_flow, dict) or not stock_flow:
        return "unavailable", "empty_stock_flow"

    def _flow_direction(value: Optional[float]) -> Optional[str]:
        if value is None or value == 0:
            return None
        return "inflow" if value > 0 else "outflow"

    numeric_values = [
        _coerce_numeric_value(stock_flow.get("main_net_inflow")),
        _coerce_numeric_value(stock_flow.get("inflow_5d")),
        _coerce_numeric_value(stock_flow.get("inflow_10d")),
    ]
    if all(value is None for value in numeric_values):
        return "unavailable", "missing_or_na_flow_fields"

    ordered_signals = [
        _flow_direction(value) for value in numeric_values
    ]
    directions = {signal for signal in ordered_signals if signal is not None}
    if not directions or len(directions) > 1:
        return "neutral", "conflict_or_missing"
    for signal in ordered_signals:
        if signal is not None:
            return signal, "ok"
    return "neutral", "neutral"


def _capital_flow_status_for_stability(reason: str, language: str) -> str:
    normalized = str(reason or "").strip().lower()
    if "not_supported" in normalized or "unsupported" in normalized or "not available" in normalized:
        return "市場資金流服務暫不支持" if language == "zh" else "Capital flow source unsupported"
    if "empty_stock_flow" in normalized or "missing" in normalized:
        return "資金流數據缺失" if language == "zh" else "capital flow data unavailable"
    return "資金流數據不可用" if language == "zh" else "capital flow unavailable"


def _set_decision_stability_unavailable(
    result: "AnalysisResult",
    language: str,
    *,
    current_price: Optional[float],
    support: Optional[float],
    resistance: Optional[float],
    flow_status: str,
) -> None:
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
    dashboard["decision_stability"] = {
        "applied": False,
        "reason": "資金流不可用，未使用資金流校準" if language == "zh" else "Capital flow unavailable; stability calibration not applied",
        "capital_flow_status": _capital_flow_status_for_stability(flow_status, language),
        "current_price": current_price,
        "support": support,
        "resistance": resistance,
        "capital_flow_bias": "unavailable",
    }
    _sync_stability_dashboard_fields(result)


def _record_decision_score_calibration(
    result: "AnalysisResult",
    *,
    raw_score: int,
    adjusted_score: int,
    final_action: str,
    guardrail_reason: Optional[str],
) -> None:
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
    calibration = score_band_metadata(raw_score)
    calibration.update(
        {
            "raw_score": raw_score,
            "adjusted_score": adjusted_score,
            "final_action": final_action,
        }
    )
    if guardrail_reason:
        calibration["guardrail_reason"] = guardrail_reason
    dashboard["decision_score_calibration"] = calibration


def _bound_hold_watch_sentiment_score(
    result: "AnalysisResult",
    *,
    reason: Optional[str] = None,
    final_action: str = "watch",
) -> None:
    try:
        score = int(getattr(result, "sentiment_score", 50))
    except (TypeError, ValueError):
        score = 50
    adjusted_score = min(59, max(45, score))
    result.sentiment_score = adjusted_score
    _record_decision_score_calibration(
        result,
        raw_score=score,
        adjusted_score=adjusted_score,
        final_action=final_action,
        guardrail_reason=reason,
    )


def _apply_hold_watch_dashboard(
    result: "AnalysisResult",
    language: str,
    *,
    advice: str,
    reason: str,
    current_price: Optional[float],
    support: Optional[float],
    resistance: Optional[float],
    flow_bias: str,
    no_position: str,
    has_position: str,
    capital_flow_status: Optional[str] = None,
) -> None:
    result.operation_advice = advice

    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
    core = dashboard.get("core_conclusion")
    if not isinstance(core, dict):
        core = {}
        dashboard["core_conclusion"] = core
    core["signal_type"] = "🟡持有觀望" if language == "zh" else "🟡 Hold / Watch"
    core["one_sentence"] = f"{advice}：{reason}" if language == "zh" else f"{advice}: {reason}"

    position_advice = core.get("position_advice")
    if not isinstance(position_advice, dict):
        position_advice = {}
        core["position_advice"] = position_advice
    position_advice["no_position"] = no_position
    position_advice["has_position"] = has_position

    stability = {
        "applied": True,
        "reason": reason,
        "current_price": current_price,
        "support": support,
        "resistance": resistance,
        "capital_flow_bias": flow_bias,
    }
    if capital_flow_status is not None:
        stability["capital_flow_status"] = capital_flow_status
    score_calibration = dashboard.get("decision_score_calibration")
    if isinstance(score_calibration, dict):
        stability["raw_score"] = score_calibration.get("raw_score")
        stability["adjusted_score"] = score_calibration.get("adjusted_score")
        stability["final_action"] = score_calibration.get("final_action")
    dashboard["decision_stability"] = stability

    if reason and reason not in str(result.risk_warning or ""):
        sep = "；" if language == "zh" else "; "
        result.risk_warning = f"{result.risk_warning}{sep}{reason}" if result.risk_warning else reason
    result.buy_reason = reason or result.buy_reason


def _downgrade_buy_without_capital_flow(
    result: "AnalysisResult",
    language: str,
    *,
    current_price: Optional[float],
    support: Optional[float],
    resistance: Optional[float],
    flow_status: str,
) -> None:
    status_text = _capital_flow_status_for_stability(flow_status, language)
    if language == "zh":
        advice = "持有觀察"
        reason = f"{status_text}，買入結論缺少資金面確認，先按觀察處理。"
        no_position = "空倉先不追買，等待資金流恢復、支撐確認或有效突破後再行動。"
        has_position = "持倉以關鍵支撐為風控線，資金流恢復前控制倉位。"
        confidence = "低"
    else:
        advice = "Hold and watch"
        reason = f"{status_text}; the buy call lacks capital-flow confirmation, so treat it as watch-only."
        no_position = "Do not chase; wait for capital-flow recovery, support confirmation, or a valid breakout."
        has_position = "Use key support as the risk line and keep position size controlled until capital flow recovers."
        confidence = "Low"

    result.decision_type = "hold"
    result.confidence_level = confidence
    _bound_hold_watch_sentiment_score(result, reason=reason, final_action="hold")
    _apply_hold_watch_dashboard(
        result,
        language,
        advice=advice,
        reason=reason,
        current_price=current_price,
        support=support,
        resistance=resistance,
        flow_bias="unavailable",
        no_position=no_position,
        has_position=has_position,
        capital_flow_status=status_text,
    )
    _sync_stability_dashboard_fields(result)
    logger.info("[decision_stability] Downgraded buy because capital flow is unavailable: %s", flow_status)


def _downgrade_to_structural_hold(
    result: "AnalysisResult",
    language: str,
    *,
    advice_key: str,
    reason_key: str,
    current_price: float,
    support: Optional[float],
    resistance: Optional[float],
    flow_bias: str,
) -> None:
    result.decision_type = "hold"
    _set_structural_hold_wording(
        result,
        language,
        advice_key=advice_key,
        reason_key=reason_key,
        current_price=current_price,
        support=support,
        resistance=resistance,
        flow_bias=flow_bias,
        calibrate_score=True,
    )


def _set_structural_hold_wording(
    result: "AnalysisResult",
    language: str,
    *,
    advice_key: str,
    reason_key: str,
    current_price: float,
    support: Optional[float],
    resistance: Optional[float],
    flow_bias: str,
    calibrate_score: bool = False,
) -> None:
    advice_map = {
        "zh": {
            "range": "震盪觀望",
            "shakeout": "洗盤觀察",
            "hold": "持有觀察",
        },
        "en": {
            "range": "Range-bound watch",
            "shakeout": "Shakeout watch",
            "hold": "Hold and watch",
        },
        "ko": {
            "range": "박스권 관망",
            "shakeout": "흔들기 관찰",
            "hold": "보유 관찰",
        },
    }
    advice_default = {"zh": "持有觀察", "en": "Hold and watch", "ko": "보유 관찰"}.get(language, "Hold and watch")
    advice = advice_map.get(language, advice_map["en"]).get(advice_key, advice_default)
    reason_templates = {
        "zh": {
            "buy_near_resistance": "價格接近壓力位且主力資金未確認流入，不宜僅因短線反彈追買。",
            "buy_with_outflow": "主力資金流出與買入結論衝突，買點需等待支撐確認或資金迴流。",
            "sell_near_support": "價格貼近支撐且未見資金持續流出，不宜僅因單日下跌直接賣出。",
            "sell_with_inflow": "主力資金流入與賣出結論衝突，先按持有觀察處理並跟蹤支撐失效。",
            "hold_shakeout": "價格回落至支撐附近但資金未確認流出，更適合按洗盤觀察處理。",
            "hold_mid_range": "價格處於支撐與壓力之間且資金流不明確，維持震盪觀望更可操作。",
        },
        "en": {
            "buy_near_resistance": "Price is near resistance without confirmed main-force inflow, so chasing the rebound is not actionable.",
            "buy_with_outflow": "Main-force outflow conflicts with a buy call; wait for support confirmation or capital inflow.",
            "sell_near_support": "Price is near support without sustained outflow, so a one-day drop is not enough to sell.",
            "sell_with_inflow": "Main-force inflow conflicts with a sell call; hold and watch for support failure.",
            "hold_shakeout": "Price pulled back near support without confirmed outflow, which is better treated as a shakeout watch.",
            "hold_mid_range": "Price is between support and resistance with neutral fund flow, so range-bound watch is more actionable.",
        },
        "ko": {
            "buy_near_resistance": "가격이 저항선에 근접했고 주력 자금 유입이 확인되지 않아 단기 반등만 보고 추격 매수하기 어렵습니다.",
            "buy_with_outflow": "주력 자금 유출이 매수 결론과 상충하므로 지지 확인이나 자금 재유입을 기다려야 합니다.",
            "sell_near_support": "가격이 지지선에 근접했고 지속적 유출이 없어 하루 하락만으로 매도하기 어렵습니다.",
            "sell_with_inflow": "주력 자금 유입이 매도 결론과 상충하므로 우선 보유 관찰하며 지지 이탈을 추적합니다.",
            "hold_shakeout": "가격이 지지선 부근까지 눌렸지만 유출이 확인되지 않아 흔들기 관찰로 처리하는 것이 적절합니다.",
            "hold_mid_range": "가격이 지지선과 저항선 사이이고 자금 흐름이 불명확해 박스권 관망이 더 실행 가능합니다.",
        },
    }
    reason = reason_templates.get(language, reason_templates["en"]).get(reason_key, "")
    if calibrate_score:
        final_action = "watch" if advice_key in {"range", "shakeout"} else "hold"
        _bound_hold_watch_sentiment_score(result, reason=reason, final_action=final_action)
    result.operation_advice = advice
    if advice_key == "range":
        if language == "zh" and "震盪" not in str(result.trend_prediction):
            result.trend_prediction = "震盪"
        elif language == "en":
            result.trend_prediction = "Sideways"
        elif language == "ko":
            result.trend_prediction = "횡보"

    if language == "zh":
        no_position = "空倉先不追漲殺跌，等待支撐確認、放量突破或資金迴流後再行動。"
        has_position = "持倉以關鍵支撐為風控線，未跌破前以觀察和分批控倉為主。"
    elif language == "ko":
        no_position = "현금 보유 시 추격·투매를 삼가고 지지 확인·대량 돌파·자금 재유입 후 행동하세요."
        has_position = "보유 시 핵심 지지선을 리스크 관리선으로 삼고, 이탈 전까지 관찰과 분할 관리 위주로 대응하세요."
    else:
        no_position = "Do not chase or panic; wait for support confirmation, breakout, or renewed inflow."
        has_position = "Use key support as the risk line and manage position size unless support fails."
    _apply_hold_watch_dashboard(
        result,
        language,
        advice=advice,
        reason=reason,
        current_price=current_price,
        support=support,
        resistance=resistance,
        flow_bias=flow_bias,
        no_position=no_position,
        has_position=has_position,
    )
    logger.info("[decision_stability] Applied structural hold calibration: %s", reason_key)


def get_stock_name_multi_source(
    stock_code: str,
    context: Optional[Dict] = None,
    data_manager = None
) -> str:
    """
    多來源獲取股票中文名稱

    獲取策略（按優先級）：
    1. 從傳入的 context 中獲取（realtime 數據）
    2. 從靜態映射表 STOCK_NAME_MAP 獲取
    3. 從 DataFetcherManager 獲取（各數據源）
    4. 返回默認名稱（股票+代碼）

    Args:
        stock_code: 股票代碼
        context: 分析上下文（可選）
        data_manager: DataFetcherManager 實例（可選）

    Returns:
        股票中文名稱
    """
    # 1. 從上下文獲取（實時行情數據）
    if context:
        # 優先從 stock_name 字段獲取
        if context.get('stock_name'):
            name = context['stock_name']
            if name and not name.startswith('股票'):
                return name

        # 其次從 realtime 數據獲取
        if 'realtime' in context and context['realtime'].get('name'):
            return context['realtime']['name']

    # 2. 從靜態映射表獲取
    if stock_code in STOCK_NAME_MAP:
        return STOCK_NAME_MAP[stock_code]

    # 3. 從數據源獲取
    if data_manager is None:
        try:
            from data_provider.base import DataFetcherManager
            data_manager = DataFetcherManager()
        except Exception as e:
            logger.debug(f"無法初始化 DataFetcherManager: {e}")

    if data_manager:
        try:
            name = data_manager.get_stock_name(stock_code)
            if name:
                # 更新緩存
                STOCK_NAME_MAP[stock_code] = name
                return name
        except Exception as e:
            logger.debug(f"從數據源獲取股票名稱失敗: {e}")

    # 4. 返回默認名稱
    return f'股票{stock_code}'


@dataclass
class AnalysisResult:
    """
    AI 分析結果數據類 - 決策儀表盤版

    封裝 Gemini 返回的分析結果，包含決策儀表盤和詳細分析
    """
    code: str
    name: str

    # ========== 核心指標 ==========
    sentiment_score: int  # 綜合評分 0-100 (>70強烈看多, >60看多, 40-60震盪, <40看空)
    trend_prediction: str  # 趨勢預測：強烈看多/看多/震盪/看空/強烈看空
    operation_advice: str  # 操作建議：買入/加倉/持有/減倉/賣出/觀望
    decision_type: str = "hold"  # 決策類型：buy/hold/sell（用於統計）
    confidence_level: str = "中"  # 置信度：高/中/低
    report_language: str = "zh"  # 報告輸出語言：zh/en
    action: Optional[str] = None  # 建議動作 taxonomy：buy/add/hold/reduce/sell/watch/avoid/alert
    action_label: Optional[str] = None  # 本地化建議動作標籤

    # ========== 決策儀表盤 (新增) ==========
    dashboard: Optional[Dict[str, Any]] = None  # 完整的決策儀表盤數據

    # ========== 走勢分析 ==========
    trend_analysis: str = ""  # 走勢形態分析（支撐位、壓力位、趨勢線等）
    short_term_outlook: str = ""  # 短期展望（1-3日）
    medium_term_outlook: str = ""  # 中期展望（1-2周）

    # ========== 技術面分析 ==========
    technical_analysis: str = ""  # 技術指標綜合分析
    ma_analysis: str = ""  # 均線分析（多頭/空頭排列，金叉/死叉等）
    volume_analysis: str = ""  # 量能分析（放量/縮量，主力動向等）
    pattern_analysis: str = ""  # K線形態分析

    # ========== 基本面分析 ==========
    fundamental_analysis: str = ""  # 基本面綜合分析
    sector_position: str = ""  # 板塊地位和行業趨勢
    company_highlights: str = ""  # 公司亮點/風險點

    # ========== 情緒面/消息面分析 ==========
    news_summary: str = ""  # 近期重要新聞/公告摘要
    market_sentiment: str = ""  # 市場情緒分析
    hot_topics: str = ""  # 相關熱點話題

    # ========== 綜合分析 ==========
    analysis_summary: str = ""  # 綜合分析摘要
    key_points: str = ""  # 核心看點（3-5個要點）
    risk_warning: str = ""  # 風險提示
    buy_reason: str = ""  # 買入/賣出理由

    # ========== 元數據 ==========
    market_snapshot: Optional[Dict[str, Any]] = None  # 當日行情快照（展示用）
    raw_response: Optional[str] = None  # 原始響應（調試用）
    search_performed: bool = False  # 是否執行了聯網搜索
    data_sources: str = ""  # 數據來源說明
    success: bool = True
    error_message: Optional[str] = None

    # ========== 價格數據（分析時快照）==========
    current_price: Optional[float] = None  # 分析時的股價
    change_pct: Optional[float] = None     # 分析時的漲跌幅(%)

    # ========== 模型標記（Issue #528）==========
    model_used: Optional[str] = None  # 分析使用的 LLM 模型（完整名，如 gemini/gemini-2.0-flash）

    # ========== 歷史對比（Report Engine P0）==========
    query_id: Optional[str] = None  # 本次分析 query_id，用於歷史對比時排除本次記錄

    # ========== 基本面上下文（僅運行時，用於通知拼裝；不持久化到 to_dict）==========
    fundamental_context: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典"""
        return {
            'code': self.code,
            'name': self.name,
            'sentiment_score': self.sentiment_score,
            'trend_prediction': self.trend_prediction,
            'operation_advice': self.operation_advice,
            'decision_type': self.decision_type,
            'confidence_level': self.confidence_level,
            'report_language': self.report_language,
            'action': self.action,
            'action_label': self.action_label,
            'dashboard': self.dashboard,  # 決策儀表盤數據
            'trend_analysis': self.trend_analysis,
            'short_term_outlook': self.short_term_outlook,
            'medium_term_outlook': self.medium_term_outlook,
            'technical_analysis': self.technical_analysis,
            'ma_analysis': self.ma_analysis,
            'volume_analysis': self.volume_analysis,
            'pattern_analysis': self.pattern_analysis,
            'fundamental_analysis': self.fundamental_analysis,
            'sector_position': self.sector_position,
            'company_highlights': self.company_highlights,
            'news_summary': self.news_summary,
            'market_sentiment': self.market_sentiment,
            'hot_topics': self.hot_topics,
            'analysis_summary': self.analysis_summary,
            'key_points': self.key_points,
            'risk_warning': self.risk_warning,
            'buy_reason': self.buy_reason,
            'market_snapshot': self.market_snapshot,
            'search_performed': self.search_performed,
            'success': self.success,
            'error_message': self.error_message,
            'current_price': self.current_price,
            'change_pct': self.change_pct,
            'model_used': self.model_used,
        }

    def get_core_conclusion(self) -> str:
        """獲取核心結論（一句話）"""
        if self.dashboard and 'core_conclusion' in self.dashboard:
            return self.dashboard['core_conclusion'].get('one_sentence', self.analysis_summary)
        return self.analysis_summary

    def get_position_advice(self, has_position: bool = False) -> str:
        """獲取持倉建議"""
        if self.dashboard and 'core_conclusion' in self.dashboard:
            pos_advice = self.dashboard['core_conclusion'].get('position_advice', {})
            if has_position:
                return pos_advice.get('has_position', self.operation_advice)
            return pos_advice.get('no_position', self.operation_advice)
        return self.operation_advice

    def get_sniper_points(self) -> Dict[str, str]:
        """獲取狙擊點位"""
        if self.dashboard and 'battle_plan' in self.dashboard:
            return self.dashboard['battle_plan'].get('sniper_points', {})
        return {}

    def get_checklist(self) -> List[str]:
        """獲取檢查清單"""
        if self.dashboard and 'battle_plan' in self.dashboard:
            return self.dashboard['battle_plan'].get('action_checklist', [])
        return []

    def get_risk_alerts(self) -> List[str]:
        """獲取風險警報"""
        if self.dashboard and 'intelligence' in self.dashboard:
            return self.dashboard['intelligence'].get('risk_alerts', [])
        return []

    def get_emoji(self) -> str:
        """根據操作建議返回對應 emoji"""
        _, emoji, _ = get_signal_level(
            self.operation_advice,
            self.sentiment_score,
            self.report_language,
        )
        return emoji

    def get_confidence_stars(self) -> str:
        """返回置信度星級"""
        star_map = {
            "高": "⭐⭐⭐",
            "high": "⭐⭐⭐",
            "中": "⭐⭐",
            "medium": "⭐⭐",
            "低": "⭐",
            "low": "⭐",
        }
        return star_map.get(str(self.confidence_level or "").strip().lower(), "⭐⭐")


def populate_decision_action_fields(
    result: AnalysisResult,
    *,
    explicit_action: Any = None,
    report_type: Any = None,
    use_existing_action: bool = True,
    align_with_score: bool = True,
) -> AnalysisResult:
    """Populate optional decision action fields without changing legacy advice."""

    action_source = explicit_action
    if action_source is None and use_existing_action:
        action_source = getattr(result, "action", None)

    fields = build_action_fields(
        operation_advice=getattr(result, "operation_advice", None),
        explicit_action=action_source,
        report_type=report_type,
        report_language=getattr(result, "report_language", "zh"),
        sentiment_score=getattr(result, "sentiment_score", None),
        guardrail_reason=getattr(result, "guardrail_reason", None),
        align_with_score=align_with_score,
    )
    result.action = fields["action"]
    result.action_label = fields["action_label"]
    return result


class GeminiAnalyzer:
    """
    Gemini AI 分析器

    職責：
    1. 調用 Google Gemini API 進行股票分析
    2. 結合預先搜索的新聞和技術面數據生成分析報告
    3. 解析 AI 返回的 JSON 格式結果

    使用方式：
        analyzer = GeminiAnalyzer()
        result = analyzer.analyze(context, news_context)
    """

    # ========================================
    # 系統提示詞 - 決策儀表盤 v2.0
    # ========================================
    # 輸出格式升級：從簡單信號升級為決策儀表盤
    # 核心模塊：核心結論 + 數據透視 + 輿情情報 + 作戰計劃
    # ========================================

    LEGACY_DEFAULT_SYSTEM_PROMPT = """你是一位專注於趨勢交易的{market_placeholder}投資分析師，負責生成專業的【決策儀表盤】分析報告。

{guidelines_placeholder}

""" + CORE_TRADING_SKILL_POLICY_ZH + """

""" + CANONICAL_DECISION_SCALE_PROMPT_ZH + """

## 輸出格式：決策儀表盤 JSON

請嚴格按照以下 JSON 格式輸出，這是一個完整的【決策儀表盤】：

```json
{
    "stock_name": "股票中文名稱",
    "sentiment_score": 0-100整數,
    "trend_prediction": "強烈看多/看多/震盪/看空/強烈看空",
    "operation_advice": "買入/加倉/持有/減倉/賣出/觀望",
    "decision_type": "buy/hold/sell",
    "action": "buy/add/hold/reduce/sell/watch/avoid/alert",
    "guardrail_reason": "當分數區間與最終 action 不一致時填寫降級/升級原因，否則留空",
    "confidence_level": "高/中/低",

    "dashboard": {
        "core_conclusion": {
            "one_sentence": "一句話核心結論（30字以內，直接告訴用戶做什麼）",
            "signal_type": "🟢買入信號/🟡持有觀望/🔴賣出信號/⚠️風險警告",
            "time_sensitivity": "立即行動/今日內/本週內/不急",
            "position_advice": {
                "no_position": "空倉者建議：具體操作指引",
                "has_position": "持倉者建議：具體操作指引"
            }
        },

        "data_perspective": {
            "trend_status": {
                "ma_alignment": "均線排列狀態描述",
                "is_bullish": true/false,
                "trend_score": 0-100
            },
            "price_position": {
                "current_price": 當前價格數值,
                "ma5": MA5數值,
                "ma10": MA10數值,
                "ma20": MA20數值,
                "bias_ma5": 乖離率百分比數值,
                "bias_status": "安全/警戒/危險",
                "support_level": 支撐位價格,
                "resistance_level": 壓力位價格
            },
            "volume_analysis": {
                "volume_ratio": 量比數值,
                "volume_status": "放量/縮量/平量",
                "turnover_rate": 換手率百分比,
                "volume_meaning": "量能含義解讀（如：縮量回調錶示拋壓減輕）"
            },
            "chip_structure": {
                "profit_ratio": 獲利比例,
                "avg_cost": 平均成本,
                "concentration": 籌碼集中度,
                "chip_health": "健康/一般/警惕"
            }
        },

        "intelligence": {
            "latest_news": "【最新消息】近期重要新聞摘要",
            "risk_alerts": ["風險點1：具體描述", "風險點2：具體描述"],
            "positive_catalysts": ["利好1：具體描述", "利好2：具體描述"],
            "earnings_outlook": "業績預期分析（基於年報預告、業績快報等）",
            "sentiment_summary": "輿情情緒一句話總結"
        },

        "battle_plan": {
            "sniper_points": {
                "ideal_buy": "理想買入點：XX元（在MA5附近）",
                "secondary_buy": "次優買入點：XX元（在MA10附近）",
                "stop_loss": "止損位：XX元（跌破MA20或X%）",
                "take_profit": "目標位：XX元（前高/整數關口）"
            },
            "position_strategy": {
                "suggested_position": "建議倉位：X成",
                "entry_plan": "分批建倉策略描述",
                "risk_control": "風控策略描述"
            },
            "action_checklist": [
                "✅/⚠️/❌ 檢查項1：多頭排列",
                "✅/⚠️/❌ 檢查項2：乖離率合理（強勢趨勢可放寬）",
                "✅/⚠️/❌ 檢查項3：量能配合",
                "✅/⚠️/❌ 檢查項4：無重大利空",
                "✅/⚠️/❌ 檢查項5：籌碼健康",
                "✅/⚠️/❌ 檢查項6：PE估值合理"
            ]
        },

        "phase_decision": {
            "phase_context": {"phase": "premarket/intraday/lunch_break/closing_auction/postmarket/non_trading/unknown"},
            "action_window": "盤前計劃/盤中跟蹤/午間確認/收盤前風控/盤後復盤/非交易日觀察",
            "immediate_action": "立即行動/等待確認/觀察/止損止盈預警/禁止追高/無盤中動作",
            "watch_conditions": ["觀察條件1", "觀察條件2"],
            "next_check_time": "下一次檢查點或市場本地時間",
            "confidence_reason": "置信度理由，說明階段和數據質量限制",
            "data_limitations": ["階段或數據質量限制1", "階段或數據質量限制2"]
        },

        "signal_attribution": {
            "technical_indicators": 技術指標貢獻度(0-100),
            "news_sentiment": 新聞輿情貢獻度(0-100),
            "fundamentals": 基本面貢獻度(0-100),
            "market_conditions": 市場環境貢獻度(0-100),
            "strongest_bullish_signal": "最強看多信號名稱",
            "strongest_bearish_signal": "最強看空信號名稱"
        }
    },

    "analysis_summary": "100字綜合分析摘要",
    "key_points": "3-5個核心看點，逗號分隔",
    "risk_warning": "風險提示",
    "buy_reason": "操作理由，引用交易理念",

    "trend_analysis": "走勢形態分析",
    "short_term_outlook": "短期1-3日展望",
    "medium_term_outlook": "中期1-2周展望",
    "technical_analysis": "技術面綜合分析",
    "ma_analysis": "均線系統分析",
    "volume_analysis": "量能分析",
    "pattern_analysis": "K線形態分析",
    "fundamental_analysis": "基本面分析",
    "sector_position": "板塊行業分析",
    "company_highlights": "公司亮點/風險",
    "news_summary": "新聞摘要",
    "market_sentiment": "市場情緒",
    "hot_topics": "相關熱點",

    "search_performed": true/false,
    "data_sources": "數據來源說明"
}
```

## 評分標準

### 強烈買入（80-100分）：
- ✅ 多頭排列：MA5 > MA10 > MA20
- ✅ 低乖離率：<2%，最佳買點
- ✅ 縮量回調或放量突破
- ✅ 籌碼集中健康
- ✅ 消息面有利好催化

### 買入（60-79分）：
- ✅ 多頭排列或弱勢多頭
- ✅ 乖離率 <5%
- ✅ 量能正常
- ⚪ 允許一項次要條件不滿足

### 觀望（40-59分）：
- ⚠️ 乖離率 >5%（追高風險）
- ⚠️ 均線纏繞趨勢不明
- ⚠️ 有風險事件

### 減倉（20-39分）：
- ⚠️ 趨勢走弱或跌破關鍵均線
- ⚠️ 資金/量能轉弱，風險明顯高於收益
- ⚠️ 以降低倉位和保護收益為主

### 賣出（0-19分）：
- ❌ 空頭排列或趨勢顯著惡化
- ❌ 跌破關鍵支撐/止損位
- ❌ 放量下跌或重大利空

## 決策儀表盤核心原則

1. **核心結論先行**：一句話說清該買該賣
2. **分持倉建議**：空倉者和持倉者給不同建議
3. **精確狙擊點**：必須給出具體價格，不說模糊的話
4. **檢查清單可視化**：用 ✅⚠️❌ 明確顯示每項檢查結果
5. **風險優先級**：輿情中的風險點要醒目標出

## 可操作性與穩定性約束

- 不得僅因為單日漲跌或評分跨線就在“買入/賣出”之間劇烈切換。
- 操作建議必須同時參考價格位置（支撐/壓力位）、量能/籌碼、主力資金流向和風險事件。
- 股價位於支撐與壓力之間、資金流不明確時，優先輸出“持有/震盪/觀望/洗盤觀察”等可執行的中性建議；`decision_type` 仍保持 `hold`。
- 只有在接近支撐確認或有效突破壓力，且資金流/量價配合時，才能給出買入；接近壓力且資金流出時不得追買。
- 只有在跌破關鍵支撐、主力資金持續流出或風險顯著放大時，才能給出賣出/減倉。
- 必須輸出 `dashboard.phase_decision` 七字段；盤中/午休/臨近收盤要給出當前動作、觀察條件和下一次檢查點。
- 建議輸出可選展示字段 `dashboard.signal_attribution` 六字段；解釋推薦理由的構成，包括技術指標、新聞輿情、基本面、市場環境的貢獻度，以及最強看多/看空信號。
- 盤前、非交易日或未知階段不得偽造今日盤中走勢；quote/daily_bars/technical 存在 stale、fallback、missing、fetch_failed、partial 或 estimated 時，`confidence_level` 不得為高。"""

    SYSTEM_PROMPT = """你是一位{market_placeholder}投資分析師，負責生成專業的【決策儀表盤】分析報告。

{guidelines_placeholder}

{default_skill_policy_section}
{skills_section}

""" + CANONICAL_DECISION_SCALE_PROMPT_ZH + """

## 輸出格式：決策儀表盤 JSON

請嚴格按照以下 JSON 格式輸出，這是一個完整的【決策儀表盤】：

```json
{
    "stock_name": "股票中文名稱",
    "sentiment_score": 0-100整數,
    "trend_prediction": "強烈看多/看多/震盪/看空/強烈看空",
    "operation_advice": "買入/加倉/持有/減倉/賣出/觀望",
    "decision_type": "buy/hold/sell",
    "action": "buy/add/hold/reduce/sell/watch/avoid/alert",
    "guardrail_reason": "當分數區間與最終 action 不一致時填寫降級/升級原因，否則留空",
    "confidence_level": "高/中/低",

    "dashboard": {
        "core_conclusion": {
            "one_sentence": "一句話核心結論（30字以內，直接告訴用戶做什麼）",
            "signal_type": "🟢買入信號/🟡持有觀望/🔴賣出信號/⚠️風險警告",
            "time_sensitivity": "立即行動/今日內/本週內/不急",
            "position_advice": {
                "no_position": "空倉者建議：具體操作指引",
                "has_position": "持倉者建議：具體操作指引"
            }
        },

        "data_perspective": {
            "trend_status": {
                "ma_alignment": "均線排列狀態描述",
                "is_bullish": true/false,
                "trend_score": 0-100
            },
            "price_position": {
                "current_price": 當前價格數值,
                "ma5": MA5數值,
                "ma10": MA10數值,
                "ma20": MA20數值,
                "bias_ma5": 乖離率百分比數值,
                "bias_status": "安全/警戒/危險",
                "support_level": 支撐位價格,
                "resistance_level": 壓力位價格
            },
            "volume_analysis": {
                "volume_ratio": 量比數值,
                "volume_status": "放量/縮量/平量",
                "turnover_rate": 換手率百分比,
                "volume_meaning": "量能含義解讀（如：縮量回調錶示拋壓減輕）"
            },
            "chip_structure": {
                "profit_ratio": 獲利比例,
                "avg_cost": 平均成本,
                "concentration": 籌碼集中度,
                "chip_health": "健康/一般/警惕"
            }
        },

        "intelligence": {
            "latest_news": "【最新消息】近期重要新聞摘要",
            "risk_alerts": ["風險點1：具體描述", "風險點2：具體描述"],
            "positive_catalysts": ["利好1：具體描述", "利好2：具體描述"],
            "earnings_outlook": "業績預期分析（基於年報預告、業績快報等）",
            "sentiment_summary": "輿情情緒一句話總結"
        },

        "battle_plan": {
            "sniper_points": {
                "ideal_buy": "理想入場位：XX元（滿足主要技能觸發條件）",
                "secondary_buy": "次優入場位：XX元（更保守或確認後執行）",
                "stop_loss": "止損位：XX元（失效條件或X%風險）",
                "take_profit": "目標位：XX元（按阻力位/風險回報比制定）"
            },
            "position_strategy": {
                "suggested_position": "建議倉位：X成",
                "entry_plan": "分批建倉策略描述",
                "risk_control": "風控策略描述"
            },
            "action_checklist": [
                "✅/⚠️/❌ 檢查項1：當前結構是否滿足激活技能條件",
                "✅/⚠️/❌ 檢查項2：入場位置與風險回報是否合理",
                "✅/⚠️/❌ 檢查項3：量價/波動/籌碼是否支持判斷",
                "✅/⚠️/❌ 檢查項4：無重大利空",
                "✅/⚠️/❌ 檢查項5：倉位與止損計劃明確",
                "✅/⚠️/❌ 檢查項6：估值/業績/催化與結論匹配"
            ]
        },

        "phase_decision": {
            "phase_context": {"phase": "premarket/intraday/lunch_break/closing_auction/postmarket/non_trading/unknown"},
            "action_window": "盤前計劃/盤中跟蹤/午間確認/收盤前風控/盤後復盤/非交易日觀察",
            "immediate_action": "立即行動/等待確認/觀察/止損止盈預警/禁止追高/無盤中動作",
            "watch_conditions": ["觀察條件1", "觀察條件2"],
            "next_check_time": "下一次檢查點或市場本地時間",
            "confidence_reason": "置信度理由，說明階段和數據質量限制",
            "data_limitations": ["階段或數據質量限制1", "階段或數據質量限制2"]
        },

        "signal_attribution": {
            "technical_indicators": 技術指標貢獻度(0-100),
            "news_sentiment": 新聞輿情貢獻度(0-100),
            "fundamentals": 基本面貢獻度(0-100),
            "market_conditions": 市場環境貢獻度(0-100),
            "strongest_bullish_signal": "最強看多信號名稱",
            "strongest_bearish_signal": "最強看空信號名稱"
        }
    },

    "analysis_summary": "100字綜合分析摘要",
    "key_points": "3-5個核心看點，逗號分隔",
    "risk_warning": "風險提示",
    "buy_reason": "操作理由，引用激活技能或風險框架",

    "trend_analysis": "走勢形態分析",
    "short_term_outlook": "短期1-3日展望",
    "medium_term_outlook": "中期1-2周展望",
    "technical_analysis": "技術面綜合分析",
    "ma_analysis": "均線系統分析",
    "volume_analysis": "量能分析",
    "pattern_analysis": "K線形態分析",
    "fundamental_analysis": "基本面分析",
    "sector_position": "板塊行業分析",
    "company_highlights": "公司亮點/風險",
    "news_summary": "新聞摘要",
    "market_sentiment": "市場情緒",
    "hot_topics": "相關熱點",

    "search_performed": true/false,
    "data_sources": "數據來源說明"
}
```

## 評分標準

### 強烈買入（80-100分）：
- ✅ 多個激活技能同時支持積極結論
- ✅ 上行空間、觸發條件與風險回報清晰
- ✅ 關鍵風險已排查，倉位與止損計劃明確
- ✅ 重要數據和情報結論彼此一致

### 買入（60-79分）：
- ✅ 主信號偏積極，但仍有少量待確認項
- ✅ 允許存在可控風險或次優入場點
- ✅ 需要在報告中明確補充觀察條件

### 觀望（40-59分）：
- ⚠️ 信號分歧較大，或缺乏足夠確認
- ⚠️ 風險與機會大致均衡
- ⚠️ 更適合等待觸發條件或迴避不確定性

### 減倉（20-39分）：
- ⚠️ 主要結論轉弱，風險明顯高於收益
- ⚠️ 觸發了部分失效條件，現有倉位需要降低暴露
- ⚠️ 更適合保護收益而不是進攻

### 賣出（0-19分）：
- ❌ 觸發了止損/失效條件或重大利空
- ❌ 趨勢或風險顯著惡化
- ❌ 現有倉位應優先退出

## 決策儀表盤核心原則

1. **核心結論先行**：一句話說清該買該賣
2. **分持倉建議**：空倉者和持倉者給不同建議
3. **精確狙擊點**：必須給出具體價格，不說模糊的話
4. **檢查清單可視化**：用 ✅⚠️❌ 明確顯示每項檢查結果
5. **風險優先級**：輿情中的風險點要醒目標出

## 可操作性與穩定性約束

- 不得僅因為單日漲跌或評分跨線就在“買入/賣出”之間劇烈切換。
- 操作建議必須同時參考價格位置（支撐/壓力位）、量能/籌碼、主力資金流向和風險事件。
- 股價位於支撐與壓力之間、資金流不明確時，優先輸出“持有/震盪/觀望/洗盤觀察”等可執行的中性建議；`decision_type` 仍保持 `hold`。
- 只有在接近支撐確認或有效突破壓力，且資金流/量價配合時，才能給出買入；接近壓力且資金流出時不得追買。
- 只有在跌破關鍵支撐、主力資金持續流出或風險顯著放大時，才能給出賣出/減倉。
- 必須輸出 `dashboard.phase_decision` 七字段；盤中/午休/臨近收盤要給出當前動作、觀察條件和下一次檢查點。
- 建議輸出可選展示字段 `dashboard.signal_attribution` 六字段；解釋推薦理由的構成，包括技術指標、新聞輿情、基本面、市場環境的貢獻度，以及最強看多/看空信號。
- 盤前、非交易日或未知階段不得偽造今日盤中走勢；quote/daily_bars/technical 存在 stale、fallback、missing、fetch_failed、partial 或 estimated 時，`confidence_level` 不得為高。"""

    TEXT_SYSTEM_PROMPT = """你是一位專業的股票分析助手。

- 回答必須基於用戶提供的數據與上下文
- 若信息不足，要明確指出不確定性
- 不要編造價格、財報或新聞事實
"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        config: Optional[Config] = None,
        skills: Optional[List[str]] = None,
        skill_instructions: Optional[str] = None,
        default_skill_policy: Optional[str] = None,
        use_legacy_default_prompt: Optional[bool] = None,
    ):
        """Initialize LLM Analyzer via LiteLLM.

        Args:
            api_key: Ignored (kept for backward compatibility). Keys are loaded from config.
        """
        self._config_override = config
        self._requested_skills = list(skills) if skills is not None else None
        self._skill_instructions_override = skill_instructions
        self._default_skill_policy_override = default_skill_policy
        self._use_legacy_default_prompt_override = use_legacy_default_prompt
        self._resolved_prompt_state: Optional[Dict[str, Any]] = None
        self._router = None
        self._legacy_router_model_list: List[Dict[str, Any]] = []
        self._litellm_available = False
        self._init_litellm()
        if not self._litellm_available:
            try:
                backend_id, _fallback_backend_id = self._resolve_generation_backend_config()
            except GenerationError:
                backend_id = ""
            if backend_id in LOCAL_CLI_GENERATION_BACKEND_IDS:
                logger.info(
                    "Analyzer generation backend: %s configured; LiteLLM API keys are not "
                    "required for stock analysis generation",
                    backend_id,
                )
            else:
                logger.warning("No LLM configured (LITELLM_MODEL / API keys), AI analysis will be unavailable")

    def _get_runtime_config(self) -> Config:
        """Return the runtime config, honoring injected overrides for tests/pipeline."""
        return getattr(self, "_config_override", None) or get_config()

    def _get_skill_prompt_sections(self) -> tuple[str, str, bool]:
        """Resolve skill instructions + default baseline + prompt mode."""
        skill_instructions = getattr(self, "_skill_instructions_override", None)
        default_skill_policy = getattr(self, "_default_skill_policy_override", None)
        use_legacy_default_prompt = getattr(self, "_use_legacy_default_prompt_override", None)

        if skill_instructions is not None and default_skill_policy is not None:
            return (
                skill_instructions,
                default_skill_policy,
                bool(use_legacy_default_prompt) if use_legacy_default_prompt is not None else False,
            )

        resolved_state = getattr(self, "_resolved_prompt_state", None)
        if resolved_state is None:
            from src.agent.factory import resolve_skill_prompt_state

            prompt_state = resolve_skill_prompt_state(
                self._get_runtime_config(),
                skills=getattr(self, "_requested_skills", None),
            )
            resolved_state = {
                "skill_instructions": prompt_state.skill_instructions,
                "default_skill_policy": prompt_state.default_skill_policy,
                "use_legacy_default_prompt": bool(getattr(prompt_state, "use_legacy_default_prompt", False)),
            }
            self._resolved_prompt_state = resolved_state

        return (
            skill_instructions if skill_instructions is not None else resolved_state.get("skill_instructions", ""),
            default_skill_policy if default_skill_policy is not None else resolved_state.get("default_skill_policy", ""),
            (
                use_legacy_default_prompt
                if use_legacy_default_prompt is not None
                else bool(resolved_state.get("use_legacy_default_prompt", False))
            ),
        )

    def _get_analysis_system_prompt(self, report_language: str, stock_code: str = "") -> str:
        """Build the analyzer system prompt with output-language guidance."""
        lang = normalize_report_language(report_language)
        market_role = get_market_role(stock_code, lang)
        market_guidelines = get_market_guidelines(stock_code, lang)
        skill_instructions, default_skill_policy, use_legacy_default_prompt = self._get_skill_prompt_sections()
        if use_legacy_default_prompt:
            base_prompt = self.LEGACY_DEFAULT_SYSTEM_PROMPT.replace(
                "{market_placeholder}", market_role
            ).replace(
                "{guidelines_placeholder}", market_guidelines
            )
        else:
            skills_section = ""
            if skill_instructions:
                skills_section = f"## 激活的交易技能\n\n{skill_instructions}\n"
            default_skill_policy_section = ""
            if default_skill_policy:
                default_skill_policy_section = f"{default_skill_policy}\n"
            base_prompt = (
                self.SYSTEM_PROMPT.replace("{market_placeholder}", market_role)
                .replace("{guidelines_placeholder}", market_guidelines)
                .replace("{default_skill_policy_section}", default_skill_policy_section)
                .replace("{skills_section}", skills_section)
            )
        if lang == "en":
            return base_prompt + """

## Output Language (highest priority)

- Keep all JSON keys unchanged.
- `decision_type` must remain `buy|hold|sell`.
- All human-readable JSON values must be written in English.
- Use the common English company name when you are confident; otherwise keep the original listed company name instead of inventing one.
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, nested dashboard text, checklist items, and all narrative summaries.
"""
        if lang == "ko":
            return base_prompt + """

## Output Language (highest priority)

- Keep all JSON keys unchanged.
- `decision_type` must remain `buy|hold|sell`.
- All human-readable JSON values must be written in Korean (한국어).
- Use the common Korean or original listed company name when confident; do not invent one.
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, nested dashboard text, checklist items, and all narrative summaries.
"""
        return base_prompt + """

## 輸出語言（最高優先級）

- 所有 JSON 鍵名保持不變。
- `decision_type` 必須保持為 `buy|hold|sell`。
- 所有面向用戶的人類可讀文本值必須使用中文。
"""

    def _has_channel_config(self, config: Config) -> bool:
        """Check if multi-channel config (channels / YAML / legacy model_list) is active."""
        return bool(config.llm_model_list) and not all(
            e.get('model_name', '').startswith('__legacy_') for e in config.llm_model_list
        )

    @staticmethod
    def _legacy_router_provider_alias(model: str) -> str:
        provider = model.split("/", 1)[0] if "/" in model else "openai"
        return f"__legacy_{provider}__"

    @staticmethod
    def _build_legacy_router_model_list_from_config(
        model: str,
        model_list: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Build legacy-router candidates from configured legacy llm_model_list entries."""
        if not model:
            return []
        target_model = model
        target_legacy_alias = GeminiAnalyzer._legacy_router_provider_alias(model)
        legacy_entries: List[Dict[str, Any]] = []
        for entry in model_list or []:
            if not isinstance(entry, dict):
                continue
            model_name = str(entry.get("model_name") or "").strip()
            if model_name != target_legacy_alias:
                continue

            params = entry.get("litellm_params")
            if not isinstance(params, dict):
                continue

            api_key = str(params.get("api_key") or "").strip()
            if not api_key or len(api_key) < 8:
                continue

            deployed_params = dict(params)
            deployed_params["model"] = target_model
            deployed_params["api_key"] = api_key
            legacy_entries.append({
                "model_name": target_model,
                "litellm_params": deployed_params,
            })

        return legacy_entries

    def _init_litellm(self) -> None:
        """Initialize litellm Router from channels / YAML / legacy keys."""
        config = self._get_runtime_config()
        if self._get_hermes_config_error(config) is not None:
            logger.error("Analyzer LLM: Hermes channel configuration blocks legacy fallback")
            return
        litellm_model = config.litellm_model
        if not litellm_model:
            backend_id = ""
            try:
                backend_id = resolve_generation_backend_id(config)
            except GenerationError:
                pass
            if backend_id in LOCAL_CLI_GENERATION_BACKEND_IDS:
                logger.info(
                    "Analyzer LiteLLM: LITELLM_MODEL not configured; using %s generation backend",
                    backend_id,
                )
            else:
                logger.warning("Analyzer LLM: LITELLM_MODEL not configured")
            return

        self._litellm_available = True

        # --- Channel / YAML path: build Router from pre-built model_list ---
        if self._has_channel_config(config):
            model_list = config.llm_model_list
            if self._get_mixed_hermes_route_error(config, litellm_model) is not None:
                self._litellm_available = False
                logger.error("Analyzer LLM: mixed Hermes/non-Hermes route requires deployment-level no-proxy support")
                return
            router_model_list = model_list
            if route_has_hermes(model_list, litellm_model):
                # Hermes-only routes are dispatched directly with a request-scoped
                # no-proxy OpenAI client. Keeping them out of Router prevents the
                # default proxy-aware transport from seeing the Hermes bearer key.
                router_model_list = filter_non_hermes_deployments(model_list)
                if not router_model_list:
                    self._litellm_available = True
                    logger.info("Analyzer LLM: Hermes-only route will use direct no-proxy completion")
                    return
            try:
                self._router = Router(
                    model_list=router_model_list,
                    routing_strategy="simple-shuffle",
                    num_retries=2,
                )
            except TypeError:
                logger.debug("Analyzer LLM: Router constructor signature not compatible; fallback to direct mode")
                self._router = None
            else:
                unique_models = list(dict.fromkeys(
                    e['litellm_params']['model'] for e in model_list
                ))
                logger.info(
                    f"Analyzer LLM: Router initialized from channels/YAML — "
                    f"{len(router_model_list)} deployment(s), models: {unique_models}"
                )
                return

        # --- Legacy path: build Router for multi-key, or use single key ---
        keys = get_api_keys_for_model(litellm_model, config)
        legacy_model_list = self._build_legacy_router_model_list_from_config(
            litellm_model,
            config.llm_model_list,
        )
        if len(legacy_model_list) <= 1 and keys:
            extra_params = extra_litellm_params(litellm_model, config)
            configured_model_list = [
                {
                    "model_name": litellm_model,
                    "litellm_params": {
                        "model": litellm_model,
                        "api_key": k,
                        **extra_params,
                    },
                }
                for k in keys
            ]
            if not legacy_model_list:
                legacy_model_list = configured_model_list
            elif len(legacy_model_list) < len(configured_model_list):
                legacy_model_list = configured_model_list

        if len(legacy_model_list) > 1:
            self._legacy_router_model_list = legacy_model_list
            try:
                self._router = Router(
                    model_list=legacy_model_list,
                    routing_strategy="simple-shuffle",
                    num_retries=2,
                )
            except TypeError:
                logger.debug("Analyzer LLM: Legacy Router constructor signature not compatible; using legacy model_list fallback")
                self._router = None
            else:
                logger.info(
                    f"Analyzer LLM: Legacy Router initialized with {len(legacy_model_list)} keys "
                    f"for {litellm_model}"
                )
                return

        if keys:
            logger.info(f"Analyzer LLM: litellm initialized (model={litellm_model})")
        else:
            logger.info(
                f"Analyzer LLM: litellm initialized (model={litellm_model}, "
                f"API key from environment)"
            )

    def is_available(self) -> bool:
        """Check whether the configured generation backend is available."""
        backend_error = self.get_generation_backend_config_error()
        if backend_error is not None:
            return self._can_use_generation_fallback(backend_error)
        backend_id, _fallback_backend_id = self._resolve_generation_backend_config()
        if backend_id in LOCAL_CLI_GENERATION_BACKEND_IDS:
            return True
        return self._litellm_runtime_available()

    def _litellm_runtime_available(self) -> bool:
        return self._router is not None or self._litellm_available

    def _can_use_generation_fallback(self, backend_error: GenerationError) -> bool:
        if not backend_error.fallbackable:
            return False
        try:
            _backend_id, fallback_backend_id = self._resolve_generation_backend_config()
        except GenerationError:
            return False
        return (
            fallback_backend_id == LITELLM_BACKEND_ID
            and self._litellm_runtime_available()
        )

    def _resolve_generation_backend_config(self) -> Tuple[str, Optional[str]]:
        """Resolve and validate generation backend ids."""
        config = self._get_runtime_config()
        backend_id = resolve_generation_backend_id(config)
        fallback_backend_id = resolve_generation_fallback_backend_id(config)
        return backend_id, fallback_backend_id

    def get_generation_backend_config_error(self) -> Optional[GenerationError]:
        """Return a structured backend config error, if the backend cannot run."""
        try:
            backend_id, _fallback_backend_id = self._resolve_generation_backend_config()
            config = self._get_runtime_config()
            hermes_error = self._get_hermes_config_error(config)
            if hermes_error is not None:
                return hermes_error
            for model in [getattr(config, "litellm_model", "")] + list(getattr(config, "litellm_fallback_models", []) or []):
                mixed_error = self._get_mixed_hermes_route_error(config, model)
                if mixed_error is not None:
                    return mixed_error
            if backend_id in LOCAL_CLI_GENERATION_BACKEND_IDS:
                backend = self._get_generation_backend(backend_id)
                get_config_error = getattr(backend, "get_config_error", None)
                if callable(get_config_error):
                    return get_config_error()
        except GenerationError as exc:
            return exc
        return None

    def _get_hermes_config_error(self, config: Config) -> Optional[GenerationError]:
        issues = list(getattr(config, "llm_channel_config_issues", []) or [])
        if not getattr(config, "llm_blocks_legacy_fallback", False) or not issues:
            return None
        blocked_routes = set(getattr(config, "llm_blocked_hermes_routes", []) or [])
        selected_models = [
            ("LITELLM_MODEL", getattr(config, "litellm_model", "") or ""),
            *[
                ("LITELLM_FALLBACK_MODELS", fallback_model)
                for fallback_model in list(getattr(config, "litellm_fallback_models", []) or [])
            ],
        ]
        selected_blocked_route = ""
        selected_field = ""
        for field_name, model in selected_models:
            raw_model = str(model or "").strip()
            if not raw_model:
                continue
            candidates = hermes_blocked_route_candidates(raw_model)
            candidates.add(raw_model)
            try:
                candidates.add(canonicalize_hermes_model_ref(raw_model).route_model)
            except (TypeError, ValueError) as exc:
                logger.debug("Failed to canonicalize selected Hermes route candidate %r: %s", raw_model, exc)
            matched = candidates & blocked_routes
            if matched:
                selected_blocked_route = sorted(matched)[0]
                selected_field = field_name
                break
        if blocked_routes and not selected_blocked_route and getattr(config, "llm_model_list", None):
            return None
        first = issues[0]
        code = (
            "explicit_hermes_route_invalid"
            if selected_blocked_route
            else first.get("code", "invalid_hermes_channel")
        )
        return GenerationError(
            error_code=GenerationErrorCode.UNSAFE_CONFIG,
            stage="configuration",
            retryable=False,
            fallbackable=False,
            backend=LITELLM_BACKEND_ID,
            provider=HERMES_CHANNEL_NAME,
            details={
                "field": selected_field or first.get("field", "LLM_HERMES_API_KEY"),
                "code": code,
                "reason": code,
                "message": first.get("message", "Hermes channel configuration is invalid"),
                "issues": issues,
                "route_name": selected_blocked_route or None,
            },
        )

    def _get_mixed_hermes_route_error(self, config: Config, model: str) -> Optional[GenerationError]:
        if not model:
            return None
        origins = route_deployment_origins(getattr(config, "llm_model_list", []) or [], model)
        if not origins.is_mixed:
            return None
        return GenerationError(
            error_code=GenerationErrorCode.UNSAFE_CONFIG,
            stage="configuration",
            retryable=False,
            fallbackable=False,
            backend=LITELLM_BACKEND_ID,
            provider=HERMES_CHANNEL_NAME,
            details={
                "field": "LLM_CHANNELS",
                "code": "mixed_hermes_route_unsupported",
                "reason": "router_deployment_no_proxy_unavailable",
                "route_name": model,
            },
        )

    def _hermes_redaction_values_for_model(self, config: Config, model: str = "") -> set[str]:
        redactions: set[str] = set()
        deployments = list(getattr(config, "llm_model_list", []) or [])
        selected_deployments = deployments
        if model:
            origins = route_deployment_origins(deployments, model)
            selected_deployments = list(origins.hermes_deployments or [])
            if not selected_deployments and not origins.has_hermes:
                return redactions
        for deployment in selected_deployments:
            if not isinstance(deployment, dict):
                continue
            if not route_has_hermes([deployment], str(deployment.get("model_name") or "")):
                continue
            params = deployment.get("litellm_params") or {}
            if isinstance(params, dict):
                redactions.update(build_hermes_redaction_values(params.get("api_key")))
        return redactions

    def _sanitize_hermes_exception_text(
        self,
        exc: Any,
        *,
        config: Optional[Config] = None,
        model: str = "",
    ) -> str:
        runtime_config = config or self._get_runtime_config()
        redactions = self._hermes_redaction_values_for_model(runtime_config, model)
        if not redactions:
            return str(exc)
        return sanitize_hermes_error_text(exc, redaction_values=redactions)

    def _litellm_redaction_values_for_model(self, config: Config, model: str = "") -> set[str]:
        redactions = self._hermes_redaction_values_for_model(config, model)
        try:
            redactions.update(build_hermes_redaction_values(*get_api_keys_for_model(model, config)))
        except Exception:
            pass
        origins = route_deployment_origins(getattr(config, "llm_model_list", []) or [], model)
        for deployment in (*origins.hermes_deployments, *origins.non_hermes_deployments):
            params = deployment.get("litellm_params") if isinstance(deployment, dict) else None
            if isinstance(params, dict):
                redactions.update(build_hermes_redaction_values(params.get("api_key")))
        return redactions

    def _sanitize_litellm_exception_text(
        self,
        exc: Any,
        *,
        config: Optional[Config] = None,
        model: str = "",
    ) -> str:
        runtime_config = config or self._get_runtime_config()
        redactions = self._litellm_redaction_values_for_model(runtime_config, model)
        sanitized = sanitize_hermes_error_text(exc, redaction_values=redactions)
        return redact_diagnostic_text(sanitized, limit=500)

    def _dispatch_litellm_completion(
        self,
        model: str,
        call_kwargs: Dict[str, Any],
        *,
        config: Config,
        use_channel_router: bool,
        router_model_names: set[str],
    ) -> Any:
        """Dispatch a LiteLLM completion through router or direct fallback."""
        origins = route_deployment_origins(config.llm_model_list, model)
        if origins.is_mixed:
            raise RuntimeError("Hermes/non-Hermes mixed generation route is not supported without deployment-level no-proxy client support")
        if origins.is_hermes_only:
            deployment = origins.hermes_deployments[0]
            params = dict(deployment.get("litellm_params") or {})
            api_key = str(params.get("api_key") or "").strip()
            base_url = str(params.get("api_base") or "").strip()
            if is_masked_secret_placeholder(api_key):
                raise RuntimeError("Hermes API key is a masked placeholder and cannot be used for generation")
            timeout = float(call_kwargs.get("timeout") or 30.0)
            hermes_kwargs = dict(call_kwargs)
            hermes_kwargs["model"] = str(params.get("model") or model)
            hermes_kwargs["stream"] = False
            hermes_kwargs.pop("api_key", None)
            hermes_kwargs.pop("api_base", None)
            with open_hermes_no_proxy_client(api_key=api_key, base_url=base_url, timeout=timeout) as client:
                hermes_kwargs["client"] = client
                return litellm.completion(**hermes_kwargs)

        wire_models = resolve_fallback_litellm_wire_models(model, config.llm_model_list)
        register_fallback_model_pricing(wire_models)
        effective_kwargs = dict(call_kwargs)
        if use_channel_router and self._router and model in router_model_names:
            return self._router.completion(**effective_kwargs)
        if self._router and model == config.litellm_model and not use_channel_router:
            return self._router.completion(**effective_kwargs)

        keys = get_api_keys_for_model(model, config)
        if keys:
            effective_kwargs["api_key"] = keys[0]
        effective_kwargs.update(extra_litellm_params(model, config))
        return litellm.completion(**effective_kwargs)

    def _normalize_usage(
        self,
        usage_obj: Any,
        *,
        model: str = "",
        provider: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Normalize usage objects from LiteLLM responses/chunks."""
        if not usage_obj:
            usage = attach_message_hmacs({}, messages) if messages is not None else {}
            return filter_prompt_cache_telemetry(usage, self._get_runtime_config())
        usage = normalize_litellm_usage(usage_obj, model=model, provider=provider)
        if messages is not None:
            usage = attach_message_hmacs(usage, messages)
        return filter_prompt_cache_telemetry(usage, self._get_runtime_config())

    @staticmethod
    def _get_response_field(obj: Any, key: str) -> Any:
        """Read a field from dict-like or object-like LiteLLM payloads."""
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    def _extract_text_blocks(self, blocks: Any) -> str:
        """Extract text from OpenAI-compatible content block lists."""
        if not blocks:
            return ""

        parts: List[str] = []
        for block in blocks:
            if isinstance(block, str):
                parts.append(block)
                continue

            text = None
            if isinstance(block, dict):
                text = block.get("text")
                if text is None:
                    text = block.get("content")
            else:
                text = getattr(block, "text", None)
                if text is None:
                    text = getattr(block, "content", None)

            if isinstance(text, str) and text:
                parts.append(text)

        return "".join(parts).strip()

    def _extract_completion_text(self, response: Any) -> str:
        """Extract text from non-stream LiteLLM completion responses."""
        choices = self._get_response_field(response, "choices")
        if not choices:
            return ""

        choice = choices[0]
        message = self._get_response_field(choice, "message")

        content_blocks = self._get_response_field(choice, "content_blocks")
        if content_blocks is None and message is not None:
            content_blocks = self._get_response_field(message, "content_blocks")
        block_text = self._extract_text_blocks(content_blocks)
        if block_text:
            return block_text

        content = None
        if message is not None:
            content = self._get_response_field(message, "content")
        if content is None:
            content = self._get_response_field(choice, "content")

        if isinstance(content, list):
            return self._extract_text_blocks(content)
        if isinstance(content, str):
            return content.strip()
        return str(content).strip() if content is not None else ""

    def _extract_stream_text(self, chunk: Any) -> str:
        """Extract provider-agnostic text delta from a LiteLLM streaming chunk."""
        choices = chunk.get("choices") if isinstance(chunk, dict) else getattr(chunk, "choices", None)
        if not choices:
            return ""

        choice = choices[0]
        delta = choice.get("delta") if isinstance(choice, dict) else getattr(choice, "delta", None)
        message = choice.get("message") if isinstance(choice, dict) else getattr(choice, "message", None)

        content: Any = None
        if isinstance(delta, dict):
            content = delta.get("content")
        elif isinstance(delta, str):
            content = delta
        elif delta is not None:
            content = getattr(delta, "content", None)

        if content is None:
            if isinstance(message, dict):
                content = message.get("content")
            elif message is not None:
                content = getattr(message, "content", None)

        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)

        return content if isinstance(content, str) else ""

    def _consume_litellm_stream(
        self,
        stream_response: Any,
        *,
        model: str,
        usage_model: Optional[str] = None,
        provider: Optional[str] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Consume a LiteLLM stream into a single text payload."""
        chunks: List[str] = []
        usage: Dict[str, Any] = {}
        chars_received = 0
        next_emit_at = 1

        try:
            for chunk in stream_response:
                chunk_usage = extract_usage_payload(chunk)
                normalized_usage = self._normalize_usage(
                    chunk_usage,
                    model=usage_model or model,
                    provider=provider,
                )
                if normalized_usage:
                    usage = normalized_usage

                delta_text = self._extract_stream_text(chunk)
                if not delta_text:
                    continue

                chunks.append(delta_text)
                chars_received += len(delta_text)
                if progress_callback and chars_received >= next_emit_at:
                    progress_callback(chars_received)
                    next_emit_at = chars_received + 160
        except Exception as exc:
            raise _LiteLLMStreamError(
                f"{model} stream interrupted: {exc}",
                partial_received=chars_received > 0,
            ) from exc

        response_text = "".join(chunks).strip()
        if not response_text:
            raise _LiteLLMStreamError(
                f"{model} stream returned empty response",
                partial_received=False,
            )

        if progress_callback and chars_received > 0:
            progress_callback(chars_received)

        return response_text, usage

    def _get_generation_backend(self, backend_id: Optional[str] = None) -> GenerationBackend:
        """Return the configured generation backend."""
        config = self._get_runtime_config()
        resolved_backend_id = backend_id or self._resolve_generation_backend_config()[0]
        return create_generation_backend(
            resolved_backend_id,
            config=config,
            litellm_completion_callable=self._call_litellm_impl,
        )

    def _call_litellm(
        self,
        prompt: str,
        generation_config: dict,
        *,
        system_prompt: Optional[str] = None,
        stream: bool = False,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
        response_validator: Optional[Callable[[str], None]] = None,
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """Compatibility wrapper around the configured generation backend."""
        preflight_error = self.get_generation_backend_config_error()
        if preflight_error is not None and not self._can_use_generation_fallback(preflight_error):
            raise preflight_error
        backend_id, fallback_backend_id = self._resolve_generation_backend_config()
        try:
            result = self._get_generation_backend(backend_id).generate(
                prompt,
                generation_config,
                system_prompt=system_prompt,
                stream=stream,
                stream_progress_callback=stream_progress_callback,
                response_validator=response_validator,
                audit_context=audit_context,
            )
        except GenerationError as exc:
            if not exc.fallbackable or not fallback_backend_id:
                raise
            try:
                fallback_backend = self._get_generation_backend(fallback_backend_id)
            except GenerationError as fallback_exc:
                raise GenerationError(
                    error_code=fallback_exc.error_code,
                    stage="fallback",
                    retryable=False,
                    fallbackable=False,
                    backend=fallback_backend_id,
                    provider=fallback_exc.provider,
                    details={
                        "primary_error": {
                            "error_code": exc.error_code.value,
                            "backend": exc.backend,
                            "provider": exc.provider,
                            "stage": exc.stage,
                            "details": exc.details,
                        },
                        "fallback_error": fallback_exc.details,
                    },
                ) from fallback_exc
            try:
                result = fallback_backend.generate(
                    prompt,
                    generation_config,
                    system_prompt=system_prompt,
                    stream=stream,
                    stream_progress_callback=stream_progress_callback,
                    response_validator=response_validator,
                    audit_context=audit_context,
                )
            except _AllModelsFailedError:
                raise
            except GenerationError as fallback_exc:
                raise GenerationError(
                    error_code=fallback_exc.error_code,
                    stage="fallback",
                    retryable=False,
                    fallbackable=False,
                    backend=fallback_backend_id,
                    provider=fallback_exc.provider,
                    details={
                        "reason": "fallback_backend_failed",
                        "primary_error": {
                            "error_code": exc.error_code.value,
                            "backend": exc.backend,
                            "provider": exc.provider,
                            "stage": exc.stage,
                            "details": exc.details,
                        },
                        "fallback_error": {
                            "error_code": fallback_exc.error_code.value,
                            "backend": fallback_exc.backend,
                            "provider": fallback_exc.provider,
                            "stage": fallback_exc.stage,
                            "details": fallback_exc.details,
                        },
                    },
                ) from fallback_exc
            except Exception as fallback_exc:
                raise GenerationError(
                    error_code=GenerationErrorCode.UNKNOWN_BACKEND_ERROR,
                    stage="fallback",
                    retryable=False,
                    fallbackable=False,
                    backend=fallback_backend_id,
                    provider=fallback_backend_id,
                    details={
                        "reason": "fallback_backend_failed",
                        "primary_error": {
                            "error_code": exc.error_code.value,
                            "backend": exc.backend,
                            "provider": exc.provider,
                            "stage": exc.stage,
                            "details": exc.details,
                        },
                        "fallback_error": str(fallback_exc),
                    },
                ) from fallback_exc
        return result.text, result.model, result.usage

    def _call_litellm_impl(
        self,
        prompt: str,
        generation_config: dict,
        *,
        system_prompt: Optional[str] = None,
        stream: bool = False,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
        response_validator: Optional[Callable[[str], None]] = None,
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """Call LLM via litellm with fallback across configured models.

        When channels/YAML are configured, every model goes through the Router
        (which handles per-model key selection, load balancing, and retries).
        In legacy mode, the primary model may use the Router while fallback
        models fall back to direct litellm.completion().

        Args:
            prompt: User prompt text.
            generation_config: Dict with optional keys: temperature, max_output_tokens, max_tokens.
            response_validator: Optional callable that accepts the raw response text and raises
                an exception if the response is unacceptable (e.g. not valid JSON).  When it
                raises, the current model is treated as failed and the next fallback model is
                tried.  If all models fail validation, :class:`_AllModelsFailedError` is raised
                with ``last_response_text`` set to the last raw response received.

        Returns:
            Tuple of (response text, model_used, usage). On success model_used is the full model
            name and usage is a dict with prompt_tokens, completion_tokens, total_tokens.
        """
        config = self._get_runtime_config()
        max_tokens = (
            generation_config.get('max_output_tokens')
            or generation_config.get('max_tokens')
            or 8192
        )
        requested_temperature = generation_config.get('temperature', 0.7)
        requested_timeout = generation_config.get("timeout")

        models_to_try = [config.litellm_model] + (config.litellm_fallback_models or [])
        models_to_try = [m for m in models_to_try if m]

        use_channel_router = self._has_channel_config(config)

        last_error = None
        last_response_text: Optional[str] = None
        last_model: Optional[str] = None
        last_usage: Dict[str, Any] = {}
        effective_system_prompt = system_prompt or self.TEXT_SYSTEM_PROMPT
        router_model_names = set(get_configured_llm_models(config.llm_model_list))
        for model in models_to_try:
            origins = route_deployment_origins(config.llm_model_list, model)
            model_stream = bool(stream and not origins.has_hermes)
            recovery_model_list = config.llm_model_list
            legacy_router_model_list = getattr(self, "_legacy_router_model_list", None) or []
            if legacy_router_model_list and model == config.litellm_model and not use_channel_router:
                recovery_model_list = legacy_router_model_list
            usage_model, usage_provider = resolved_model_provider_identity(model, recovery_model_list)

            try:
                def _attach_usage_audit(
                    usage: Dict[str, Any],
                    messages: List[Dict[str, Any]],
                ) -> Dict[str, Any]:
                    if audit_context is None:
                        return filter_prompt_cache_telemetry(
                            attach_message_hmacs(usage, messages),
                            config,
                        )
                    effective_audit_context = dict(audit_context)
                    effective_audit_context["provider"] = usage_provider
                    effective_audit_context["transport"] = (
                        effective_audit_context.get("transport") or "litellm"
                    )
                    return filter_prompt_cache_telemetry(
                        attach_legacy_message_stability_audit(
                            usage,
                            messages,
                            effective_audit_context,
                        ),
                        config,
                    )

                model_short = model.split("/")[-1] if "/" in model else model
                extra = get_thinking_extra_body(model_short)
                call_kwargs: Dict[str, Any] = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": effective_system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": max_tokens,
                }
                if requested_timeout not in (None, ""):
                    call_kwargs["timeout"] = requested_timeout
                if extra:
                    call_kwargs["extra_body"] = extra
                uses_router = (
                    (use_channel_router and self._router and model in router_model_names)
                    or (self._router and model == config.litellm_model and not use_channel_router)
                )
                if not uses_router:
                    try:
                        keys = get_api_keys_for_model(model, config)
                    except AttributeError:
                        keys = []
                    if keys:
                        call_kwargs["api_key"] = keys[0]
                    try:
                        call_kwargs.update(extra_litellm_params(model, config))
                    except AttributeError:
                        pass
                call_kwargs = apply_litellm_generation_params(
                    call_kwargs,
                    model,
                    requested_temperature,
                    model_list=recovery_model_list,
                )
                route_context = build_provider_cache_route_context(
                    model=model,
                    provider=usage_provider,
                    call_kwargs=call_kwargs,
                    model_list=recovery_model_list,
                    call_type="analysis",
                )
                hint_result = apply_prompt_cache_hints(call_kwargs, route_context, config)
                call_kwargs = hint_result.call_kwargs
                if requested_timeout not in (None, ""):
                    call_kwargs["timeout"] = requested_timeout
                if hint_result.diagnostics:
                    logger.debug("[PromptCache] %s", hint_result.diagnostics)

                _stream_text: Optional[str] = None
                _stream_usage: Dict[str, Any] = {}

                if model_stream:
                    try:
                        stream_response = call_litellm_with_param_recovery(
                            lambda kwargs: self._dispatch_litellm_completion(
                                model,
                                kwargs,
                                config=config,
                                use_channel_router=use_channel_router,
                                router_model_names=router_model_names,
                            ),
                            model=model,
                            call_kwargs={**call_kwargs, "stream": True},
                            model_list=recovery_model_list,
                            cache_recovery=False,
                            logger=logger,
                        )
                        _stream_text, _stream_usage = self._consume_litellm_stream(
                            stream_response,
                            model=model,
                            usage_model=usage_model,
                            provider=usage_provider,
                            progress_callback=stream_progress_callback,
                        )
                    except _LiteLLMStreamError as exc:
                        safe_error = self._sanitize_litellm_exception_text(exc, config=config, model=model)
                        if exc.partial_received:
                            logger.warning(
                                "[LiteLLM] %s stream failed after partial output, retrying non-stream for same model: %s",
                                model,
                                safe_error,
                            )
                        else:
                            logger.warning(
                                "[LiteLLM] %s stream unavailable before first chunk, falling back to non-stream: %s",
                                model,
                                safe_error,
                            )
                        last_error = RuntimeError(f"{type(exc).__name__}: {safe_error}")
                    except Exception as exc:
                        safe_error = self._sanitize_litellm_exception_text(exc, config=config, model=model)
                        logger.warning(
                            "[LiteLLM] %s stream request failed before first chunk, falling back to non-stream: %s",
                            model,
                            safe_error,
                        )

                if _stream_text is not None:
                    last_response_text = _stream_text
                    last_model = model
                    _stream_usage = _attach_usage_audit(_stream_usage, call_kwargs["messages"])
                    last_usage = _stream_usage
                    if response_validator is not None:
                        response_validator(_stream_text)
                    return _stream_text, model, _stream_usage

                response = call_litellm_with_param_recovery(
                    lambda kwargs: self._dispatch_litellm_completion(
                        model,
                        kwargs,
                        config=config,
                        use_channel_router=use_channel_router,
                        router_model_names=router_model_names,
                    ),
                    model=model,
                    call_kwargs=call_kwargs,
                    model_list=recovery_model_list,
                    logger=logger,
                )

                content = self._extract_completion_text(response)
                if content:
                    usage_messages = None if audit_context is not None else call_kwargs["messages"]
                    usage = self._normalize_usage(
                        extract_usage_payload(response),
                        model=usage_model or model,
                        provider=usage_provider,
                        messages=usage_messages,
                    )
                    if audit_context is not None:
                        usage = _attach_usage_audit(usage, call_kwargs["messages"])
                    last_response_text = content
                    last_model = model
                    last_usage = usage
                    if response_validator is not None:
                        response_validator(content)
                    return (content, model, usage)
                raise ValueError("LLM returned empty response")

            except Exception as e:
                safe_error = self._sanitize_litellm_exception_text(e, config=config, model=model)
                logger.warning("[LiteLLM] %s failed: %s", model, safe_error)
                last_error = RuntimeError(f"{type(e).__name__}: {safe_error}")
                continue

        raise _AllModelsFailedError(
            f"All LLM models failed (tried {len(models_to_try)} model(s)). Last error: {last_error}",
            last_response_text=last_response_text,
            last_model=last_model,
            last_usage=last_usage,
        )

    def generate_text(
        self,
        prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> Optional[str]:
        """Public entry point for free-form text generation.

        External callers (e.g. MarketAnalyzer) must use this method instead of
        calling _call_litellm() directly or accessing private attributes such as
        _litellm_available, _router, _model, _use_openai, or _use_anthropic.

        Args:
            prompt:      Text prompt to send to the LLM.
            max_tokens:  Maximum tokens in the response (default 2048).
            temperature: Sampling temperature (default 0.7).

        Returns:
            Response text, or None if the LLM call fails (error is logged).
        """
        try:
            result = self._call_litellm(
                prompt,
                generation_config={"max_tokens": max_tokens, "temperature": temperature},
            )
            if isinstance(result, tuple):
                text, model_used, usage = result
                if should_persist_usage_telemetry(usage):
                    persist_llm_usage(usage, model_used, call_type="market_review")
                return text
            return result
        except GenerationError:
            raise
        except Exception as exc:
            logger.error("[generate_text] LLM call failed: %s", exc)
            return None

    def analyze(
        self, 
        context: Dict[str, Any],
        news_context: Optional[str] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
        analysis_context_pack_summary: Optional[str] = None,
    ) -> AnalysisResult:
        """
        分析單隻股票
        
        流程：
        1. 格式化輸入數據（技術面 + 新聞）
        2. 調用 Gemini API（帶重試和模型切換）
        3. 解析 JSON 響應
        4. 返回結構化結果
        
        Args:
            context: 從 storage.get_analysis_context() 獲取的上下文數據
            news_context: 預先搜索的新聞內容（可選）

        Returns:
            AnalysisResult 對象
        """
        def _emit_progress(progress: int, message: str) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(progress, message)
            except Exception as exc:
                logger.debug("[analyzer] progress callback skipped: %s", exc)

        code = context.get('code', 'Unknown')
        config = self._get_runtime_config()
        report_language = normalize_report_language(getattr(config, "report_language", "zh"))
        system_prompt = self._get_analysis_system_prompt(report_language, stock_code=code)
        skill_instructions, default_skill_policy, use_legacy_default_prompt = self._get_skill_prompt_sections()
        
        # 請求前增加延時（防止連續請求觸發限流）
        request_delay = config.gemini_request_delay
        if request_delay > 0:
            logger.debug(f"[LLM] 請求前等待 {request_delay:.1f} 秒...")
            _emit_progress(65, f"{code}：LLM 請求前等待 {request_delay:.1f} 秒")
            time.sleep(request_delay)
        
        # 優先從上下文獲取股票名稱（由 main.py 傳入）
        name = context.get('stock_name')
        if not name or name.startswith('股票'):
            # 備選：從 realtime 中獲取
            if 'realtime' in context and context['realtime'].get('name'):
                name = context['realtime']['name']
            else:
                # 最後從映射表獲取
                name = STOCK_NAME_MAP.get(code, f'股票{code}')

        backend_error = self.get_generation_backend_config_error()
        if backend_error is not None and not self._can_use_generation_fallback(backend_error):
            details = backend_error.details or {}
            field = str(details.get("field") or "GENERATION_BACKEND")
            requested_backend = str(details.get("requested_backend") or backend_error.backend)
            reason = str(details.get("reason") or backend_error.error_code.value)
            if report_language == "en":
                summary = (
                    "AI analysis is unavailable because the generation backend "
                    f"cannot start: {backend_error.error_code.value}."
                )
                risk_warning = (
                    f"Check {field}={requested_backend} ({reason}) or set a valid "
                    "backend/fallback before retrying."
                )
            elif report_language == "ko":
                summary = (
                    "생성 백엔드를 시작할 수 없어 AI 분석을 사용할 수 없습니다: "
                    f"{backend_error.error_code.value}."
                )
                risk_warning = (
                    f"{field}={requested_backend} ({reason})를 확인하거나 유효한 "
                    "백엔드/폴백을 설정한 뒤 다시 시도하세요."
                )
            else:
                summary = (
                    "AI 分析功能不可用：生成後端無法啟動，"
                    f"{backend_error.error_code.value}。"
                )
                risk_warning = (
                    f"請檢查 {field}={requested_backend}（{reason}），"
                    "或配置有效後端/回退後重試。"
                )
            return AnalysisResult(
                code=code,
                name=name,
                sentiment_score=50,
                trend_prediction=localize_trend_prediction('震盪', report_language),
                operation_advice=localize_operation_advice('持有', report_language),
                confidence_level=localize_confidence_level('低', report_language),
                analysis_summary=summary,
                risk_warning=risk_warning,
                success=False,
                error_message=(
                    f"{backend_error.error_code.value}: {field}={requested_backend}"
                ),
                model_used=None,
                report_language=report_language,
            )

        # 如果模型不可用，返回默認結果
        if not self.is_available():
            return AnalysisResult(
                code=code,
                name=name,
                sentiment_score=50,
                trend_prediction=localize_trend_prediction('震盪', report_language),
                operation_advice=localize_operation_advice('持有', report_language),
                confidence_level=localize_confidence_level('低', report_language),
                analysis_summary=_localized_text(
                    report_language,
                    en='AI analysis is unavailable because no API key is configured.',
                    zh='AI 分析功能未啟用（未配置 API Key）',
                    ko='API 키가 설정되지 않아 AI 분석을 사용할 수 없습니다.',
                ),
                risk_warning=_localized_text(
                    report_language,
                    en='Configure an LLM API key (GEMINI_API_KEY/ANTHROPIC_API_KEY/OPENAI_API_KEY) and retry.',
                    zh='請配置 LLM API Key（GEMINI_API_KEY/ANTHROPIC_API_KEY/OPENAI_API_KEY）後重試',
                    ko='LLM API 키(GEMINI_API_KEY/ANTHROPIC_API_KEY/OPENAI_API_KEY)를 설정한 뒤 다시 시도하세요.',
                ),
                success=False,
                error_message=_localized_text(
                    report_language,
                    en='LLM API key is not configured',
                    zh='LLM API Key 未配置',
                    ko='LLM API 키가 설정되지 않았습니다',
                ),
                model_used=None,
                report_language=report_language,
            )
        
        try:
            # 格式化輸入（包含技術面數據和新聞）
            prompt = self._format_prompt(
                context,
                name,
                news_context,
                report_language=report_language,
                analysis_context_pack_summary=analysis_context_pack_summary,
            )
            legacy_audit_context = {
                "language": report_language,
                "market_group": _legacy_market_group(code),
                "analysis_mode": "stock_analysis",
                "legacy_prompt_mode": "legacy_default" if use_legacy_default_prompt else "skill_aware",
                "skill_config": {
                    "skill_instructions": skill_instructions,
                    "default_skill_policy": default_skill_policy,
                    "use_legacy_default_prompt": use_legacy_default_prompt,
                },
                "transport": "litellm",
                "dynamic_markers": _legacy_audit_marker_specs(
                    context,
                    code=code,
                    stock_name=name,
                    report_language=report_language,
                    news_context=news_context,
                    analysis_context_pack_summary=analysis_context_pack_summary,
                ),
            }
            
            config = self._get_runtime_config()
            backend_id, _fallback_backend_id = self._resolve_generation_backend_config()
            model_name = config.litellm_model or "unknown"
            if backend_id in LOCAL_CLI_GENERATION_BACKEND_IDS:
                model_name = backend_id
                legacy_audit_context["transport"] = backend_id
            logger.info(f"========== AI 分析 {name}({code}) ==========")
            logger.info(f"[LLM配置] 模型: {model_name}")
            logger.info(f"[LLM配置] Prompt 長度: {len(prompt)} 字符")
            logger.info(f"[LLM配置] 是否包含新聞: {'是' if news_context else '否'}")

            # 本地 CLI backend 是進程執行能力，不記錄完整 prompt。
            if backend_id in LOCAL_CLI_GENERATION_BACKEND_IDS:
                prompt_preview = redact_diagnostic_text(prompt, limit=500)
            else:
                prompt_preview = prompt[:500] + "..." if len(prompt) > 500 else prompt
            logger.info(f"[LLM Prompt 預覽]\n{prompt_preview}")
            if backend_id not in LOCAL_CLI_GENERATION_BACKEND_IDS:
                logger.debug(f"=== 完整 Prompt ({len(prompt)}字符) ===\n{prompt}\n=== End Prompt ===")

            # 設置生成配置
            generation_config = {
                "temperature": config.llm_temperature,
                "max_output_tokens": 8192,
            }

            logger.info(f"[LLM調用] 開始調用 {model_name}...")
            _emit_progress(68, f"{name}：LLM 已接收請求，等待響應")

            # 使用 litellm 調用（支持完整性校驗重試）
            current_prompt = prompt
            retry_count = 0
            max_retries = config.report_integrity_retry if config.report_integrity_enabled else 0

            while True:
                start_time = time.time()
                try:
                    response_text, model_used, llm_usage = self._call_litellm(
                        current_prompt,
                        generation_config,
                        system_prompt=system_prompt,
                        stream=True,
                        stream_progress_callback=stream_progress_callback,
                        response_validator=self._validate_json_response,
                        audit_context=legacy_audit_context,
                    )
                except _AllModelsFailedError as exc:
                    if exc.last_response_text is not None:
                        logger.warning(
                            "[LLM JSON] %s(%s): all models returned invalid JSON, using text fallback",
                            name,
                            code,
                        )
                        response_text = exc.last_response_text
                        model_used = exc.last_model
                        llm_usage = exc.last_usage
                    else:
                        raise
                elapsed = time.time() - start_time

                # 記錄響應信息
                logger.info(
                    f"[LLM返回] {model_name} 響應成功, 耗時 {elapsed:.2f}s, 響應長度 {len(response_text)} 字符"
                )
                if backend_id in LOCAL_CLI_GENERATION_BACKEND_IDS:
                    response_preview = redact_diagnostic_text(response_text, limit=300)
                else:
                    response_preview = response_text[:300] + "..." if len(response_text) > 300 else response_text
                logger.info(f"[LLM返回 預覽]\n{response_preview}")
                if backend_id not in LOCAL_CLI_GENERATION_BACKEND_IDS:
                    logger.debug(
                        f"=== {model_name} 完整響應 ({len(response_text)}字符) ===\n{response_text}\n=== End Response ==="
                    )
                # Keep parser/retry progress monotonic so task progress/message never "goes backward".
                parse_progress = min(99, 93 + retry_count * 2)
                _emit_progress(parse_progress, f"{name}：LLM 返回完成，正在解析 JSON")

                # 解析響應
                result = self._parse_response(response_text, code, name)
                result.raw_response = response_text
                result.search_performed = bool(news_context)
                result.market_snapshot = self._build_market_snapshot(context)
                result.model_used = model_used
                result.report_language = report_language
                normalize_chip_structure_availability(result, context.get("chip"))

                # 內容完整性校驗（可選）
                if not config.report_integrity_enabled:
                    break
                require_phase_decision = isinstance(context.get("market_phase_context"), dict)
                pass_integrity, missing_fields = self._check_content_integrity(
                    result,
                    require_phase_decision=require_phase_decision,
                )
                if pass_integrity:
                    break
                if retry_count < max_retries:
                    current_prompt = self._build_integrity_retry_prompt(
                        prompt,
                        response_text,
                        missing_fields,
                        report_language=report_language,
                    )
                    retry_count += 1
                    logger.info(
                        "[LLM完整性] 必填字段缺失 %s，第 %d 次補全重試",
                        missing_fields,
                        retry_count,
                    )
                    retry_progress = min(99, 92 + retry_count * 2)
                    _emit_progress(
                        retry_progress,
                        f"{name}：報告字段不完整，正在補全重試（{retry_count}/{max_retries}）",
                    )
                else:
                    self._apply_placeholder_fill(result, missing_fields)
                    logger.warning(
                        "[LLM完整性] 必填字段缺失 %s，已佔位補全，不阻塞流程",
                        missing_fields,
                    )
                    break

            if should_persist_usage_telemetry(llm_usage):
                persist_llm_usage(llm_usage, model_used, call_type="analysis", stock_code=code)

            logger.info(f"[LLM解析] {name}({code}) 分析完成: {result.trend_prediction}, 評分 {result.sentiment_score}")

            return result
            
        except Exception as e:
            safe_error = self._sanitize_hermes_exception_text(e)
            logger.error("AI 分析 %s(%s) 失敗: %s", name, code, safe_error)
            return AnalysisResult(
                code=code,
                name=name,
                sentiment_score=50,
                trend_prediction=localize_trend_prediction('震盪', report_language),
                operation_advice=localize_operation_advice('持有', report_language),
                confidence_level=localize_confidence_level('低', report_language),
                analysis_summary=_localized_text(
                    report_language,
                    en=f'Analysis failed: {safe_error[:100]}',
                    zh=f'分析過程出錯: {safe_error[:100]}',
                    ko=f'분석 중 오류가 발생했습니다: {safe_error[:100]}',
                ),
                risk_warning=_localized_text(
                    report_language,
                    en='Analysis failed. Please retry later or review manually.',
                    zh='分析失敗，請稍後重試或手動分析',
                    ko='분석에 실패했습니다. 잠시 후 다시 시도하거나 수동으로 검토하세요.',
                ),
                success=False,
                error_message=safe_error,
                model_used=None,
                report_language=report_language,
            )
    
    def _format_prompt(
        self, 
        context: Dict[str, Any], 
        name: str,
        news_context: Optional[str] = None,
        report_language: str = "zh",
        analysis_context_pack_summary: Optional[str] = None,
    ) -> str:
        """
        格式化分析提示詞（決策儀表盤 v2.0）
        
        包含：技術指標、實時行情（量比/換手率）、籌碼分佈、趨勢分析、新聞
        
        Args:
            context: 技術面數據上下文（包含增強數據）
            name: 股票名稱（默認值，可能被上下文覆蓋）
            news_context: 預先搜索的新聞內容
        """
        code = context.get('code', 'Unknown')
        report_language = normalize_report_language(report_language)
        _, _, use_legacy_default_prompt = self._get_skill_prompt_sections()
        
        # 優先使用上下文中的股票名稱（從 realtime_quote 獲取）
        stock_name = context.get('stock_name', name)
        if not stock_name or stock_name == f'股票{code}':
            stock_name = STOCK_NAME_MAP.get(code, f'股票{code}')
            
        today = context.get('today', {})
        unknown_text = get_unknown_text(report_language)
        no_data_text = get_no_data_text(report_language)
        quote_section_title, close_price_label = _phase_aware_quote_labels(context)
        hide_regular_session_ohlc = _should_hide_regular_session_ohlc(context)
        realtime_overlay_quote = hide_regular_session_ohlc and _today_has_realtime_overlay(today)
        pct_chg_label = "實時漲跌幅" if realtime_overlay_quote else "漲跌幅"
        volume_label = "實時成交量" if realtime_overlay_quote else "成交量"
        amount_label = "實時成交額" if realtime_overlay_quote else "成交額"
        quote_rows = [
            f"| {close_price_label} | {today.get('close', 'N/A')} 元 |",
        ]
        if not hide_regular_session_ohlc:
            quote_rows.extend(
                [
                    f"| 開盤價 | {today.get('open', 'N/A')} 元 |",
                    f"| 最高價 | {today.get('high', 'N/A')} 元 |",
                    f"| 最低價 | {today.get('low', 'N/A')} 元 |",
                ]
            )
        quote_rows.extend(
            [
                f"| {pct_chg_label} | {today.get('pct_chg', 'N/A')}% |",
                f"| {volume_label} | {self._format_volume(today.get('volume'))} |",
                f"| {amount_label} | {self._format_amount(today.get('amount'))} |",
            ]
        )
        quote_rows_text = "\n".join(quote_rows)
        
        # ========== 構建決策儀表盤格式的輸入 ==========
        prompt = f"""# 決策儀表盤分析請求

## 📊 股票基礎信息
| 項目 | 數據 |
|------|------|
| 股票代碼 | **{code}** |
| 股票名稱 | **{stock_name}** |
| 分析日期 | {context.get('date', unknown_text)} |

---
"""
        prompt += format_market_phase_prompt_section(
            context.get("market_phase_context"),
            report_language=report_language,
        )
        daily_market_context_section = format_daily_market_context_prompt_section(
            context.get("daily_market_context"),
            report_language=report_language,
        )
        if daily_market_context_section:
            prompt += daily_market_context_section
        if isinstance(analysis_context_pack_summary, str) and analysis_context_pack_summary:
            prompt += analysis_context_pack_summary
        prompt += f"""

## 📈 技術面數據

### {quote_section_title}
| 指標 | 數值 |
|------|------|
{quote_rows_text}

### 均線系統（關鍵判斷指標）
| 均線 | 數值 | 說明 |
|------|------|------|
| MA5 | {today.get('ma5', 'N/A')} | 短期趨勢線 |
| MA10 | {today.get('ma10', 'N/A')} | 中短期趨勢線 |
| MA20 | {today.get('ma20', 'N/A')} | 中期趨勢線 |
| 均線形態 | {context.get('ma_status', unknown_text)} | 多頭/空頭/纏繞 |
"""
        
        # 添加實時行情數據（量比、換手率等）
        if 'realtime' in context:
            rt = context['realtime']
            prompt += f"""
### 實時行情增強數據
| 指標 | 數值 | 解讀 |
|------|------|------|
| 當前價格 | {rt.get('price', 'N/A')} 元 | |
| **量比** | **{rt.get('volume_ratio', 'N/A')}** | {rt.get('volume_ratio_desc', '')} |
| **換手率** | **{rt.get('turnover_rate', 'N/A')}%** | |
| 市盈率(動態) | {rt.get('pe_ratio', 'N/A')} | |
| 市淨率 | {rt.get('pb_ratio', 'N/A')} | |
| 總市值 | {self._format_amount(rt.get('total_mv'))} | |
| 流通市值 | {self._format_amount(rt.get('circ_mv'))} | |
| 60日漲跌幅 | {rt.get('change_60d', 'N/A')}% | 中期表現 |
"""

        # 添加財報與分紅（價值投資口徑）
        fundamental_context = context.get("fundamental_context") if isinstance(context, dict) else None
        earnings_block = (
            fundamental_context.get("earnings", {})
            if isinstance(fundamental_context, dict)
            else {}
        )
        earnings_data = (
            earnings_block.get("data", {})
            if isinstance(earnings_block, dict)
            else {}
        )
        financial_report = (
            earnings_data.get("financial_report", {})
            if isinstance(earnings_data, dict)
            else {}
        )
        dividend_metrics = (
            earnings_data.get("dividend", {})
            if isinstance(earnings_data, dict)
            else {}
        )
        if isinstance(financial_report, dict) or isinstance(dividend_metrics, dict):
            financial_report = financial_report if isinstance(financial_report, dict) else {}
            dividend_metrics = dividend_metrics if isinstance(dividend_metrics, dict) else {}
            ttm_yield = dividend_metrics.get("ttm_dividend_yield_pct", "N/A")
            ttm_cash = dividend_metrics.get("ttm_cash_dividend_per_share", "N/A")
            ttm_count = dividend_metrics.get("ttm_event_count", "N/A")
            report_date = financial_report.get("report_date", "N/A")
            prompt += f"""
### 財報與分紅（價值投資口徑）
| 指標 | 數值 | 說明 |
|------|------|------|
| 最近報告期 | {report_date} | 來自結構化財報字段 |
| 營業收入 | {financial_report.get('revenue', 'N/A')} | |
| 歸母淨利潤 | {financial_report.get('net_profit_parent', 'N/A')} | |
| 經營現金流 | {financial_report.get('operating_cash_flow', 'N/A')} | |
| ROE | {financial_report.get('roe', 'N/A')} | |
| 近12個月每股現金分紅 | {ttm_cash} | 僅現金分紅、稅前口徑 |
| TTM 股息率 | {ttm_yield} | 公式：近12個月每股現金分紅 / 當前價格 × 100% |
| TTM 分紅事件數 | {ttm_count} | |

> 若上述字段為 N/A 或缺失，請明確寫“數據缺失，無法判斷”，禁止編造。
"""

        capital_flow_block = (
            fundamental_context.get("capital_flow", {})
            if isinstance(fundamental_context, dict)
            else {}
        )
        capital_flow_data = (
            capital_flow_block.get("data", {})
            if isinstance(capital_flow_block, dict)
            else {}
        )
        stock_flow = (
            capital_flow_data.get("stock_flow", {})
            if isinstance(capital_flow_data, dict)
            else {}
        )
        sector_flow = (
            capital_flow_data.get("sector_rankings", {})
            if isinstance(capital_flow_data, dict)
            else {}
        )
        has_capital_flow = (
            isinstance(stock_flow, dict)
            and any(v is not None for v in stock_flow.values())
        ) or (
            isinstance(sector_flow, dict)
            and (sector_flow.get("top") or sector_flow.get("bottom"))
        )
        if has_capital_flow:
            top_sectors = sector_flow.get("top", []) if isinstance(sector_flow, dict) else []
            bottom_sectors = sector_flow.get("bottom", []) if isinstance(sector_flow, dict) else []
            top_sector_text = "、".join(
                str(item.get("name", "")).strip()
                for item in top_sectors[:3]
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            ) or "N/A"
            bottom_sector_text = "、".join(
                str(item.get("name", "")).strip()
                for item in bottom_sectors[:3]
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            ) or "N/A"
            prompt += f"""
### 主力資金流向（操作建議過濾器）
| 指標 | 數值 | 決策含義 |
|------|------|----------|
| 主力淨流入 | {stock_flow.get('main_net_inflow', 'N/A')} | 正值偏支持，負值偏壓制 |
| 5日淨流入 | {stock_flow.get('inflow_5d', 'N/A')} | 用於判斷資金持續性 |
| 10日淨流入 | {stock_flow.get('inflow_10d', 'N/A')} | 用於判斷資金持續性 |
| 資金流入靠前板塊 | {top_sector_text} | 板塊資金共振參考 |
| 資金流出靠前板塊 | {bottom_sector_text} | 板塊風險參考 |

> 資金流向只能作為價格位置的過濾器：接近壓力且主力流出時不得追買；接近支撐且未放量跌破時，優先判斷為持有觀察、震盪或洗盤觀察。
"""

        # 添加三大法人動向（臺股籌碼過濾器）— tw-only；僅當 institution 區塊 status='ok'
        # 且有淨額時注入，其他市場 status='not_supported' 會跳過，嚴格 additive。
        institution_block = (
            fundamental_context.get("institution", {})
            if isinstance(fundamental_context, dict)
            else {}
        )
        institution_data = (
            institution_block.get("data", {})
            if isinstance(institution_block, dict)
            else {}
        )
        if (
            isinstance(institution_block, dict)
            and institution_block.get("status") == "ok"
            and isinstance(institution_data, dict)
            and all(
                institution_data.get(key) is not None
                for key in ("foreign_net", "trust_net", "dealer_net", "total_net")
            )
        ):
            prompt += f"""
### 三大法人動向（臺股籌碼過濾器，淨買賣超，單位:股）
| 法人 | 淨買賣超 | 決策含義 |
|------|------|----------|
| 外資 | {institution_data.get('foreign_net', 'N/A')} | 正值=淨買超偏支持，負值=淨賣超偏壓制 |
| 投信 | {institution_data.get('trust_net', 'N/A')} | 投信持續買超常伴隨中線做多 |
| 自營商 | {institution_data.get('dealer_net', 'N/A')} | 短線避險/自營方向參考 |
| 三大法人合計 | {institution_data.get('total_net', 'N/A')} | 臺股最受關注的籌碼信號 |
| 資料日期 | {institution_data.get('date', 'N/A')} | 來源 {institution_data.get('source', 'N/A')} |

> 三大法人是臺股的籌碼過濾器（相當於 A 股主力資金/龍虎榜的角色，但口徑不同、不可混用）：外資與投信同向淨買支持價格、同向淨賣壓制價格。請據此判斷臺股籌碼結構，不要在有本數據時寫“籌碼結構：數據缺失”。
"""

        # 添加籌碼分佈數據
        if 'chip' in context:
            chip = context['chip']
            profit_ratio = chip.get('profit_ratio', 0)
            prompt += f"""
### 籌碼分佈數據（效率指標）
| 指標 | 數值 | 健康標準 |
|------|------|----------|
| **獲利比例** | **{profit_ratio:.1%}** | 70-90%時警惕 |
| 平均成本 | {chip.get('avg_cost', 'N/A')} 元 | 現價應高於5-15% |
| 90%籌碼集中度 | {chip.get('concentration_90', 0):.2%} | <15%為集中 |
| 70%籌碼集中度 | {chip.get('concentration_70', 0):.2%} | |
| 籌碼狀態 | {chip.get('chip_status', unknown_text)} | |
"""
        else:
            chip_unavailable_text = get_chip_unavailable_text(report_language)
            chip_instruction = (
                "Do not fabricate profit ratio, average cost, or concentration. Mention chip data "
                "unavailability only once in the report; do not repeat per-field no-data text in `chip_structure`."
                if report_language in ("en", "ko")
                else "請勿編造獲利比例、平均成本或集中度；報告中只說明一次籌碼數據不可用，不要把“數據缺失，無法判斷”逐字段重複寫入 `chip_structure`。"
            )
            prompt += f"""
### 籌碼分佈數據（效率指標）
> {chip_unavailable_text}
> {chip_instruction}
"""
        
        # 添加趨勢分析結果（僅隱式內建 bull_trend 默認回退保留舊口徑）
        if 'trend_analysis' in context:
            trend = _sanitize_trend_analysis_for_prompt(
                context['trend_analysis'],
                volume_change_ratio=context.get('volume_change_ratio'),
            )
            consistency_notes = trend.get('prompt_consistency_notes', [])
            if use_legacy_default_prompt:
                bias_warning = "🚨 超過5%，嚴禁追高！" if trend.get('bias_ma5', 0) > 5 else "✅ 安全範圍"
                prompt += f"""
### 趨勢分析預判（基於交易理念）
| 指標 | 數值 | 判定 |
|------|------|------|
| 趨勢狀態 | {trend.get('trend_status', unknown_text)} | |
| 均線排列 | {trend.get('ma_alignment', unknown_text)} | MA5>MA10>MA20為多頭 |
| 趨勢強度 | {trend.get('trend_strength', 0)}/100 | |
| **乖離率(MA5)** | **{trend.get('bias_ma5', 0):+.2f}%** | {bias_warning} |
| 乖離率(MA10) | {trend.get('bias_ma10', 0):+.2f}% | |
| 量能狀態 | {trend.get('volume_status', unknown_text)} | {trend.get('volume_trend', '')} |
| 系統信號 | {trend.get('buy_signal', unknown_text)} | |
| 系統評分 | {trend.get('signal_score', 0)}/100 | |

#### 系統分析理由
**買入理由**：
{chr(10).join('- ' + r for r in trend.get('signal_reasons', ['無'])) if trend.get('signal_reasons') else '- 無'}

**風險因素**：
{chr(10).join('- ' + r for r in trend.get('risk_factors', ['無'])) if trend.get('risk_factors') else '- 無'}
"""
                if consistency_notes:
                    prompt += f"""

**一致性約束**：
{chr(10).join('- ' + note for note in consistency_notes)}
"""
            else:
                bias_warning = (
                    "🚨 偏離較大，需謹慎評估追高風險"
                    if trend.get('bias_ma5', 0) > 5
                    else "✅ 位置相對可控"
                )
                prompt += f"""
### 技術與結構分析（供激活技能判斷參考）
| 指標 | 數值 | 說明 |
|------|------|------|
| 趨勢狀態 | {trend.get('trend_status', unknown_text)} | |
| 均線排列 | {trend.get('ma_alignment', unknown_text)} | 結合激活技能判斷結構強弱 |
| 趨勢強度 | {trend.get('trend_strength', 0)}/100 | |
| **價格位置(MA5)** | **{trend.get('bias_ma5', 0):+.2f}%** | {bias_warning} |
| 價格位置(MA10) | {trend.get('bias_ma10', 0):+.2f}% | |
| 量能狀態 | {trend.get('volume_status', unknown_text)} | {trend.get('volume_trend', '')} |
| 系統信號 | {trend.get('buy_signal', unknown_text)} | |
| 系統評分 | {trend.get('signal_score', 0)}/100 | |

#### 系統分析理由
**支持因素**：
{chr(10).join('- ' + r for r in trend.get('signal_reasons', ['無'])) if trend.get('signal_reasons') else '- 無'}

**風險因素**：
{chr(10).join('- ' + r for r in trend.get('risk_factors', ['無'])) if trend.get('risk_factors') else '- 無'}
"""
                if consistency_notes:
                    prompt += f"""

**一致性約束**：
{chr(10).join('- ' + note for note in consistency_notes)}
"""
        
        # 添加昨日對比數據
        if 'yesterday' in context:
            volume_change = context.get('volume_change_ratio', 'N/A')
            prompt += f"""
### 量價變化
- 成交量較昨日變化：{volume_change}倍
- 價格較昨日變化：{context.get('price_change_ratio', 'N/A')}%
"""
            parsed_volume_change = _safe_float(volume_change, default=math.nan)
            if math.isfinite(parsed_volume_change) and parsed_volume_change > 10:
                prompt += """
- ⚠️ 量能異常提示：成交量較昨日放大超過10倍，可能受異常數據或一次性衝量影響，必須降權解讀，不能機械視為強確認信號
"""
        
        # 添加新聞搜索結果（重點區域）
        news_window_days: Optional[int] = None
        context_window = context.get("news_window_days")
        try:
            if context_window is not None:
                parsed_window = int(context_window)
                if parsed_window > 0:
                    news_window_days = parsed_window
        except (TypeError, ValueError):
            news_window_days = None

        if news_window_days is None:
            prompt_config = self._get_runtime_config()
            news_window_days = resolve_news_window_days(
                news_max_age_days=getattr(prompt_config, "news_max_age_days", 3),
                news_strategy_profile=getattr(prompt_config, "news_strategy_profile", "short"),
            )
        prompt += """
---

## 📰 輿情情報
"""
        if news_context:
            prompt += f"""
以下是 **{stock_name}({code})** 近{news_window_days}日的新聞搜索結果，請重點提取：
1. 🚨 **風險警報**：減持、處罰、利空
2. 🎯 **利好催化**：業績、合同、政策
3. 📊 **業績預期**：年報預告、業績快報
4. 🕒 **時間規則（強制）**：
   - 輸出到 `risk_alerts` / `positive_catalysts` / `latest_news` 的每一條都必須帶具體日期（YYYY-MM-DD）
   - 超出近{news_window_days}日窗口的新聞一律忽略
   - 時間未知、無法確定發佈日期的新聞一律忽略

```
{news_context}
```
"""
        else:
            prompt += """
未搜索到該股票近期的相關新聞。請主要依據技術面數據進行分析。
"""

        # 注入缺失數據警告
        if context.get('data_missing'):
            prompt += """
⚠️ **數據缺失警告**
由於接口限制，當前無法獲取完整的實時行情和技術指標數據。
請 **忽略上述表格中的 N/A 數據**，重點依據 **【📰 輿情情報】** 中的新聞進行基本面和情緒面分析。
在回答技術面問題（如均線、乖離率）時，請直接說明“數據缺失，無法判斷”，**嚴禁編造數據**。
"""

        # 明確的輸出要求
        prompt += f"""
---

## ✅ 分析任務

請為 **{stock_name}({code})** 生成【決策儀表盤】，嚴格按照 JSON 格式輸出。
"""
        if context.get('is_index_etf'):
            prompt += """
> ⚠️ **指數/ETF 分析約束**：該標的為指數跟蹤型 ETF 或市場指數。
> - 風險分析僅關注：**指數走勢、跟蹤誤差、市場流動性**
> - 嚴禁將基金公司的訴訟、聲譽、高管變動納入風險警報
> - 業績預期基於**指數成分股整體表現**，而非基金公司財報
> - `risk_alerts` 中不得出現基金管理人相關的公司經營風險

"""
        prompt += f"""
### ⚠️ 重要：輸出正確的股票名稱格式
正確的股票名稱格式為“股票名稱（股票代碼）”，例如“貴州茅臺（600519）”。
如果上方顯示的股票名稱為"股票{code}"或不正確，請在分析開頭**明確輸出該股票的正確中文全稱**。
"""
        if use_legacy_default_prompt:
            prompt += f"""

### 重點關注（必須明確回答）：
1. ❓ 是否滿足 MA5>MA10>MA20 多頭排列？
2. ❓ 當前乖離率是否在安全範圍內（<5%）？—— 超過5%必須標註"嚴禁追高"
3. ❓ 量能是否配合（縮量回調/放量突破）？
4. ❓ 籌碼結構是否健康？
5. ❓ 消息面有無重大利空？（減持、處罰、業績變臉等）
"""
        else:
            prompt += f"""

### 重點關注（必須明確回答）：
1. ❓ 當前結構是否滿足激活技能的關鍵觸發條件？
2. ❓ 當前入場位置與風險回報是否合理？若偏離過大，請明確說明等待條件
3. ❓ 量能、波動與籌碼結構是否支持當前結論？
4. ❓ 消息面有無重大利空或與技能結論衝突的信息？
5. ❓ 若結論成立，具體觸發條件、止損位、觀察點分別是什麼？
"""
        prompt += f"""

### 決策儀表盤要求：
- **股票名稱**：必須輸出正確的中文全稱（如"貴州茅臺"而非"股票600519"）
- **核心結論**：一句話說清該買/該賣/該等
- **持倉分類建議**：空倉者怎麼做 vs 持倉者怎麼做
- **具體狙擊點位**：買入價、止損價、目標價（精確到分）
- **檢查清單**：每項用 ✅/⚠️/❌ 標記
- **消息面時間合規**：`latest_news`、`risk_alerts`、`positive_catalysts` 不得包含超出近{news_window_days}日或時間未知的信息
- **技術面一致性**：嚴禁把“空頭排列”和“多頭排列”等互斥結論同時當作有效依據；若基本面/事件面與技術面衝突，必須明確寫“事件先行、技術待確認”或“基本面偏多，但技術面尚未確認”
 
請輸出完整的 JSON 格式決策儀表盤。"""

        if report_language == "en":
            prompt += """

### Output language requirements (highest priority)
- Keep every JSON key exactly as defined above; do not translate keys.
- `decision_type` must remain `buy`, `hold`, or `sell`.
- All human-readable JSON values must be in English.
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, all nested dashboard text, checklist items, and every summary field.
- Use the common English company name when you are confident. If not, keep the listed company name rather than inventing one.
- When data is missing, explain it in English instead of Chinese.
"""
        elif report_language == "ko":
            prompt += """

### Output language requirements (highest priority)
- Keep every JSON key exactly as defined above; do not translate keys.
- `decision_type` must remain `buy`, `hold`, or `sell`.
- All human-readable JSON values must be in Korean (한국어).
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, all nested dashboard text, checklist items, and every summary field.
- Use the common Korean or original listed company name when you are confident. If not, keep the listed company name rather than inventing one.
- When data is missing, explain it in Korean instead of Chinese.
"""
        else:
            prompt += f"""

### 輸出語言要求（最高優先級）
- 所有 JSON 鍵名必須保持不變，不要翻譯鍵名。
- `decision_type` 必須保持為 `buy`、`hold`、`sell`。
- 所有面向用戶的人類可讀文本值必須使用中文。
- 當數據缺失時，請使用中文直接說明“{no_data_text}，無法判斷”。
"""
        
        return prompt
    
    def _format_volume(self, volume: Optional[float]) -> str:
        """格式化成交量顯示"""
        if volume is None:
            return 'N/A'
        if volume >= 1e8:
            return f"{volume / 1e8:.2f} 億股"
        elif volume >= 1e4:
            return f"{volume / 1e4:.2f} 萬股"
        else:
            return f"{volume:.0f} 股"
    
    def _format_amount(self, amount: Optional[float]) -> str:
        """格式化成交額顯示"""
        if amount is None:
            return 'N/A'
        if amount >= 1e8:
            return f"{amount / 1e8:.2f} 億元"
        elif amount >= 1e4:
            return f"{amount / 1e4:.2f} 萬元"
        else:
            return f"{amount:.0f} 元"

    def _format_percent(self, value: Optional[float]) -> str:
        """格式化百分比顯示"""
        if value is None:
            return 'N/A'
        try:
            return f"{float(value):.2f}%"
        except (TypeError, ValueError):
            return 'N/A'

    def _format_price(self, value: Optional[float]) -> str:
        """格式化價格顯示"""
        if value is None:
            return 'N/A'
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return 'N/A'

    def _build_market_snapshot(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """構建當日行情快照（展示用）"""
        today = context.get('today', {}) or {}
        realtime = context.get('realtime', {}) or {}
        yesterday = context.get('yesterday', {}) or {}

        prev_close = yesterday.get('close')
        close = today.get('close')
        high = today.get('high')
        low = today.get('low')

        amplitude = None
        change_amount = None
        if prev_close not in (None, 0) and high is not None and low is not None:
            try:
                amplitude = (float(high) - float(low)) / float(prev_close) * 100
            except (TypeError, ValueError, ZeroDivisionError):
                amplitude = None
        if prev_close is not None and close is not None:
            try:
                change_amount = float(close) - float(prev_close)
            except (TypeError, ValueError):
                change_amount = None

        snapshot = {
            "date": context.get('date', '未知'),
            "close": self._format_price(close),
            "open": self._format_price(today.get('open')),
            "high": self._format_price(high),
            "low": self._format_price(low),
            "prev_close": self._format_price(prev_close),
            "pct_chg": self._format_percent(today.get('pct_chg')),
            "change_amount": self._format_price(change_amount),
            "amplitude": self._format_percent(amplitude),
            "volume": self._format_volume(today.get('volume')),
            "amount": self._format_amount(today.get('amount')),
        }

        if realtime:
            snapshot.update({
                "price": self._format_price(realtime.get('price')),
                "volume_ratio": realtime.get('volume_ratio', 'N/A'),
                "turnover_rate": self._format_percent(realtime.get('turnover_rate')),
                "source": getattr(realtime.get('source'), 'value', realtime.get('source', 'N/A')),
            })

        return snapshot

    def _check_content_integrity(
        self,
        result: AnalysisResult,
        *,
        require_phase_decision: bool = False,
    ) -> Tuple[bool, List[str]]:
        """Delegate to module-level check_content_integrity."""
        return check_content_integrity(result, require_phase_decision=require_phase_decision)

    def _build_integrity_complement_prompt(self, missing_fields: List[str], report_language: str = "zh") -> str:
        """Build complement instruction for missing mandatory fields."""
        report_language = normalize_report_language(report_language)
        if report_language in ("en", "ko"):
            lines = ["### Completion requirements: fill the missing mandatory fields below and output the full JSON again:"]
            for f in missing_fields:
                if f == "sentiment_score":
                    lines.append("- sentiment_score: integer score from 0 to 100")
                elif f == "operation_advice":
                    lines.append("- operation_advice: localized action advice")
                elif f == "analysis_summary":
                    lines.append("- analysis_summary: concise analysis summary")
                elif f == "dashboard.core_conclusion.one_sentence":
                    lines.append("- dashboard.core_conclusion.one_sentence: one-line decision")
                elif f == "dashboard.intelligence.risk_alerts":
                    lines.append("- dashboard.intelligence.risk_alerts: risk alert list (can be empty)")
                elif f == "dashboard.battle_plan.sniper_points.stop_loss":
                    lines.append("- dashboard.battle_plan.sniper_points.stop_loss: stop-loss level")
                elif f == "dashboard.phase_decision.phase_context":
                    lines.append("- dashboard.phase_decision.phase_context: public market phase summary subset")
                elif f == "dashboard.phase_decision.action_window":
                    lines.append("- dashboard.phase_decision.action_window: phase-aware action window")
                elif f == "dashboard.phase_decision.immediate_action":
                    lines.append("- dashboard.phase_decision.immediate_action: act now / wait / watch / no intraday action")
                elif f == "dashboard.phase_decision.watch_conditions":
                    lines.append("- dashboard.phase_decision.watch_conditions: list of watch conditions")
                elif f == "dashboard.phase_decision.next_check_time":
                    lines.append("- dashboard.phase_decision.next_check_time: next check point or market-local time")
                elif f == "dashboard.phase_decision.confidence_reason":
                    lines.append("- dashboard.phase_decision.confidence_reason: confidence rationale and data limits")
                elif f == "dashboard.phase_decision.data_limitations":
                    lines.append("- dashboard.phase_decision.data_limitations: list of phase/data quality limitations")
            return "\n".join(lines)

        lines = ["### 補全要求：請在上方分析基礎上補充以下必填內容，並輸出完整 JSON："]
        for f in missing_fields:
            if f == "sentiment_score":
                lines.append("- sentiment_score: 0-100 綜合評分")
            elif f == "operation_advice":
                lines.append("- operation_advice: 買入/加倉/持有/減倉/賣出/觀望")
            elif f == "analysis_summary":
                lines.append("- analysis_summary: 綜合分析摘要")
            elif f == "dashboard.core_conclusion.one_sentence":
                lines.append("- dashboard.core_conclusion.one_sentence: 一句話決策")
            elif f == "dashboard.intelligence.risk_alerts":
                lines.append("- dashboard.intelligence.risk_alerts: 風險警報列表（可為空數組）")
            elif f == "dashboard.battle_plan.sniper_points.stop_loss":
                lines.append("- dashboard.battle_plan.sniper_points.stop_loss: 止損價")
            elif f == "dashboard.phase_decision.phase_context":
                lines.append("- dashboard.phase_decision.phase_context: 公開低敏市場階段摘要子集")
            elif f == "dashboard.phase_decision.action_window":
                lines.append("- dashboard.phase_decision.action_window: 階段化行動窗口")
            elif f == "dashboard.phase_decision.immediate_action":
                lines.append("- dashboard.phase_decision.immediate_action: 立即行動/等待確認/觀察/無盤中動作")
            elif f == "dashboard.phase_decision.watch_conditions":
                lines.append("- dashboard.phase_decision.watch_conditions: 觀察條件數組")
            elif f == "dashboard.phase_decision.next_check_time":
                lines.append("- dashboard.phase_decision.next_check_time: 下一次檢查點或市場本地時間")
            elif f == "dashboard.phase_decision.confidence_reason":
                lines.append("- dashboard.phase_decision.confidence_reason: 置信度理由與數據限制")
            elif f == "dashboard.phase_decision.data_limitations":
                lines.append("- dashboard.phase_decision.data_limitations: 階段/數據質量限制數組")
        return "\n".join(lines)

    def _build_integrity_retry_prompt(
        self,
        base_prompt: str,
        previous_response: str,
        missing_fields: List[str],
        report_language: str = "zh",
    ) -> str:
        """Build retry prompt using the previous response as the complement baseline."""
        complement = self._build_integrity_complement_prompt(missing_fields, report_language=report_language)
        previous_output = previous_response.strip()
        if normalize_report_language(report_language) in ("en", "ko"):
            prefix = "### The previous output is below. Complete the missing fields based on that output and return the full JSON again. Do not omit existing fields:"
        else:
            prefix = "### 上一次輸出如下，請在該輸出基礎上補齊缺失字段，並重新輸出完整 JSON。不要省略已有字段："
        return "\n\n".join([
            base_prompt,
            prefix,
            previous_output,
            complement,
        ])

    def _apply_placeholder_fill(self, result: AnalysisResult, missing_fields: List[str]) -> None:
        """Delegate to module-level apply_placeholder_fill."""
        apply_placeholder_fill(result, missing_fields)

    def _extract_analysis_json_object(self, response_text: str) -> Tuple[str, Dict[str, Any]]:
        """Extract the single allowed JSON object from an LLM response."""

        text = response_text or ""
        stripped = text.strip()
        if not stripped:
            raise ValueError("empty_response")

        fence_pattern = re.compile(
            r"```[ \t]*(?P<lang>[A-Za-z0-9_-]*)[ \t]*\n?(?P<body>.*?)```",
            flags=re.DOTALL,
        )
        fenced_matches = list(fence_pattern.finditer(text))
        if len(fenced_matches) > 1:
            raise ValueError("ambiguous_json")
        if len(fenced_matches) == 1:
            match = fenced_matches[0]
            outside = (text[:match.start()] + text[match.end():]).strip()
            if outside:
                raise ValueError("ambiguous_json")
            fence_lang = (match.group("lang") or "").strip().lower()
            if fence_lang not in {"", "json"}:
                raise ValueError("ambiguous_json")
            json_str = match.group("body").strip()
            data = self._load_analysis_json_candidate(json_str)
            return json_str, data
        if "```" in text:
            raise ValueError("ambiguous_json")

        try:
            data = self._load_analysis_json_candidate(stripped)
        except json.JSONDecodeError as exc:
            if self._contains_embedded_json_object(text):
                raise ValueError("ambiguous_json") from exc
            raise
        return stripped, data

    def _load_analysis_json_candidate(self, json_str: str) -> Dict[str, Any]:
        """Parse one already-selected JSON candidate, repairing common LLM JSON drift."""
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            stripped = (json_str or "").strip()
            try:
                _obj, end = json.JSONDecoder().raw_decode(stripped)
            except json.JSONDecodeError:
                pass
            else:
                if stripped[end:].strip():
                    raise
            if not (stripped.startswith("{") and stripped.endswith("}")):
                raise
            repaired = self._fix_json_string(stripped)
            data = json.loads(repaired)
        if not isinstance(data, dict):
            raise TypeError("json_root_not_object")
        return data

    @staticmethod
    def _contains_embedded_json_object(text: str) -> bool:
        decoder = json.JSONDecoder()
        count = 0
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                _obj, end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            count += 1
            before = text[:index].strip()
            after = text[index + end:].strip()
            if count > 1 or before or after:
                return True
        return False

    def _validate_analysis_minimal_contract(self, data: Dict[str, Any]) -> None:
        try:
            AnalysisReportSchema.model_validate(data)
        except Exception as exc:
            logger.warning(
                "AnalysisReportSchema validation failed; continuing with raw parser contract: %s",
                str(exc)[:200],
            )
        minimal_keys = {
            "sentiment_score",
            "trend_prediction",
            "operation_advice",
            "analysis_summary",
            "dashboard",
        }
        if not any(key in data for key in minimal_keys):
            raise self._generation_validation_error(
                GenerationErrorCode.SCHEMA_VALIDATION_FAILED,
                reason="minimal_contract_failed",
                message="analysis JSON does not contain any minimal parser field",
            )
        if "sentiment_score" in data:
            try:
                int(data.get("sentiment_score", 50))
            except (TypeError, ValueError) as exc:
                raise self._generation_validation_error(
                    GenerationErrorCode.SCHEMA_VALIDATION_FAILED,
                    reason="parser_contract_failed",
                    message="sentiment_score must be integer-compatible",
                ) from exc

    def _generation_validation_error(
        self,
        error_code: GenerationErrorCode,
        *,
        reason: str,
        message: str,
    ) -> GenerationError:
        try:
            backend_id, _fallback_backend_id = self._resolve_generation_backend_config()
        except GenerationError:
            backend_id = "generation_backend"
        return GenerationError(
            error_code=error_code,
            stage="validation",
            retryable=True,
            fallbackable=True,
            backend=backend_id,
            provider=backend_id,
            details={
                "reason": reason,
                "message": message,
            },
        )

    def _parse_response(
        self, 
        response_text: str, 
        code: str, 
        name: str
    ) -> AnalysisResult:
        """
        解析 Gemini 響應（決策儀表盤版）
        
        嘗試從響應中提取 JSON 格式的分析結果，包含 dashboard 字段
        如果解析失敗，嘗試智能提取或返回默認結果
        """
        try:
            report_language = normalize_report_language(
                getattr(self._get_runtime_config(), "report_language", "zh")
            )
            try:
                _json_str, data = self._extract_analysis_json_object(response_text)
                self._validate_analysis_minimal_contract(data)
            except Exception as exc:
                logger.warning("無法從響應中提取唯一有效 JSON，標記為解析失敗: %s", exc)
                return self._parse_text_response(response_text, code, name)

            # 提取 dashboard 數據
            dashboard = data.get('dashboard', None)
            guardrail_reason = data.get("guardrail_reason") or data.get("downgrade_reason")
            if guardrail_reason and isinstance(dashboard, dict):
                score_calibration = dashboard.get("decision_score_calibration")
                if not isinstance(score_calibration, dict):
                    score_calibration = {}
                    dashboard["decision_score_calibration"] = score_calibration
                score_calibration.setdefault("guardrail_reason", str(guardrail_reason).strip())
            # 歸一化 signal_attribution（LLM 可能返回字符串/負數/總和≠100）
            normalize_report_signal_attribution(dashboard)

            # 優先使用 AI 返回的股票名稱（如果原名稱無效或包含代碼）
            ai_stock_name = data.get('stock_name')
            if ai_stock_name and (name.startswith('股票') or name == code or 'Unknown' in name):
                name = ai_stock_name

            # 解析所有字段，使用默認值防止缺失
            # 解析 decision_type，如果沒有則根據 operation_advice 推斷
            decision_type = data.get('decision_type', '')
            if not decision_type:
                op = data.get('operation_advice', localize_operation_advice('持有', report_language))
                decision_type = infer_decision_type_from_advice(op, default='hold')

            explicit_action = data.get("action")
            if explicit_action is None and isinstance(dashboard, dict):
                explicit_action = dashboard.get("action")

            result = AnalysisResult(
                code=code,
                name=name,
                # 核心指標
                sentiment_score=int(data.get('sentiment_score', 50)),
                trend_prediction=data.get('trend_prediction', localize_trend_prediction('震盪', report_language)),
                operation_advice=data.get('operation_advice', localize_operation_advice('持有', report_language)),
                decision_type=decision_type,
                confidence_level=localize_confidence_level(
                    data.get('confidence_level', localize_confidence_level('中', report_language)),
                    report_language,
                ),
                report_language=report_language,
                # 決策儀表盤
                dashboard=dashboard,
                # 走勢分析
                trend_analysis=data.get('trend_analysis', ''),
                short_term_outlook=data.get('short_term_outlook', ''),
                medium_term_outlook=data.get('medium_term_outlook', ''),
                # 技術面
                technical_analysis=data.get('technical_analysis', ''),
                ma_analysis=data.get('ma_analysis', ''),
                volume_analysis=data.get('volume_analysis', ''),
                pattern_analysis=data.get('pattern_analysis', ''),
                # 基本面
                fundamental_analysis=data.get('fundamental_analysis', ''),
                sector_position=data.get('sector_position', ''),
                company_highlights=data.get('company_highlights', ''),
                # 情緒面/消息面
                news_summary=data.get('news_summary', ''),
                market_sentiment=data.get('market_sentiment', ''),
                hot_topics=data.get('hot_topics', ''),
                # 綜合
                analysis_summary=data.get('analysis_summary', _localized_text(
                    report_language, en='Analysis completed', zh='分析完成', ko='분석 완료')),
                key_points=data.get('key_points', ''),
                risk_warning=data.get('risk_warning', ''),
                buy_reason=data.get('buy_reason', ''),
                # 元數據
                search_performed=data.get('search_performed', False),
                data_sources=data.get('data_sources', _localized_text(
                    report_language, en='Technical data', zh='技術面數據', ko='기술적 데이터')),
                success=True,
            )
            return populate_decision_action_fields(
                result,
                explicit_action=explicit_action,
                align_with_score=False,
            )
                
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解析失敗: {e}，標記為解析失敗")
            return self._parse_text_response(response_text, code, name)
    
    def _fix_json_string(self, json_str: str) -> str:
        """修復常見的 JSON 格式問題"""
        import re
        
        # 移除註釋
        json_str = re.sub(r'//.*?\n', '\n', json_str)
        json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)
        
        # 修復尾隨逗號
        json_str = re.sub(r',\s*}', '}', json_str)
        json_str = re.sub(r',\s*]', ']', json_str)
        
        # 確保布爾值是小寫
        json_str = json_str.replace('True', 'true').replace('False', 'false')
        
        # fix by json-repair
        json_str = repair_json(json_str)
        
        return json_str

    def _validate_json_response(self, text: str) -> None:
        """Validate that *text* contains one parser-compatible JSON object.

        Used as the ``response_validator`` argument to :meth:`_call_litellm` so
        that a JSON-less or unparseable reply from the primary model is treated
        as a model failure and triggers fallback to the next configured model.

        Raises:
            GenerationError: if the response has no unique parser-compatible
                JSON object, the selected JSON candidate cannot be parsed, or
                the parsed object cannot satisfy the minimal parser contract.
        """
        try:
            _json_str, data = self._extract_analysis_json_object(text)
        except ValueError as exc:
            reason = str(exc) or "invalid_json"
            if reason == "ambiguous_json":
                message = "JSON source is ambiguous"
            else:
                message = "No unique JSON object found in LLM response"
            raise self._generation_validation_error(
                GenerationErrorCode.INVALID_JSON,
                reason=reason,
                message=message,
            ) from exc
        except json.JSONDecodeError as exc:
            raise self._generation_validation_error(
                GenerationErrorCode.INVALID_JSON,
                reason="invalid_json",
                message=str(exc)[:200],
            ) from exc
        except Exception as exc:
            raise self._generation_validation_error(
                GenerationErrorCode.INVALID_JSON,
                reason="invalid_json",
                message=str(exc)[:200],
            ) from exc

        self._validate_analysis_minimal_contract(data)
    
    def _parse_text_response(
        self, 
        response_text: str, 
        code: str, 
        name: str
    ) -> AnalysisResult:
        """從純文本響應中儘可能提取分析信息"""
        report_language = normalize_report_language(
            getattr(self._get_runtime_config(), "report_language", "zh")
        )
        # 嘗試識別關鍵詞來判斷情緒
        sentiment_score = 50
        trend = localize_trend_prediction('震盪', report_language)
        advice = localize_operation_advice('持有', report_language)
        
        text_lower = response_text.lower()
        
        # 簡單的情緒識別
        positive_keywords = ['看多', '買入', '上漲', '突破', '強勢', '利好', '加倉', 'bullish', 'buy']
        negative_keywords = ['看空', '賣出', '下跌', '跌破', '弱勢', '利空', '減倉', 'bearish', 'sell']
        
        positive_count = sum(1 for kw in positive_keywords if kw in text_lower)
        negative_count = sum(1 for kw in negative_keywords if kw in text_lower)
        
        if positive_count > negative_count + 1:
            sentiment_score = 65
            trend = localize_trend_prediction('看多', report_language)
            advice = localize_operation_advice('買入', report_language)
            decision_type = 'buy'
        elif negative_count > positive_count + 1:
            sentiment_score = 35
            trend = localize_trend_prediction('看空', report_language)
            advice = localize_operation_advice('賣出', report_language)
            decision_type = 'sell'
        else:
            decision_type = 'hold'
        
        # 截取前500字符作為摘要
        summary = response_text[:500] if response_text else _localized_text(
            report_language, en='No analysis result', zh='無分析結果', ko='분석 결과 없음')
        
        result = AnalysisResult(
            code=code,
            name=name,
            sentiment_score=sentiment_score,
            trend_prediction=trend,
            operation_advice=advice,
            decision_type=decision_type,
            confidence_level=localize_confidence_level('低', report_language),
            analysis_summary=summary,
            key_points=_localized_text(
                report_language,
                en='JSON parsing failed; treat this as best-effort output.',
                zh='JSON解析失敗，僅供參考',
                ko='JSON 파싱에 실패했습니다. 참고용으로만 사용하세요.',
            ),
            risk_warning=_localized_text(
                report_language,
                en='The result may be inaccurate. Cross-check with other information.',
                zh='分析結果可能不準確，建議結合其他信息判斷',
                ko='결과가 부정확할 수 있습니다. 다른 정보와 교차 확인하세요.',
            ),
            raw_response=response_text,
            success=False,
            error_message='LLM response is not valid JSON; analysis result will not be persisted',
            report_language=report_language,
        )
        return populate_decision_action_fields(result, align_with_score=False)
    
    def batch_analyze(
        self, 
        contexts: List[Dict[str, Any]],
        delay_between: float = 2.0
    ) -> List[AnalysisResult]:
        """
        批量分析多隻股票
        
        注意：為避免 API 速率限制，每次分析之間會有延遲
        
        Args:
            contexts: 上下文數據列表
            delay_between: 每次分析之間的延遲（秒）
            
        Returns:
            AnalysisResult 列表
        """
        results = []
        
        for i, context in enumerate(contexts):
            if i > 0:
                logger.debug(f"等待 {delay_between} 秒後繼續...")
                time.sleep(delay_between)
            
            result = self.analyze(context)
            results.append(result)
        
        return results


# 便捷函數
def get_analyzer() -> GeminiAnalyzer:
    """獲取 LLM 分析器實例"""
    return GeminiAnalyzer()


if __name__ == "__main__":
    # 測試代碼
    logging.basicConfig(level=logging.DEBUG)
    
    # 模擬上下文數據
    test_context = {
        'code': '600519',
        'date': '2026-01-09',
        'today': {
            'open': 1800.0,
            'high': 1850.0,
            'low': 1780.0,
            'close': 1820.0,
            'volume': 10000000,
            'amount': 18200000000,
            'pct_chg': 1.5,
            'ma5': 1810.0,
            'ma10': 1800.0,
            'ma20': 1790.0,
            'volume_ratio': 1.2,
        },
        'ma_status': '多頭排列 📈',
        'volume_change_ratio': 1.3,
        'price_change_ratio': 1.5,
    }
    
    analyzer = GeminiAnalyzer()
    
    if analyzer.is_available():
        print("=== AI 分析測試 ===")
        result = analyzer.analyze(test_context)
        print(f"分析結果: {result.to_dict()}")
    else:
        print("Gemini API 未配置，跳過測試")
