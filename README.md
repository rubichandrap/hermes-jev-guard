# hermes-jev-guard

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that puts TypeSafe
[Jev](https://typesafe.ai) (a System One model) in front of every session: a route hint, a
tool-risk gate, and a done-check. Stdlib only, fail-open by default.

- `pre_llm_call` — Jev reads the user message, picks a route and a complexity score; the result is injected as a small hint on the user message.
- `pre_tool_call` — Jev scores `terminal` / `write_file` / `patch` calls for destructive or irreversible risk; probability ≥ `approve_at` escalates to human approval, ≥ `block_at` blocks the call.
- `pre_verify` — after a coding turn, Jev checks the final response for overclaiming or unfinished work; a flag sends the agent back to work once.

Soft routing: Hermes has no hook that swaps the model per turn, so the route hint steers the
running model rather than switching it. Jev returns calibrated probabilities, not truth; tune the
thresholds on your own traffic.

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

If Jev errors or times out, the hooks fail open: the agent proceeds and a warning is logged.

## Shell-hook mode (alternative, no plugin)

`jev_guard.py` also runs standalone. Append the block from
[hooks.example.yaml](hooks.example.yaml) to `~/.hermes/config.yaml`, then dry-run one event with
`hermes hooks test pre_tool_call --for-tool terminal`.

## Development

```bash
python3 jev_guard.py --self-test   # offline logic check, no network
hermes plugins doctor . --ci       # manifest + register(ctx) + hook registry
```

Model `jev-latest` (currently `jev-1.13.0`). Pricing and limits:
<https://docs.typesafe.ai/models>.

MIT
