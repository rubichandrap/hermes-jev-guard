#!/usr/bin/env python3
"""Hermes hooks (and one middleware) that put TypeSafe Jev (System One) in the loop:

  pre_llm_call  -> {"context": ...}                    plan injected into the user message
  pre_tool_call -> {"action": "approve"|"block", ...}  plan gate + risk gate + human gate
  pre_verify    -> {"action": "continue", ...}         done-check verdict
  llm_request   -> {"request": ...}                    model tier from the plan (middleware)

Hermes pipes a JSON payload on stdin and reads one directive JSON back; plugin mode
calls the same functions with keyword payloads. Fail-open: on any error this prints a
note to stderr and outputs nothing, so the agent proceeds. See README.md for the
config.yaml block and env knobs.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"

TIMEOUT = float(os.environ.get("JEV_TIMEOUT", "8"))
APPROVE_AT = float(os.environ.get("JEV_APPROVE_AT", "0.7"))
BLOCK_AT = float(os.environ.get("JEV_BLOCK_AT", "0.97"))
VERIFY_AT = float(os.environ.get("JEV_VERIFY_AT", "0.7"))
MAX_STATE_CHARS = int(os.environ.get("JEV_MAX_STATE_CHARS", "12000"))

# Model tier -> model id for the llm_request middleware. Empty means "leave the model
# alone": the middleware only rewrites a request when that tier has a model configured.
ECONOMY_MODEL = os.environ.get("JEV_ECONOMY_MODEL", "")
STANDARD_MODEL = os.environ.get("JEV_STANDARD_MODEL", "")
FRONTIER_MODEL = os.environ.get("JEV_FRONTIER_MODEL", "")

# One JSONL line per Jev call, read by jev_flow_tui.py. Plugin mode overrides this
# with ctx.state.data_dir; env JEV_LOG wins for both modes.
LOG_PATH = os.environ.get("JEV_LOG") or str(
    Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    / "plugin-data" / "hermes-jev-guard" / "jev-flow.jsonl")

_SETTING_NAMES = ("timeout", "approve_at", "block_at", "verify_at", "max_state_chars",
                  "log_path", "economy_model", "standard_model", "frontier_model")


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

# The execution lanes Hermes actually supports: the running agent alone, or delegate_task
# children for read-only fan-out, code edits, or a separate verification pass.
LANES = {
    "none": "the running agent handles it alone; no subagent",
    "parallel_read": "spawn read-only subagents to gather information in parallel",
    "worktree_code": "spawn subagents that edit code in their own workspaces",
    "review_pass": "after finishing, run a separate verification pass over the result",
}

TIERS = {
    "economy": "cheap, fast model; simple low-stakes work",
    "standard": "balanced model; everyday coding and analysis",
    "frontier": "strongest available model; hard reasoning or high-stakes work",
}

VERDICTS = {
    "complete": "claims match the evidence shown; the work looks finished",
    "verify_more": "work unfinished, unverified, or overclaimed; the agent can continue on its own",
    "ask_human": "more changes are needed and the user must approve them before anything else is edited",
}

LANE_DIRECTIVES = {
    "none": "Do not call delegate_task for this turn; handle it here.",
    "parallel_read": "Delegate the information gathering; keep child goals self-contained and read-only.",
    "worktree_code": "Delegate the code edits; each child gets its own workspace, so state paths and constraints in every goal.",
    "review_pass": "Before you stop, run one separate verification pass over the result.",
}

# Tools the human gate escalates after Jev flags a turn (see on_pre_verify).
GATE_TOOLS = ("write_file", "patch", "delegate_task")

# Per-session plan state; the hooks fire in-process, so a plain dict is enough.
# ponytail: capped, oldest dropped; a gateway running for weeks would otherwise leak.
_STATE: dict[str, dict] = {}
_STATE_LIMIT = 200


def _remember(session: str, **fields) -> None:
    entry = _STATE.setdefault(session, {})
    entry.update(fields)
    while len(_STATE) > _STATE_LIMIT:
        _STATE.pop(next(iter(_STATE)))


def _plan(session: str) -> dict:
    return _STATE.get(session) or {}


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


def log_call(event: str, ms: float, ok: bool, state=None, answers=None, error=None,
             session: str = "") -> None:
    """Append one JSONL record to LOG_PATH. Never raises: the hook must not wedge.

    ponytail: plain append, no rotation - one line per Jev call; rotate when the file
    grows enough to matter.
    """
    try:
        path = Path(LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.time(), "event": event, "ms": round(ms, 1), "ok": ok, "session": session,
            "state_chars": len(state or ""), "state_head": (state or "")[:200],
            "answers": answers, "error": error,
            "thresholds": {"approve_at": APPROVE_AT, "block_at": BLOCK_AT, "verify_at": VERIFY_AT},
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def ask(state, questions: dict, event: str = "", session: str = "") -> dict:
    """POST one question set to Jev; log the call whether it succeeds or fails."""
    body = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            answers = json.loads(resp.read().decode("utf-8"))["answers"]
    except Exception as exc:
        log_call(event, (time.monotonic() - started) * 1000, False, state,
                 error=f"{type(exc).__name__}: {exc}", session=session)
        raise
    log_call(event, (time.monotonic() - started) * 1000, True, state, answers=answers,
             session=session)
    return answers


def clip(obj, limit: int | None = None) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return text[: limit or MAX_STATE_CHARS]


def _event_field(payload: dict, name: str, default=None):
    """Event-specific kwargs arrive under `extra`; accept a top-level copy too."""
    extra = payload.get("extra") or {}
    value = extra.get(name)
    return payload.get(name) if value is None else value


def _session(payload: dict) -> str:
    return payload.get("session_id") or payload.get("task_id") or ""


def _pick(answers: dict, key: str) -> tuple[str, float | None]:
    """(choice, probability of that choice) for a Choice answer."""
    block = answers.get(key) or {}
    chosen = block.get("choice", "")
    p = (block.get("probabilities") or {}).get(chosen)
    return chosen, p if isinstance(p, (int, float)) else None


def _label(head: str, table: dict, key: str, p: float | None = None) -> str:
    return head + (f" (p={p:.2f})" if p is not None else "") + f" - {table.get(key, '')}"


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
        "lane": {
            "type": "choice",
            "instructions": "How should this request be executed: by the running agent alone, or "
                            "with subagents? A subagent starts from scratch and cannot see the "
                            "conversation, so choose one only when the work splits into "
                            "self-contained pieces.",
            "criteria": LANES,
        },
        "tier": {
            "type": "choice",
            "instructions": "Which model class fits this request?",
            "criteria": TIERS,
        },
    }, "pre_llm_call", _session(payload))
    lane, lane_p = _pick(answers, "lane")
    tier, _ = _pick(answers, "tier")
    route, route_p = _pick(answers, "route")
    score = (answers.get("complexity") or {}).get("score")
    if lane not in LANES or tier not in TIERS:
        return {}
    _remember(_session(payload), lane=lane, tier=tier, user_message=message)

    lines = [
        _label(f"Jev plan: lane={lane}", LANES, lane, lane_p),
        _label(f"model tier: {tier}", TIERS, tier)
        + ("" if tier in tier_models() else " (no tier model configured; stay on the current model)"),
        _label(f"route: {route}", ROUTES, route, route_p)
        + (f" Complexity {score:.2f}/2." if isinstance(score, (int, float)) else ""),
        LANE_DIRECTIVES[lane],
    ]
    return {"context": "\n".join(lines)}


def on_pre_tool_call(payload: dict, ask=ask) -> dict:
    session = _session(payload)
    plan = _plan(session)
    tool = (payload.get("tool_name") or "").strip()

    if tool == "delegate_task" and plan.get("lane") == "none":
        return {"action": "block",
                "message": "Jev plan for this turn is lane=none (no subagent). Do the work here, "
                           "or say why the plan is wrong."}

    if tool in GATE_TOOLS and plan.get("pending_human_gate"):
        _remember(session, pending_human_gate=False)  # one gate per flagged turn
        return {"action": "approve",
                "message": f"Jev flagged this turn's result for the user: {tool} may only run "
                           "with your approval."}

    state = {"tool": tool,
             "input": payload.get("tool_input") or payload.get("args") or {},
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
    }, "pre_tool_call", session)["risk"]["noul"]
    if risk >= BLOCK_AT:
        return {"action": "block",
                "message": f"Jev risk {risk:.2f}: blocked; rewrite as a safer, reversible step."}
    if risk >= APPROVE_AT:
        return {"action": "approve",
                "message": f"Jev risk {risk:.2f}: ask the user before running this."}
    return {}


def on_pre_verify(payload: dict, ask=ask) -> dict:
    response = (_event_field(payload, "final_response") or "").strip()
    if not response:
        return {}
    session = _session(payload)
    state = {"user_message": _plan(session).get("user_message", ""),
             "final_response": response,
             "changed_paths": _event_field(payload, "changed_paths") or []}
    verdict = ask(clip(state), {
        "verdict": {
            "type": "choice",
            "instructions": "The agent is about to finish. Is this turn's work complete, or does "
                            "it need more work? If more work is needed: can the agent continue on "
                            "its own, or must the user approve further changes first?",
            "criteria": VERDICTS,
        },
    }, "pre_verify", session)["verdict"]
    chosen = verdict.get("choice", "")
    if chosen == "ask_human":
        _remember(session, pending_human_gate=True)  # arms the tool-level approval gate
    if int(_event_field(payload, "attempt") or 0) >= 1:
        return {}  # one nudge per turn; Hermes caps nudges at max_verify_nudges anyway
    if chosen == "ask_human":
        return {"action": "continue",
                "message": "Jev done-check: do not change anything else yet. Tell the user what "
                           "still needs doing and why, then let them decide; any further edit or "
                           "delegation now hits the approval gate."}
    if chosen == "verify_more":
        return {"action": "continue",
                "message": "Jev done-check flagged this (fix it now, do not ask). Show real "
                           "evidence for each claim, or finish the remaining work before stopping."}
    return {}


def on_llm_request(**kwargs):
    """llm_request middleware: swap the model for the plan's tier, when configured.

    Opt-in - an empty tier model makes this a no-op. Replacing the model mid-conversation
    costs the prompt cache, so configure a tier map only when the routing win is worth it.
    """
    request = kwargs.get("request")
    if not isinstance(request, dict):
        return None
    tier = _plan(kwargs.get("session_id") or kwargs.get("task_id") or "").get("tier")
    target = tier_models().get(tier or "")
    if not target or request.get("model") == target:
        return None
    updated = dict(request)
    updated["model"] = target
    return {"request": updated, "source": "hermes-jev-guard", "reason": f"jev model tier {tier}"}


def tier_models() -> dict:
    return {tier: globals()[f"{tier.upper()}_MODEL"] for tier in TIERS
            if globals()[f"{tier.upper()}_MODEL"]}


HANDLERS = {
    "pre_llm_call": on_pre_llm_call,
    "pre_tool_call": on_pre_tool_call,
    "pre_verify": on_pre_verify,
}


def handle(payload: dict) -> dict:
    handler = HANDLERS.get(payload.get("hook_event_name") or "")
    return handler(payload) if handler else {}


def _scripted_ask(route="deep_reasoning", complexity=1.5, risk=0.0,
                  lane="none", tier="standard", verdict="complete"):
    """Deterministic stand-in for ask() so the decision logic is testable offline."""
    def fake(state, questions, event=None, session=None):
        out = {}
        if "route" in questions:
            out["route"] = {"type": "choice", "choice": route,
                            "probabilities": {route: 0.9}, "confidence": 0.9}
        if "complexity" in questions:
            out["complexity"] = {"type": "score", "score": complexity}
        if "lane" in questions:
            out["lane"] = {"type": "choice", "choice": lane,
                           "probabilities": {lane: 0.8}, "confidence": 0.8}
        if "tier" in questions:
            out["tier"] = {"type": "choice", "choice": tier,
                           "probabilities": {tier: 0.7}, "confidence": 0.7}
        if "risk" in questions:
            out["risk"] = {"type": "noul", "noul": risk}
        if "verdict" in questions:
            out["verdict"] = {"type": "choice", "choice": verdict,
                              "probabilities": {verdict: 0.85}, "confidence": 0.85}
        return out
    return fake


def plan_for(text: str, ask=ask) -> str:
    """One Jev plan for a message; the text the pre_llm_call hook injects (CLI: --ask)."""
    return on_pre_llm_call({"session_id": "", "extra": {"user_message": text}},
                           ask=ask).get("context") or "(no plan: Jev returned nothing)"


def self_test() -> int:
    configure(approve_at=0.7, block_at=0.97, verify_at=0.7,
              economy_model="", standard_model="", frontier_model="")  # deterministic
    _STATE.clear()

    hint = on_pre_llm_call({"session_id": "s0", "extra": {"user_message": "design a queue"}},
                           ask=_scripted_ask())["context"]
    assert "lane=none" in hint and "model tier: standard" in hint and "route: deep_reasoning" in hint, hint
    assert "no tier model configured" in hint, hint  # no swap mapped: tell the model to stay put
    assert "Do not call delegate_task" in hint, hint
    assert on_pre_llm_call({"extra": {"user_message": "  "}}, ask=_scripted_ask()) == {}

    # plan gate: an explicit lane lets delegate_task through, lane=none blocks it
    on_pre_llm_call({"session_id": "s1", "extra": {"user_message": "fix the parser"}},
                    ask=_scripted_ask(lane="worktree_code", tier="frontier"))
    assert on_pre_tool_call({"session_id": "s1", "tool_name": "delegate_task"},
                            ask=_scripted_ask(risk=0.1)) == {}
    on_pre_llm_call({"session_id": "s2", "extra": {"user_message": "say hi"}},
                    ask=_scripted_ask(lane="none"))
    blocked = on_pre_tool_call({"session_id": "s2", "tool_name": "delegate_task"},
                               ask=_scripted_ask(risk=0.1))
    assert blocked["action"] == "block" and "lane=none" in blocked["message"], blocked

    # risk gate unchanged
    tool_payload = {"session_id": "s3", "tool_name": "terminal", "tool_input": {"command": "ls"}}
    assert on_pre_tool_call(tool_payload, ask=_scripted_ask(risk=0.1)) == {}
    assert on_pre_tool_call(tool_payload, ask=_scripted_ask(risk=0.8))["action"] == "approve"
    assert on_pre_tool_call(tool_payload, ask=_scripted_ask(risk=0.99))["action"] == "block"

    # done-check: complete / verify_more / ask_human
    verify_payload = {"session_id": "s4", "extra": {"final_response": "Done. All tests pass.",
                                                    "changed_paths": ["a.py"]}}
    on_pre_llm_call({"session_id": "s4", "extra": {"user_message": "add a flag"}},
                    ask=_scripted_ask())
    assert on_pre_verify(verify_payload, ask=_scripted_ask(verdict="complete")) == {}
    assert on_pre_verify(verify_payload, ask=_scripted_ask(verdict="verify_more"))["action"] == "continue"
    gate = on_pre_verify(verify_payload, ask=_scripted_ask(verdict="ask_human"))
    assert gate["action"] == "continue" and "let them decide" in gate["message"], gate
    # the flag arms a real approval prompt on the next mutating tool, then disarms
    assert on_pre_tool_call({"session_id": "s4", "tool_name": "patch"},
                            ask=_scripted_ask())["action"] == "approve"
    assert on_pre_tool_call({"session_id": "s4", "tool_name": "patch"}, ask=_scripted_ask(risk=0.1)) == {}
    # one nudge per turn: attempt=1 stays quiet
    assert on_pre_verify({**verify_payload,
                          "extra": {**verify_payload["extra"], "attempt": 1}},
                         ask=_scripted_ask(verdict="ask_human")) == {}

    # model tier middleware: no-op without a map, rewrites with one
    assert on_llm_request(request={"model": "x"}, session_id="s1") is None
    configure(frontier_model="big-model")
    out = on_llm_request(request={"model": "x"}, session_id="s1")
    assert out["request"]["model"] == "big-model" and "frontier" in out["reason"], out
    assert on_llm_request(request={"model": "x"}, session_id="unknown") is None
    configure(frontier_model="")

    assert handle({"hook_event_name": "unknown_event"}) == {}
    assert plan_for("refactor the parser", ask=_scripted_ask(lane="worktree_code")).startswith("Jev plan: lane=worktree_code")

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        configure(log_path=str(Path(tmp) / "flow.jsonl"))
        log_call("self_test", 1.5, True, "state", {"a": 1})
        log_call("self_test", 2.5, False, error="boom")
        rows = [json.loads(x) for x in
                (Path(tmp) / "flow.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[0]["event"] == "self_test" and rows[0]["ok"] is True and rows[0]["ms"] == 1.5
        assert rows[1]["ok"] is False and "boom" in rows[1]["error"]

    print("self-test OK")
    return 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    if "--ask" in argv:
        text = " ".join(argv[argv.index("--ask") + 1:]).strip() or sys.stdin.read().strip()
        if not text:
            print("usage: jev_guard.py --ask 'message'   (or pipe the message on stdin)",
                  file=sys.stderr)
            return 2
        try:
            print(plan_for(text))
        except Exception as exc:
            print(f"jev-guard: ask failed: {exc}", file=sys.stderr)
            return 1
        return 0
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
