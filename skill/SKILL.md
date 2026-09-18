---
name: jev
description: Use when you want a Jev judgment without the plugin.
---

# Jev (TypeSafe System One) on demand

Jev is a decision model, not a chat model: you send `state` plus typed questions and get back
typed answers and calibrated probabilities. Use it for a second opinion the code can branch on —
routing a request, judging a risk, checking whether work is finished.

Advisory only: this skill cannot block a tool call or prompt the user. Enforcement lives in the
`hermes-jev-guard` plugin (hooks + middleware). With the plugin off, treat Jev's answers as advice
you follow yourself.

## Fast path: one plan for a message

Hermes installs the guard script; ask it to plan a message the same way the plugin's
`pre_llm_call` hook does (route, complexity, lane, model tier):

```bash
python3 ~/.hermes/plugins/hermes-jev-guard/jev_guard.py --ask "refactor the auth module"
printf '%s' "summarize this paper" | python3 ~/.hermes/plugins/hermes-jev-guard/jev_guard.py --ask
```

Output example:

```
Jev plan: lane=worktree_code (p=0.71) - spawn subagents that edit code in their own workspaces
model tier: frontier - strongest available model; hard reasoning or high-stakes work (no tier model configured; stay on the current model)
route: code_task (p=0.88) - write or modify code now Complexity 1.40/2.
Delegate the code edits; each child gets its own workspace, so state paths and constraints in every goal.
```

If the script is not installed, the hooks file inside the guard repo runs the same way.

## Any other question: the HTTP API

One POST, same endpoint the plugin uses. The key is already in the environment as
`TYPESAFE_API_KEY` (Hermes loads it from `$HERMES_HOME/.env` at startup); never print it.

```bash
curl -s https://api.typesafe.ai/v1/systemone \
  -H "Authorization: Bearer $TYPESAFE_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "jev-latest",
       "state": "<the text the judgment is about>",
       "questions": {
         "finished": {"type": "noul",
                      "instructions": "Does this response overclaim or leave work unfinished?",
                      "criteria": {"true": "claims unverified results", "false": "claims match the evidence"}}}}'
```

Three question types: `choice` (pick one of `criteria`, returns `choice`, `probabilities`,
`confidence`), `score` (ordered levels, returns `score`), `noul` (yes/no, returns `noul` 0-1).
Ask several questions in one call — they run in parallel against the same state.

## Reading answers

- Thresholds are yours to set; the plugin defaults are risk `approve_at 0.7` / `block_at 0.97` and
  done-check `verify_at 0.7`. Tune against your own traffic.
- High probability is not truth. Calibration is measured across groups of predictions.
- A probability near 0.5 means the model is unsure — say so instead of guessing.

## Full guardrails instead of advice

For automatic per-turn plans, blocked delegations, and approval gates, use the plugin
(`hermes plugins install rubichandrap/hermes-jev-guard`) or shell-hook mode
(`hooks.example.yaml` in the same repo).
