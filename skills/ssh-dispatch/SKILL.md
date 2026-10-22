---
name: ssh-dispatch
description: >-
  Dispatch a task from the bot's box to a native agent CLI (Codex or Claude Code) on a Linux/Ubuntu host over
  direct Tailscale SSH (dispatcher/direct_ssh_client.py). No daemon on the host; optional webhook wake.
---
# Ubuntu dispatch (SSH mode)

The work is done by the native agent CLI on the remote host; the bot only dispatches and reports. Permissions equal
running that CLI under the target account. Report only what the job record shows.

## Transport
- Client: `python3 dispatcher/direct_ssh_client.py` with `HARNESS_HOME` set. Pin its sha256 in your notes and keep a
  backup; changing it needs the user's approval.
- OpenSSH with a dedicated key per host (`targets.json` `ssh_key`), strict `$HARNESS_HOME/ssh/known_hosts`, and
  `ProxyCommand=tailscale --socket=$HARNESS_HOME/run/tailscaled.sock nc %h %p`. The remote runs the CLI in a login zsh,
  using that host's own config and login state.
- If the tailnet is down: `dispatcher/tailscale/start-tailscaled.sh`, then `tailscale ... status`. Do not relax host key
  checking, source restrictions, permissions or credentials.

## Commands
- `targets`: list backends (host, account, workspace, engine, models).
- `submit`: JSON on stdin `{"backend": "...", "prompt": "...", "session_id": "<optional>", "model": "<optional>",
  "effort": "<codex only>", "wake": {"url": "<routine webhook url>", "key_env": "<ENV_VAR_NAME>"}}` -> prints the job record.
- `status <job_id>`: one-shot read of `$HARNESS_HOME/direct-jobs/<job_id>.json`.

## Waiting: webhook wake first
- Create one webhook-triggered routine per bot. The user stores the routine key in an environment variable of the
  dispatcher (never printed, never written to disk); the URL may live in a file. Set `HARNESS_WAKE_URL_PREFIX`
  to the trusted HTTPS origin (optional path boundary); it is required for wake.
  Only `HARNESS_WAKE_KEY` may be selected unless the dispatcher sets `HARNESS_WAKE_KEY_ENV`. Redirects are refused.
- With `wake`, end the turn right after submit. On terminal state the worker POSTs job_id/status/backend/model/effort/
  exit_code (no result text) with `Authorization: Bearer <key>`, retrying 5xx up to 3 times; the outcome is stored in
  the job's `wake` field. On wake, read the job file and report.
- Without `wake`: `dispatcher/bin/job_wait.sh <job_id> [--max 760] [--bot <name>]` in the background
  (0 terminal; 2 timeout, run again; 3 worker gone / never started; 4 not found).

## Prompt suffix (always)
"Write long output (code, logs, reports) to a file under the workspace and give its path. Your final reply must be at most 15 lines: outcome, artifact paths, decisions needed, anything blocked."

## Backends and sessions
- One bot per host, managing `<host>-codex` and `<host>-claudecode`. Codex runs with the host's own
  `~/.codex/config.toml` (sandbox/approval policy); Claude runs `-p --output-format stream-json --permission-mode acceptEdits`.
- Resume with the same backend + real `session_id` only. Same repo + two write jobs -> separate worktrees.
- Report failed / needs_attention results verbatim, including permission_denials.

## Model selection
- Codex: `"model": "slug"` or `"slug:effort"`, optional `"effort"`. Allowlist from the host's `~/.codex/models_cache.json`
  (cached in `direct-jobs/<backend>-models.json`); unknown models fail at submit. Resume without model reuses the
  session's last explicit choice.
- Claude: without `model` the host default applies. With `model`, the target's optional `switch_helper` runs
  `<helper> export <model>` for that one process only; unknown models fail with exit 3 and the candidates in `error`.
  Claude backends reject `effort`.

## After SSH drop / timeout
- The remote CLI keeps running. `status <job_id>` first, then a read-only check on the same backend.

## Side effects
- Same rules as mac-dispatch: list files first, `cp -p` backups, temp file + `mv`. Copy files with `ops/push_file.sh`
  (sha256-verified), never by pasting encoded blobs through a prompt.
