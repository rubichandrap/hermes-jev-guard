# hermes-jev-guard

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that puts TypeSafe
[Jev](https://typesafe.ai) (a System One model) in front of every turn: a plan for how the turn
runs, a tool-risk gate, a plan gate, a done-check with a human gate, and a code-quality gate on
the files the turn edited. Stdlib only, fail-open by default.

## The loop, and when it fires

Each step names its trigger, so a turn that triggers nothing costs nothing. Everything below hangs
off Hermes hook points (see [Without the plugin](#without-the-plugin) for the shell-hook path).

1. **Plan (`pre_llm_call`)** — fires once per user turn, on any non-empty message, before the model
   sees it. Jev answers four questions together — route, complexity, execution **lane**, and
   **model tier** — and the plan is injected as a short block on the user message:
   - lanes: `none` (the running agent does it), `parallel_read` (read-only subagents),
     `worktree_code` (subagents edit code), `review_pass` (a separate verification pass at the end).
   - tiers: `economy` / `standard` / `frontier`.
2. **Execution** — the agent works the turn with the plan in context. Three plan decisions act here,
   and none of them is a Jev call of its own:
   - a `delegate_task` call while the plan says `lane=none` is **blocked**;
   - `force_lane` blocks the first direct tool call of a confident delegating lane, once;
   - the model tier is applied by the `llm_request` middleware when that tier has a model
     configured. Empty tier models leave the model alone.
3. **Risk gate (`pre_tool_call`)** — fires per tool call, for the tools named in `risk_tools` only
   (an empty list scores every tool). Jev answers one question — destructive or irreversible? — and
   the two thresholds decide: at `approve_at` the call is escalated to the human approval prompt, at
   `block_at` it is blocked, below both it passes. A tool outside the list never reaches Jev.
4. **Done-check (`pre_verify`)** — fires only on turns where the agent edited files, and only once
   per turn (one nudge, then it stays quiet). Jev sees the user message, the final response, the
   changed paths, and the text of the files edited this turn, and picks:
   - `complete` — the turn finishes;
   - `verify_more` — the agent is sent back to work immediately;
   - `ask_human` — the agent is told to stop editing and explain, and the **next**
     `write_file` / `patch` / `delegate_task` is escalated to the real approval prompt, so
     refusing it stops the work. One gate per flagged turn.
   - a second question rides the same call: `refactor` over `none` / `minor` / `structural`.
     `minor` or `structural` at or above `refactor_at` confidence sends the agent back to clean the
     shape up before it stops; below it the plugin refuses to order a refactor and hands the
     decision to the user instead (approval gate armed). Skipped when no edited file could be read.

Jev fails open: an error or a timeout logs a warning to stderr and the agent proceeds. It returns
calibrated probabilities, not truth — tune the thresholds on your own traffic.

Two costs worth knowing: the plan block rides every user message (about 99 tokens, absorbed by
prompt caching), and a model swap mid-conversation breaks the prompt cache, which is why tier
models are opt-in.

## Install

```bash
hermes plugins install rubichandrap/hermes-jev-guard
```

Install prompts to enable the plugin and to store `TYPESAFE_API_KEY` in `~/.hermes/.env` when it
is missing. `/plugins` shows it loaded; the hooks fire from then on.

Payload text (user messages, tool inputs, final responses, and the text of files edited during a
turn) is sent to `api.typesafe.ai` — enable this only on sessions whose content may leave the
machine.

## Settings

Stored under `plugins.entries.hermes-jev-guard.settings` in `config.yaml`; the matching `JEV_*`
environment variables are the fallback defaults.

| Setting | Env fallback | Default | Meaning |
| --- | --- | --- | --- |
| `timeout` | `JEV_TIMEOUT` | `8` | HTTP timeout, seconds |
| `approve_at` | `JEV_APPROVE_AT` | `0.7` | risk probability that escalates to human approval |
| `block_at` | `JEV_BLOCK_AT` | `0.97` | risk probability that blocks the tool call |
| `verify_at` | `JEV_VERIFY_AT` | `0.7` | done-check probability that nudges the agent to continue |
| `refactor_at` | `JEV_REFACTOR_AT` | `0.7` | confidence the code-quality gate needs to order a refactor; below it the user decides |
| `code_chars` | `JEV_CODE_CHARS` | `8000` | edited-file text sent with the done-check, in characters |
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
- `Fix calc.py so divide() handles zero, then confirm the test suite passes.`
- `Add a median() function to /tmp/jev-hard/on/calc.py. Handle empty lists and even-length lists correctly, then verify it.` (the off arm gets its own path; everything else is identical)

| Task (runs: off / on) | Plugin off | Plugin on |
| --- | --- | --- |
| Research three things (3 / 4) | 15s median, 12-16s range; 5 tool calls; in ~13k, out 0.9-1.6k | 44s median, 29-123s range; 6 tool calls; in ~16k, out 3.4-15k; delegation ran in 2 of 4 |
| Create sorter.py, run it, show output (1 / 1) | 12s; 2 tool calls | 18s; 3 tool calls |
| Reply with exactly: `ping` (1 / 1) | 4s; 0 tool calls | 6s; 0 tool calls |
| Fix `divide()` to handle zero, confirm the suite passes (1 / 1) | 2m27s; 41 tool calls; artifact correct | 1m11s; 23 tool calls; artifact correct |
| Add `median()` handling empty and even-length lists (1 / 1) | 48s; 13 tool calls; artifact correct | 30s; 5 tool calls; artifact correct |

The two artifact rows are the accuracy check: after each turn we imported the written file and ran
our own cases (`median([1,2,3]) == 2`, `[1,2,3,4] == 2.5`, `[5] == 5`, unsorted input, empty list
raises). Both arms passed both tasks, and both claimed verification they had actually performed, so
at this size the plugin bought an equal answer for fewer tool calls, not a more accurate one. Jev's
risk scores on that work stayed at 0.01-0.17, and the done-check returned `complete` both times —
correctly, since the code had been run.

Worth knowing before designing more tests: `hermes chat -q` has no user to approve anything, so
Hermes itself blocks commands its own scanner calls dangerous. The risk gate and the human gate can
only be exercised in an interactive session (or by pointing the prompt at something Hermes leaves
alone), and a prompt with a path spelled out is required — a relative "calc.py" can resolve to the
agent's own working directory instead of the shell's.

The research gap is mostly the plan *changing the work*, not plugin latency — the slow runs are the
ones where the enforced lane pushed the turn into `delegate_task`. The plugin's own cost per turn,
measured from the flow log:

| Cost | Value |
| --- | --- |
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
| Code-quality gate | 4 live probes: `refactor=none` at 0.99 confidence on a clean file, `refactor=minor` at 0.42-0.88 on duplicated or tangled files — the unsure branch (hand the decision to the user) fired for real at conf 0.45 |
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
thresholds, error.

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
