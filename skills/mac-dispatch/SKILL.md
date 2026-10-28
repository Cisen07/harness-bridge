---
name: mac-dispatch
description: >-
  Dispatch a task from the bot's box to a native agent CLI on a Mac (Codex = backend mac-codex,
  Claude Code = backend mac-claudecode) through the harness-bridge HTTP job API over Tailscale, and get
  woken up by the bridge's webhook push when the job reaches a terminal state. Ubuntu hosts use ssh-dispatch.
---
# Mac dispatch (HTTP bridge mode)

The work is done by the native agent CLI on the Mac; the bot only dispatches and reports. Permissions equal
running that CLI on the Mac directly. Report only what the API shows.

## Machines and addresses
- One bot per machine; it manages both engines on that machine. Each Mac runs the same job API on the port in
  its `config.toml` (default 18542). Addresses live in `$HARNESS_HOME/hosts.env` (`MAC_HOSTS="alias=ip:port ..."`).
- Coordinator bots hand tasks to the machine bot instead of calling the Mac API themselves.

## Backends
- `mac-codex` (Codex CLI; the default when `backend` is omitted) and `mac-claudecode` (Claude Code CLI).
- Always send `backend` explicitly. Use the engine/model the requester named; otherwise follow your team's
  engine tiers. If an engine fails, report it; never silently switch engines. Report engine + model every time.

## Connectivity
- All requests leave the box via the userspace tailscaled SOCKS proxy:
  `curl -q -H @/dev/fd/3 --proxy socks5h://$HARNESS_SOCKS_ADDR --noproxy '' http://<mac-ip>:<port>/... 3< <(printf '%s\n' "$AUTH_HEADER")` (keep AUTH_HEADER non-exported)
- If it fails: run `dispatcher/tailscale/start-tailscaled.sh`, then `tailscale --socket=$HARNESS_HOME/run/tailscaled.sock status`.
  A Mac that shows online but times out usually means the bridge service is down: report and wait.
- Optional fallback when the bridge is up but the box cannot reach it: `dispatcher/relay/relay_via_host.sh GET|POST <path>`
  (POST body on stdin). Exit codes: 0 done; 2 usage/config; 3 method; 4 path; 5 SSH to relay failed;
  6 local timeout (GET before re-POSTing); 7 curl failed on the relay. Retry a direct POST only on curl exit 97 (SOCKS failure).

## API (details: docs/api.md)
1. `GET /health` (workspace, mode, host label), `GET /backends` (models, efforts, selector).
2. `POST /jobs` `{"backend": "...", "prompt": "...", "session_id": "<optional>", "model": "<optional>", "effort": "<optional>"}`
   (body <= 16000 bytes) -> `202 {"job_id", "status": "queued"}`. End every prompt with:
   "Write long output (code, logs, reports) to a file under the workspace and give its path. Your final reply must be at most 15 lines: outcome, artifact paths, decisions needed, anything blocked."
   - `model` may be `"slug"` or `"slug:effort"`; `effort` (low|medium|high|xhigh|max|ultra) is mac-codex only.
     Conflicts and unknown models return 400. A resume without model/effort reuses the session's last selection.
   - Mac HTTP only: optional `tier` is routing metadata, never automatic model selection.
     Use a fictional label such as `example-tier` only after selecting a model.
     A tier request requires a request/session model even if `[tier] require_explicit_selection=false`.
     With that guard true, all jobs need a request/session model; effort alone does not count.
     Defaults are `allowed_tiers=[]`, guard false. SSH submit is not aligned with this contract.
3. `GET /jobs/<id>` returns status, result, exit_code, model/effort, model_source, effective_model/effort, warnings.
4. Up to `max_workers` (default 4) jobs run at once across both backends; a second job on the same session_id gets 409.

## Waiting: webhook push first
- Each machine bot owns one webhook-triggered routine. The user copies its URL and key into the Mac's
  `webhook.conf` (0600) - the bot never sees the key. `WEBHOOK_AUTH` must be exactly `Bearer <key>`.
- After POSTing a job, record the job_id and END THE TURN. The bridge POSTs `{job_id, status, backend, host}`
  on completed / failed / needs_attention / interrupted. On wake, `GET /jobs/<job_id>` and report the real result.
- Fallback when push is not configured or a wake is overdue: run
  `dispatcher/bin/mac_wait.sh <alias|host:port> <job_id> <outfile> [--max 760] [--bot <name>]` in the background
  (exit 0 terminal; 2 timeout, run again; 4 not found; 5 API unreachable - do not resubmit). Never both poll and resubmit.

## Parallel work
- Read-only tasks or tasks touching different repos may run in parallel. Two write tasks in the same repo each
  get their own git worktree, or run one after the other.

## After cancel / timeout
- The job may still be running: `GET /jobs/<id>` first, then a read-only check on the same backend
  (leftover agent processes, `git status`, `git worktree list`, recently modified files) before claiming anything.

## Commands with side effects
- List the files a job will write before installing or changing config. Back up with `cp -p X X.bak-$(date +%Y%m%d%H%M%S)`,
  write via temp file + `mv`.
- Restarting the bridge from a job kills that job and interrupts all running jobs: confirm nothing else runs, then use
  `ops/restart_bridge_detached.py <label>`; verify the new PID and `/health` afterwards.

## Sessions
- Resume only with a real `session_id` from the same backend on the same machine. Keep a per-backend session map in
  the bot's own storage.

## Scope
- Fixed workspace = `[workspace].path`. The API does not accept cwd, shell, sandbox or permission overrides;
  only model and effort are selectable. Credentials on the Mac are never read or printed.
