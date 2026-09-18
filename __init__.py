"""Hermes plugin: Jev (TypeSafe System One) route hint, tool-risk gate, done-check.

Thin adapter — the decision logic lives in ``jev_guard.py``, which also runs
standalone as a shell hook (see README). Hooks fail open: an exception is
logged and the agent proceeds.
"""
from __future__ import annotations

import logging

from . import jev_guard

logger = logging.getLogger(__name__)

_EVENTS = ("pre_llm_call", "pre_tool_call", "pre_verify")
_TOP_LEVEL = ("tool_name", "args", "session_id", "cwd", "profile")


def _as_payload(event: str, kwargs: dict) -> dict:
    """Map plugin-hook kwargs onto the payload shape jev_guard.handle() expects."""
    return {
        "hook_event_name": event,
        "tool_name": kwargs.get("tool_name"),
        "tool_input": kwargs.get("args") if isinstance(kwargs.get("args"), dict) else None,
        "session_id": kwargs.get("session_id") or "",
        "cwd": "",
        "profile": "",
        "extra": {k: v for k, v in kwargs.items() if k not in _TOP_LEVEL},
    }


def _make_hook(event: str):
    def hook(**kwargs):
        try:
            return jev_guard.handle(_as_payload(event, kwargs)) or None
        except Exception:  # fail open, same policy as the standalone script
            logger.warning("jev-guard: %s failed open", event, exc_info=True)
            return None

    hook.__name__ = f"jev_guard_{event}"
    return hook


def register(ctx):
    """Resolve settings, then register the hooks and the model-tier middleware."""
    try:  # profile-scoped flow log; older loaders without ctx.state keep the default path
        log_path = str(ctx.state.data_dir / "jev-flow.jsonl")
    except AttributeError:
        log_path = jev_guard.LOG_PATH
    jev_guard.configure(
        log_path=log_path,
        timeout=ctx.get_config("timeout", default=jev_guard.TIMEOUT),
        approve_at=ctx.get_config("approve_at", default=jev_guard.APPROVE_AT),
        block_at=ctx.get_config("block_at", default=jev_guard.BLOCK_AT),
        verify_at=ctx.get_config("verify_at", default=jev_guard.VERIFY_AT),
        max_state_chars=ctx.get_config("max_state_chars", default=jev_guard.MAX_STATE_CHARS),
        economy_model=ctx.get_config("economy_model", default=jev_guard.ECONOMY_MODEL),
        standard_model=ctx.get_config("standard_model", default=jev_guard.STANDARD_MODEL),
        frontier_model=ctx.get_config("frontier_model", default=jev_guard.FRONTIER_MODEL),
        risk_tools=ctx.get_config("risk_tools", default=jev_guard.RISK_TOOLS),
    )
    for event in _EVENTS:
        ctx.register_hook(event, _make_hook(event))
    # Rewrites the model per request when a tier model is configured; no-op otherwise.
    ctx.register_middleware("llm_request", jev_guard.on_llm_request)
