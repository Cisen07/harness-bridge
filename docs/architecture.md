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
daemon thread. Delivery is best-effort; the bot must retain compensating queries.

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
The Mac bridge attempts a push for all four terminal states. `interrupted` jobs are pushed once at bridge start-up.
SSH has no startup recovery scan; see the matrix below.

## Transport differences

| Behavior | Mac HTTP bridge | Linux SSH dispatcher |
|---|---|---|
| Concurrency | Bounded pool, default 4 workers shared by backends, plus queue | One detached worker per accepted submit; no global execution limit |
| Same backend + session | HTTP 409 for a queued/running resume | CLI rejects with `409 Conflict` and the same conflict message before launching a worker; admission scan + reservation serialized with flock |
| Mutex scope | One bridge instance/journal | Submitters sharing one local `HARNESS_HOME/direct-jobs`; independent dispatchers are not coordinated |
| Restart / worker loss | Startup marks queued/running jobs interrupted and attempts wake; no automatic retry | No startup recovery scan; wait exits 3 if worker is gone/stale queue, may leave queued/running on disk |
| Timeout | Kills local CLI process group; escaped descendants are outside guarantee | Stops local transport; remote CLI may survive, inspect before retrying |
| Wake | Daemon thread, no durable outbox; exit may lose notification | Worker has finite retries recorded on job; no self-healing after worker loss |

SSH admission preserves `session_id` while queued/running and reads legacy `.request` records when
older jobs lack it. Corrupt/missing active-job data fails admission rather than assuming the session is free.
Stale active state can therefore continue to block a resume: inspect the worker **and remote CLI** and
reconcile privately before any state repair. This change does not introduce restart recovery or global throttling.

## Sessions and models
- A `session_id` resumes the agent's own conversation (Codex thread / Claude session) on the same backend and machine.
- Only one queued/running resume per backend + session within the scopes above (409 conflict otherwise).
- The Mac bridge reuses the last model/effort selection and records explicit changes in `model_switched_from`.
  SSH reuses locally recorded request/session selections; an unknown session falls back to host defaults with a note.
- `effective_model` / `effective_effort` are read back from the Codex rollout (or Claude's `modelUsage`) after the run.

## Why the push lives in the bridge, not in a CLI hook
Claude Code has hooks; Codex does not have an equivalent. Putting the push in the bridge (and in the SSH worker)
makes it engine-agnostic, independent of prompts, and covers states a CLI hook never sees (crashes, timeouts,
Mac bridge restarts). Delivery remains best-effort and must be backed by compensating readback.
