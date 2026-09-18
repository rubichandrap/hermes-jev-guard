# hermes-jev-guard

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that puts TypeSafe
[Jev](https://typesafe.ai) (a System One model) in front of every turn: a plan for how the turn
runs, a tool-risk gate, a plan gate, and a done-check with a human gate. Stdlib only, fail-open
by default.

## The loop

1. **Plan (`pre_llm_call`)** — Jev reads the user message once and answers four questions: route,
   complexity, execution **lane**, and **model tier**. The plan is injected as a short block on
   the user message:
   - lanes: `none` (the running agent does it), `parallel_read` (read-only subagents),
     `worktree_code` (subagents edit code), `review_pass` (a separate verification pass at the end).
   - tiers: `economy` / `standard` / `frontier`.
2. **Execution** — the agent works the turn with the plan in context. Two plan decisions are
   enforced, not just suggested:
   - a `delegate_task` call while the plan says `lane=none` is **blocked**;
   - the model tier is applied by the `llm_request` middleware when that tier has a model
     configured (see Settings). Empty tier models leave the model alone.
3. **Done-check (`pre_verify`)** — after a coding turn, Jev sees the user message, the final
   response, and the changed paths, and picks:
   - `complete` — the turn finishes;
   - `verify_more` — the agent is sent back to work immediately (one nudge per turn);
   - `ask_human` — the agent is told to stop editing and explain, and the **next**
     `write_file` / `patch` / `delegate_task` is escalated to the real approval prompt, so
     refusing it stops the work. One gate per flagged turn.
4. **Risk gate (`pre_tool_call`)** — every tool call is scored for destructive or irreversible
   risk: probability ≥ `approve_at` escalates to human approval, ≥ `block_at` blocks it.

Scope limits worth knowing: the done-check only fires on turns where the agent edited code
(that is when Hermes runs `pre_verify`), and the model tier only takes effect when you map tiers
to models — Hermes has no other per-turn model switch, and a mid-conversation model swap costs
the prompt cache. Jev returns calibrated probabilities, not truth; tune the thresholds on your
own traffic.

## Install

```bash
hermes plugins install rubichandrap/hermes-jev-guard
```

Install prompts to enable the plugin and to store `TYPESAFE_API_KEY` in `~/.hermes/.env` when it
is missing. `/plugins` shows it loaded; the hooks fire from then on.

Payload text (user messages, tool inputs, final responses) is sent to `api.typesafe.ai` — enable
this only on sessions whose content may leave the machine.

## Settings

Stored under `plugins.entries.hermes-jev-guard.settings` in `config.yaml`; the matching `JEV_*`
environment variables are the fallback defaults.

| Setting | Env fallback | Default | Meaning |
| --- | --- | --- | --- |
| `timeout` | `JEV_TIMEOUT` | `8` | HTTP timeout, seconds |
| `approve_at` | `JEV_APPROVE_AT` | `0.7` | risk probability that escalates to human approval |
| `block_at` | `JEV_BLOCK_AT` | `0.97` | risk probability that blocks the tool call |
| `verify_at` | `JEV_VERIFY_AT` | `0.7` | done-check probability that nudges the agent to continue |
| `max_state_chars` | `JEV_MAX_STATE_CHARS` | `12000` | state sent to Jev is clipped to this |
| `economy_model` | `JEV_ECONOMY_MODEL` | `""` | model id for the economy tier; empty disables the swap |
| `standard_model` | `JEV_STANDARD_MODEL` | `""` | model id for the standard tier |
| `frontier_model` | `JEV_FRONTIER_MODEL` | `""` | model id for the frontier tier |

If Jev errors or times out, the hooks fail open: the agent proceeds and a warning is logged.

## Flow log

Every Jev call appends one JSONL line to the plugin data dir,
`$HERMES_HOME/plugin-data/agent-plugin-hermes-jev-guard-<hash>/jev-flow.jsonl`
(`JEV_LOG` overrides the path): event, latency, state preview, Jev's answers,
thresholds, error. `jev-flow-tui` (separate local repo) renders it.

## Shell-hook mode (alternative, no plugin)

`jev_guard.py` also runs standalone. Append the block from
[hooks.example.yaml](hooks.example.yaml) to `~/.hermes/config.yaml`, then dry-run one event with
`hermes hooks test pre_tool_call --for-tool terminal`. Standalone mode has no middleware, so the
model tier is ignored there.

## Development

```bash
python3 jev_guard.py --self-test   # offline logic check, no network
hermes plugins doctor . --ci       # manifest + register(ctx) + hook registry
```

Model `jev-latest` (currently `jev-1.13.0`). Pricing and limits:
<https://docs.typesafe.ai/models>.

MIT
