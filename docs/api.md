# Bridge HTTP API (macOS)

Base URL: `http://<mac-tailnet-ip>:<port>` (default port 18542). From the dispatcher box:
`curl --proxy socks5h://127.0.0.1:10555 --noproxy '' ...`. Every route requires an allowed source (else 403).

## GET /health
`{"ok": true, "backend": "...", "backends": ["mac-codex","mac-claudecode"], "mode": {...}, "host": "<host_label>", "workspace": "..."}`

## GET /backends
Per backend: `host`, `account`, `engine`, `workspace`, `supports_model`, `supports_effort`; for `mac-codex` also
`models` (`{slug: [efforts]}`), `default` (from `~/.codex/config.toml`) and `selector`.

## POST /jobs
Body (JSON, 1..16000 bytes; unknown fields -> 400):
| field | type | notes |
|---|---|---|
| `prompt` | string, required | non-empty |
| `backend` | `mac-codex` (default) \| `mac-claudecode` | |
| `session_id` | string | must be a session this bridge returned for the same backend; 409 if it already has a queued/running job |
| `model` | string | Codex: allowlisted slug, optionally `slug:effort`. Claude: `claude --model` name, or a switch-helper selector when `model_mode = "switch"` |
| `effort` | `low\|medium\|high\|xhigh\|max\|ultra` | mac-codex only; must not conflict with a `:effort` suffix |

Response `202 {"job_id": "<uuid>", "status": "queued"}`. Errors: `400 {"error": "..."}` (safe to show), `409`.

## GET /jobs/<job_id>
```json
{"job_id": "...", "status": "completed", "backend": "mac-codex", "session_id": "...", "workspace": "...",
 "created_at": "2026-01-01T00:00:00+00:00", "model": "gpt-6-astra", "effort": "medium", "model_source": "request",
 "effective_model": "gpt-6-astra", "effective_effort": "medium", "exit_code": 0, "result": "...",
 "warnings": ["..."], "permission_denials": [], "error": null}
```
`model_source`: `request` | `session` (inherited on resume) | `default`. `404` for unknown ids.

## Webhook push (outbound)
`POST <WEBHOOK_URL>` with `Authorization: <WEBHOOK_AUTH>` and
`{"job_id": "...", "status": "completed|failed|needs_attention|interrupted", "backend": "mac-codex|mac-claudecode", "host": "<host_label>"}`.
5 s timeout, no redirects, no retries (best effort). See webhook-wake.md.

## Limits
`max_workers` concurrent jobs (default 4) across both backends; `job_timeout_seconds` per job (default 3600; the
Claude process group is killed on timeout).

All routes also require a shared Bearer credential from an owned 0600 token file (SECURITY.md).
POST /jobs requires Content-Type: application/json (415 otherwise), rejects any Origin or Sec-Fetch-Site
header (403), and requires Host exactly equal to the configured bind_host:port (403).
