# Architecture

## Components
| Where | Component | Role |
|---|---|---|
| Dispatcher box | userspace `tailscaled` (`dispatcher/tailscale/`) | tailnet access without root/TUN; SOCKS5 on `127.0.0.1:10555` (configurable); watchdog restarts it only after >5 min control-plane failure |
| Dispatcher box | `dispatcher/bin/mac_wait.sh` | fallback poller for Mac jobs (GET only) |
| Dispatcher box | `dispatcher/direct_ssh_client.py` + `bin/job_wait.sh` | SSH-mode job runner for Linux hosts |
| Dispatcher box | `dispatcher/relay/relay_via_host.sh` | optional fallback path to a Mac bridge through another tailnet host |
| Mac | `bridge/codex_bridge.py` + `agent_adapters.py` (launchd, KeepAlive) | HTTP job API, runs the CLIs, journals jobs, pushes terminal webhooks |
| Bot platform | webhook-triggered routine | receives the push and wakes the bot |

## Two modes
**HTTP bridge (macOS).** The bot POSTs `/jobs`; the bridge queues the job on a bounded pool (default 4 workers,
shared by both backends), runs `codex exec --json ...` or `claude -p --output-format json ...` in the fixed workspace,
parses the event stream, and journals the result in `jobs.json`. On every terminal state it pushes a webhook from a
daemon thread, so the bot does not need a live connection or a polling loop.

**SSH mode (Ubuntu/Linux).** Nothing runs on the host between jobs. `direct_ssh_client.py submit` writes the job
record and forks a detached worker on the box; the worker SSHes in through `tailscale nc`, runs the CLI in a login
zsh (`codex exec --json` or `claude -p --output-format stream-json`), parses the output and rewrites the job record.
If the request carried `wake`, the worker pushes the webhook after writing the terminal state.

## Job lifecycle
```
queued ──▶ running ──▶ completed        (exit 0, no error events)
                   ├──▶ failed           (non-zero exit, error/turn.failed event, transport error)
                   ├──▶ needs_attention  (agent reported permission denials)
                   └──▶ interrupted      (bridge restarted while queued/running; never auto-retried)
```
All four terminal states trigger the push. `interrupted` jobs are pushed once at bridge start-up.

## Sessions and models
- A `session_id` resumes the agent's own conversation (Codex thread / Claude session) on the same backend and machine.
- Only one queued/running job per session (409 otherwise). A resume without `model`/`effort` reuses the session's last
  selection so Codex does not silently fall back to its config default; explicit changes are recorded in `model_switched_from`.
- `effective_model` / `effective_effort` are read back from the Codex rollout (or Claude's `modelUsage`) after the run.

## Why the push lives in the bridge, not in a CLI hook
Claude Code has hooks; Codex does not have an equivalent. Putting the push in the bridge (and in the SSH worker)
makes it engine-agnostic, independent of prompts, and covers states a CLI hook never sees (crashes, timeouts,
bridge restarts).
