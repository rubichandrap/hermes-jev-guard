#!/usr/bin/env python3
"""Metrics for the hermes-jev-guard flow log.

Reads the JSONL the plugin writes and prints whether the flow is doing its job:
latency per hook, plan distribution, gate decisions, and the per-turn timeline.

Usage:
  python3 flow_metrics.py [--log PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path


def default_log_path() -> str:
    if os.environ.get("JEV_LOG"):
        return os.environ["JEV_LOG"]
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    candidates = list((Path(home) / "plugin-data").glob("*jev-guard*/jev-flow.jsonl"))
    if candidates:
        return str(max(candidates, key=lambda p: p.stat().st_mtime))
    return str(Path(home) / "plugin-data" / "hermes-jev-guard" / "jev-flow.jsonl")


def load(path: str) -> list[dict]:
    rows = []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def chosen(row: dict, key: str):
    """Selected value for one question in a call, e.g. chosen(row, 'lane')."""
    block = (row.get("answers") or {}).get(key) or {}
    return block.get("choice") if isinstance(block, dict) else None


def prob(row: dict, key: str) -> float | None:
    """Probability Jev gave to its own answer (confidence proxy, not correctness)."""
    block = (row.get("answers") or {}).get(key) or {}
    if not isinstance(block, dict):
        return None
    p = (block.get("probabilities") or {}).get(block.get("choice"))
    return p if isinstance(p, (int, float)) else None


def noul(row: dict, key: str) -> float | None:
    block = (row.get("answers") or {}).get(key) or {}
    value = block.get("noul") if isinstance(block, dict) else None
    return value if isinstance(value, (int, float)) else None


def risk_outcome(row: dict) -> str:
    risk = noul(row, "risk")
    limits = row.get("thresholds") or {}
    if risk is None:
        return "?"
    if risk >= (limits.get("block_at") or 1):
        return "block"
    if risk >= (limits.get("approve_at") or 1):
        return "approve"
    return "pass"


def summarize(rows: list[dict]) -> dict:
    by_event: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_event[row.get("event") or "?"].append(row)

    out: dict = {"calls": len(rows), "events": {}, "gates": Counter(), "turns": []}
    for event, group in sorted(by_event.items()):
        ms = [r["ms"] for r in group if isinstance(r.get("ms"), (int, float))]
        out["events"][event] = {
            "n": len(group),
            "failed": sum(1 for r in group if not r.get("ok")),
            "ms_p50": round(statistics.median(ms), 1) if ms else None,
            "ms_p95": round(sorted(ms)[int(len(ms) * 0.95) - 1], 1) if len(ms) > 1 else (ms[0] if ms else None),
            "ms_max": round(max(ms), 1) if ms else None,
        }
        if event == "pre_llm_call":
            out["lanes"] = Counter(chosen(r, "lane") for r in group)
            out["tiers"] = Counter(chosen(r, "tier") for r in group)
            out["routes"] = Counter(chosen(r, "route") for r in group)
            ps = [p for p in (prob(r, "lane") for r in group) if p is not None]
            out["lane_prob_mean"] = round(statistics.mean(ps), 2) if ps else None
        if event == "pre_tool_call":
            out["risk"] = Counter(risk_outcome(r) for r in group)
            risks = [r for r in group if noul(r, "risk") is not None]
            out["risk_mean"] = round(statistics.mean(noul(r, "risk") for r in risks), 3) if risks else None
            out["risk_ms"] = round(statistics.mean(r["ms"] for r in risks), 1) if risks else None
        if event == "pre_verify":
            out["verdicts"] = Counter(chosen(r, "verdict") for r in group)

    # Per-session timeline: plan -> tool gates -> verdict.
    sessions: dict[str, dict] = {}
    for row in rows:
        sid = row.get("session") or "?"
        turn = sessions.setdefault(sid, {"lane": None, "tier": None, "gates": Counter(),
                                         "verdicts": Counter(), "calls": 0, "first": row.get("ts")})
        turn["calls"] += 1
        if row.get("event") == "pre_llm_call":
            turn["lane"], turn["tier"] = chosen(row, "lane"), chosen(row, "tier")
        elif row.get("event") == "pre_tool_call":
            turn["gates"][risk_outcome(row)] += 1
        elif row.get("event") == "pre_verify":
            turn["verdicts"][chosen(row, "verdict")] += 1
    out["turns"] = [dict(session=k, **v) for k, v in sessions.items()]
    return out


def db_effectiveness(log_rows: list[dict], db_path: str) -> dict | None:
    """Join the flow log with Hermes' session DB: did the agent act on the plan?

    Enforcement hit  - plan said lane=none and a delegation was attempted anyway (the gate blocked it).
    Soft hint hit    - plan said a delegating lane and delegate_task actually ran.
    """
    import sqlite3

    plans = {r.get("session"): chosen(r, "lane") for r in log_rows
             if r.get("event") == "pre_llm_call" and r.get("session")}
    if not plans:
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    hits = {"enforced": 0, "enforce_sessions": 0, "soft": 0, "soft_sessions": 0, "sessions": {}}
    for sid, lane in plans.items():
        tools = [t for (t,) in con.execute(
            "select tool_name from messages where session_id=? and tool_name is not null and tool_name!=''",
            (sid,))]
        blocked = con.execute(
            "select count(*) from messages where session_id=? and content like '%lane=none (no subagent)%'",
            (sid,)).fetchone()[0]
        delegated = "delegate_task" in tools
        if lane == "none":
            hits["enforce_sessions"] += 1
            hits["enforced"] += 1 if (blocked or delegated) else 0
        elif lane in ("parallel_read", "worktree_code", "review_pass"):
            hits["soft_sessions"] += 1
            hits["soft"] += 1 if delegated else 0
        hits["sessions"][sid] = {"lane": lane, "delegated": delegated, "blocked": bool(blocked)}
    con.close()
    return hits


def report(path: str, db_path: str | None = None) -> int:
    rows = load(path)
    if not rows:
        print(f"no calls in {path}")
        return 1
    s = summarize(rows)
    print(f"log: {path}")
    print(f"calls: {s['calls']}   sessions: {len(s['turns'])}")
    print("\nlatency (ms, Jev call round-trip)")
    for event, e in s["events"].items():
        print(f"  {event:14} n={e['n']:4} fail={e['failed']}  p50={e['ms_p50']}  p95={e['ms_p95']}  max={e['ms_max']}")
    if "lanes" in s:
        print(f"\nplan   lanes={dict(s['lanes'])}  tiers={dict(s['tiers'])}")
        print(f"       routes={dict(s['routes'])}  mean p(chosen lane)={s['lane_prob_mean']}")
    if "risk" in s:
        print(f"gates  {dict(s['risk'])}  mean risk={s['risk_mean']}")
    if "verdicts" in s:
        print(f"done   {dict(s['verdicts'])}")
    print("\nturns")
    for turn in sorted(s["turns"], key=lambda t: t["first"] or 0):
        gates = "+".join(f"{k}:{v}" for k, v in turn["gates"].items()) or "-"
        verdicts = "+".join(f"{k}:{v}" for k, v in turn["verdicts"].items()) or "-"
        head = (turn["lane"] or "?") + "/" + (turn["tier"] or "?")
        stamp = time.strftime("%H:%M:%S", time.localtime(turn["first"])) if turn["first"] else "?"
        print(f"  {stamp}  {head:24} calls={turn['calls']:3} gates={gates:20} verdict={verdicts}")

    if db_path:
        hits = db_effectiveness(rows, db_path)
        if hits:
            print("\neffectiveness (plan vs what happened, from state.db)")
            print(f"  enforcement  {hits['enforced']}/{hits['enforce_sessions']} lane=none sessions "
                  "saw a delegation attempt or a block")
            print(f"  soft hint    {hits['soft']}/{hits['soft_sessions']} delegating-lane sessions "
                  "actually delegated")
    return 0


def default_db_path() -> str:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return str(Path(home) / "state.db")


def self_test() -> int:
    rows = [
        {"event": "pre_llm_call", "ok": True, "ms": 800.0, "session": "a",
         "answers": {"lane": {"choice": "none", "probabilities": {"none": 0.9}},
                     "tier": {"choice": "economy"}, "route": {"choice": "direct_answer"}}},
        {"event": "pre_tool_call", "ok": True, "ms": 900.0, "session": "a",
         "answers": {"risk": {"noul": 0.8}}, "thresholds": {"approve_at": 0.7, "block_at": 0.97}},
        {"event": "pre_verify", "ok": False, "ms": 100.0, "session": "a",
         "answers": {"verdict": {"choice": "ask_human"}}, "error": "TimeoutError"},
    ]
    s = summarize(rows)
    assert s["lanes"]["none"] == 1 and s["tiers"]["economy"] == 1
    assert s["risk"]["approve"] == 1 and s["verdicts"]["ask_human"] == 1
    assert s["events"]["pre_verify"]["failed"] == 1
    assert s["turns"][0]["lane"] == "none" and s["turns"][0]["gates"]["approve"] == 1
    print("self-test OK")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", default=default_log_path())
    parser.add_argument("--db", default=default_db_path(),
                        help="Hermes state.db for the plan-vs-outcome join; '' to skip")
    parser.add_argument("--json", action="store_true", help="dump the summary as JSON")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.json:
        print(json.dumps(summarize(load(args.log)), default=str, indent=2))
        return 0
    return report(args.log, args.db if args.db and Path(args.db).exists() else None)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
