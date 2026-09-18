#!/usr/bin/env python3
"""Hermes shell hooks that use TypeSafe Jev (System One) for routing hints,
tool-risk gating, and done-checks.

Hermes pipes a JSON payload on stdin and reads one directive JSON back:
  {"context": "..."}                              pre_llm_call  -> appended to the user message
  {"action": "approve"|"block", "message": "..."} pre_tool_call -> human gate / block
  {"action": "continue", "message": "..."}        pre_verify    -> nudge agent to keep working

Fail-open: on any error this prints a note to stderr and outputs nothing, so
the agent proceeds. See README.md for the config.yaml block and env knobs.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"

TIMEOUT = float(os.environ.get("JEV_TIMEOUT", "8"))
APPROVE_AT = float(os.environ.get("JEV_APPROVE_AT", "0.7"))
BLOCK_AT = float(os.environ.get("JEV_BLOCK_AT", "0.97"))
VERIFY_AT = float(os.environ.get("JEV_VERIFY_AT", "0.7"))
MAX_STATE_CHARS = int(os.environ.get("JEV_MAX_STATE_CHARS", "12000"))

_SETTING_NAMES = ("timeout", "approve_at", "block_at", "verify_at", "max_state_chars")


def configure(**overrides) -> None:
    """Override module defaults; the plugin resolves these from config.yaml.

    Env vars above stay the standalone default. Unknown keys are ignored.
    """
    for name, value in overrides.items():
        if name in _SETTING_NAMES and value is not None:
            current = globals()[name.upper()]
            globals()[name.upper()] = type(current)(value)


ROUTES = {
    "direct_answer": "simple or factual request; answer directly",
    "deep_reasoning": "design, architecture, or multi-step reasoning request",
    "code_task": "write or modify code now",
    "research": "gather external information before answering",
}


def api_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    for home in (os.environ.get("HERMES_HOME"), str(Path.home() / ".hermes")):
        if not home:
            continue
        try:
            lines = (Path(home) / ".env").read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            m = re.match(r"^(?:export\s+)?TYPESAFE_API_KEY\s*=\s*(.*)$", line.strip())
            if m:
                return m.group(1).strip().strip('"').strip("'")
    raise RuntimeError("TYPESAFE_API_KEY not found in the environment or $HERMES_HOME/.env")


def ask(state, questions: dict) -> dict:
    body = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))["answers"]


def clip(obj, limit: int | None = None) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return text[: limit or MAX_STATE_CHARS]


def _event_field(payload: dict, name: str, default=None):
    """Event-specific kwargs arrive under `extra`; accept a top-level copy too."""
    extra = payload.get("extra") or {}
    value = extra.get(name)
    return payload.get(name) if value is None else value


def on_pre_llm_call(payload: dict, ask=ask) -> dict:
    message = (_event_field(payload, "user_message") or "").strip()
    if not message:
        return {}
    answers = ask(clip(message), {
        "route": {
            "type": "choice",
            "instructions": "Which handling route fits this request best?",
            "criteria": ROUTES,
        },
        "complexity": {
            "type": "score",
            "instructions": "How much reasoning depth does this request demand?",
            "criteria": ["trivial; one fact", "moderate; some design thought",
                         "deep; multi-step reasoning"],
        },
    })
    route = answers["route"]
    chosen = route.get("choice", "")
    p = (route.get("probabilities") or {}).get(chosen)
    hint = (f"Jev route hint: {chosen}"
            + (f" (p={p:.2f})" if isinstance(p, (int, float)) else "")
            + f" - {ROUTES.get(chosen, '')}. Complexity {answers['complexity'].get('score', 0):.2f}/2.")
    return {"context": hint}


def on_pre_tool_call(payload: dict, ask=ask) -> dict:
    state = {"tool": payload.get("tool_name") or "",
             "input": payload.get("tool_input") or {},
             "cwd": payload.get("cwd") or ""}
    risk = ask(clip(state), {
        "risk": {
            "type": "noul",
            "instructions": "Does executing this action risk destroying data or making an "
                            "irreversible change to the system?",
            "criteria": {
                "true": "deletes, overwrites, force-pushes, or irreversibly changes data or system state",
                "false": "read-only, additive, or easily reversible",
            },
        },
    })["risk"]["noul"]
    if risk >= BLOCK_AT:
        return {"action": "block",
                "message": f"Jev risk {risk:.2f}: blocked; rewrite as a safer, reversible step."}
    if risk >= APPROVE_AT:
        return {"action": "approve",
                "message": f"Jev risk {risk:.2f}: ask the user before running this."}
    return {}


def on_pre_verify(payload: dict, ask=ask) -> dict:
    if int(_event_field(payload, "attempt") or 0) >= 1:
        return {}  # one nudge per turn; Hermes caps nudges at 3 anyway
    response = (_event_field(payload, "final_response") or "").strip()
    if not response:
        return {}
    state = {"final_response": response,
             "changed_paths": _event_field(payload, "changed_paths") or []}
    p = ask(clip(state), {
        "unfinished": {
            "type": "noul",
            "instructions": "Does this final response overclaim (states results, tests, or "
                            "verification it does not show evidence for) or leave work it "
                            "said it would do unfinished?",
            "criteria": {
                "true": "claims unverifiable results or visibly incomplete work",
                "false": "claims match the evidence shown; work looks complete",
            },
        },
    })["unfinished"]["noul"]
    if p >= VERIFY_AT:
        return {"action": "continue",
                "message": f"Jev done-check flagged this (p={p:.2f}). Show real evidence for "
                           "each claim, or finish the remaining work before stopping."}
    return {}


HANDLERS = {
    "pre_llm_call": on_pre_llm_call,
    "pre_tool_call": on_pre_tool_call,
    "pre_verify": on_pre_verify,
}


def handle(payload: dict) -> dict:
    handler = HANDLERS.get(payload.get("hook_event_name") or "")
    return handler(payload) if handler else {}


def _scripted_ask(route="deep_reasoning", complexity=1.5, risk=0.0, unfinished=0.0):
    """Deterministic stand-in for ask() so the decision logic is testable offline."""
    def fake(state, questions, key=None):
        out = {}
        if "route" in questions:
            out["route"] = {"type": "choice", "choice": route,
                            "probabilities": {route: 0.9}, "confidence": 0.9}
        if "complexity" in questions:
            out["complexity"] = {"type": "score", "score": complexity}
        if "risk" in questions:
            out["risk"] = {"type": "noul", "noul": risk}
        if "unfinished" in questions:
            out["unfinished"] = {"type": "noul", "noul": unfinished}
        return out
    return fake


def self_test() -> int:
    configure(approve_at=0.7, block_at=0.97, verify_at=0.7)  # deterministic regardless of env
    hint = on_pre_llm_call({"extra": {"user_message": "help me design a queue"}},
                           ask=_scripted_ask())["context"]
    assert "deep_reasoning" in hint and "Complexity 1.50/2" in hint, hint
    assert on_pre_llm_call({"extra": {"user_message": "  "}}, ask=_scripted_ask()) == {}

    tool_payload = {"tool_name": "terminal", "tool_input": {"command": "ls"}}
    assert on_pre_tool_call(tool_payload, ask=_scripted_ask(risk=0.1)) == {}
    assert on_pre_tool_call(tool_payload, ask=_scripted_ask(risk=0.8))["action"] == "approve"
    assert on_pre_tool_call(tool_payload, ask=_scripted_ask(risk=0.99))["action"] == "block"

    verify_payload = {"extra": {"final_response": "Done. All tests pass.",
                                "changed_paths": ["a.py"]}}
    assert on_pre_verify(verify_payload, ask=_scripted_ask(unfinished=0.9))["action"] == "continue"
    assert on_pre_verify({"extra": {"final_response": "Done.", "attempt": 1}},
                         ask=_scripted_ask(unfinished=0.9)) == {}
    assert on_pre_verify(verify_payload, ask=_scripted_ask(unfinished=0.1)) == {}

    assert handle({"hook_event_name": "unknown_event"}) == {}
    print("self-test OK")
    return 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        print(f"jev-guard: unreadable payload: {exc}", file=sys.stderr)
        return 0
    event = payload.get("hook_event_name")
    try:
        directive = handle(payload)
    except Exception as exc:  # fail open; the hook must never wedge the agent loop
        print(f"jev-guard: {event} failed open: {exc}", file=sys.stderr)
        return 0
    if directive:
        print(json.dumps(directive))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
