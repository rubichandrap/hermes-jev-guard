# hermes-jev-guard

Shell hooks that put TypeSafe [Jev](https://typesafe.ai) (a System One model) in front of a
[Hermes Agent](https://github.com/NousResearch/hermes-agent) session: a route hint, a tool-risk
gate, and a done-check. One stdlib-only script, fail-open by default.

- `pre_llm_call` — Jev reads the user message, picks a route and a complexity score; the result is injected as a small hint on the user message.
- `pre_tool_call` — Jev scores `terminal` / `write_file` / `patch` calls for destructive or irreversible risk; high risk escalates to human approval, extreme risk blocks the call.
- `pre_verify` — after a coding turn, Jev checks the final response for overclaiming or unfinished work; a flag sends the agent back to work once.

This is soft routing: Hermes has no hook that swaps the model per turn, so the route hint steers
the running model rather than switching it. Jev returns calibrated probabilities, not truth;
tune the thresholds on your own traffic.

## Install

1. Clone anywhere:

   ```bash
   git clone https://github.com/rubichandrap/hermes-jev-guard ~/hermes-jev-guard
   ```

2. Put `TYPESAFE_API_KEY=...` in `~/.hermes/.env` (or export it), then check the script:

   ```bash
   python3 ~/hermes-jev-guard/jev_guard.py --self-test   # offline logic check
   ```

3. Append the block from [hooks.example.yaml](hooks.example.yaml) to `~/.hermes/config.yaml`.
   The first firing asks for hook consent; `HERMES_ACCEPT_HOOKS=1` or `hooks_auto_accept: true`
   skips the prompt.

4. Dry-run one event:

   ```bash
   hermes hooks test pre_tool_call --for-tool terminal
   ```

## Knobs (environment)

| Var | Default | Meaning |
| --- | --- | --- |
| `JEV_TIMEOUT` | `8` | HTTP timeout, seconds |
| `JEV_APPROVE_AT` | `0.7` | risk probability that escalates to human approval |
| `JEV_BLOCK_AT` | `0.97` | risk probability that blocks the tool call |
| `JEV_VERIFY_AT` | `0.7` | done-check probability that nudges the agent to continue |
| `JEV_MAX_STATE_CHARS` | `12000` | state sent to Jev is clipped to this |

## Notes

- Fail-open: if Jev errors or times out, the agent proceeds and the hook logs to stderr. For the
  tool gate you can opt into `fail_closed: true` in config.
- Payload text (user messages, tool inputs, final responses) is sent to `api.typesafe.ai`.
- Model `jev-latest` (currently `jev-1.13.0`). Pricing and limits: <https://docs.typesafe.ai/models>.

MIT
