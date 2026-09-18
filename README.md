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
| `risk_tools` | `JEV_RISK_TOOLS` | `""` | pipe-separated tools that get a risk score; empty scores every tool |
| `force_lane` | `JEV_FORCE_LANE` | `""` | probability at which a delegating lane blocks the first direct tool call, once; empty keeps lanes advisory |

If Jev errors or times out, the hooks fail open: the agent proceeds and a warning is logged.

## Measurements

Measured on 2026-09-18, one machine, one model (`deepseek/deepseek-v4.1-flash` via commandcode),
Hermes CLI, plugin versus `hermes plugins disable hermes-jev-guard`. Single-turn prompts, no
caching warmup control, n is small — treat these as orders of magnitude, not precise deltas.

Prompts, identical text in both arms:

- `Research three things, in parallel if you can: (a) what TypeSafe System One is, (b) what the Jev model is, (c) what RLCD training is. One line each.`
- `Create sorter.py in this directory with a function that sorts a list of integers, then run it once with a sample list and show the output.`
- `reply with exactly: ping`

| Task (runs: off / on) | Plugin off | Plugin on |
| --- | --- | --- |
| Research three things (3 / 4) | 15s median, 12-16s range; 5 tool calls; in ~13k, out 0.9-1.6k | 44s median, 29-123s range; 6 tool calls; in ~16k, out 3.4-15k; delegation ran in 2 of 4 |
| Create sorter.py, run it, show output (1 / 1) | 12s; 2 tool calls | 18s; 3 tool calls |
| Reply with exactly: `ping` (1 / 1) | 4s; 0 tool calls | 6s; 0 tool calls |

The research gap is mostly the plan *changing the work*, not plugin latency — the slow runs are the
ones where the enforced lane pushed the turn into `delegate_task`. The plugin's own cost per turn,
measured from the flow log:

| Cost | Value |
| --- | --- |
| Trivial turn (`reply with exactly: ping`), plugin off vs on | 5s vs 7s |
| Jev round-trip, per call | p50 825ms (`pre_llm_call`), 781ms (`pre_tool_call`), 774ms (`pre_verify`) |
| Jev time per turn, read-only traffic | 0.88s (one `pre_llm_call`; unscored tools skip Jev entirely) |
| Jev time per turn, with delegation | 1.6-2.5s (extra scored tool calls) |
| Share of wall clock, read-only batch (3 turns) | 2.9% |
| Share of wall clock before `risk_tools` scoping (7 turns, all tools scored) | 17.9% |
| Plan text added to each user message | 396 chars, about 99 tokens |
| Jev input tokens per turn | 37 (trivial) to 1,232 (delegation turn) |
| Jev USD per turn at $0.042/Mtok | $0.000002 - $0.000052 |

So: pennies per thousand turns on Jev, one extra ~0.8s at the start of a turn, and a plan block of
about 99 prompt tokens that prompt caching absorbs.

Decision quality, same session set (from `flow_metrics.py` and the session DB):

| Component | Observed |
| --- | --- |
| `delegate_task` on `lane=none` | blocked on the first attempt (1/1) |
| Advisory `lane` hint, delegating lane | acted on 0/4 times — the model read the plan and did the work itself |
| `force_lane` at 0.9 (one-shot block, then release) | engaged with 3/3: one delegation, two refusals with an explicit reason in the answer |
| Risk gate | 38 scored calls, 0 escalations, 0 blocks, mean risk 0.057 — no real case met yet |
| Done-check | 3 verdicts, all `complete`; 0 nudges and 0 human gates so far |
| Model tier middleware | not exercised (no tier models configured) |

Reproduce:

```bash
python3 flow_metrics.py              # latency, plan/gate distributions, per-turn timeline
hermes plugins disable hermes-jev-guard && hermes chat -q "<prompt>"   # the off arm
hermes plugins enable hermes-jev-guard && hermes chat -q "<prompt>"    # the on arm
```

Known limits: the prompts are ours, the model is one of many, and the delegating runs reward
looking at the answer rather than the clock — a forced delegation cost ~4x wall clock for an answer
of comparable quality, so treat `force_lane` as a way to make the plan *considered*, not as a
speed feature.

## Flow log

Every Jev call appends one JSONL line to the plugin data dir,
`$HERMES_HOME/plugin-data/agent-plugin-hermes-jev-guard-<hash>/jev-flow.jsonl`
(`JEV_LOG` overrides the path): event, latency, state preview, Jev's answers,
thresholds, error. `jev-flow-tui` (separate local repo) renders it.

## Without the plugin

Two ways to keep Jev available when the plugin is off:

- **Automatic (shell hooks)** — `jev_guard.py` also runs standalone: append the block from
  [hooks.example.yaml](hooks.example.yaml) to `~/.hermes/config.yaml`, then dry-run one event with
  `hermes hooks test pre_tool_call --for-tool terminal`. Same three hooks, no middleware (so the
  model tier is ignored), and the plan gate/human gate still work because they are hook logic.
- **On demand (skill + `--ask`)** — `python3 jev_guard.py --ask "refactor the auth module"` prints
  the same plan the hook would inject. [skill/SKILL.md](skill/SKILL.md) teaches the agent when to
  use it and how to call the API for other questions; install it with
  `hermes skills install https://raw.githubusercontent.com/rubichandrap/hermes-jev-guard/main/skill/SKILL.md`.
  A skill cannot enforce anything — no blocking, no approval gate — so it is advice, not guardrails.

## Development

```bash
python3 jev_guard.py --self-test   # offline logic check, no network
hermes plugins doctor . --ci       # manifest + register(ctx) + hook registry
```

Model `jev-latest` (currently `jev-1.13.0`). Pricing and limits:
<https://docs.typesafe.ai/models>.

MIT
