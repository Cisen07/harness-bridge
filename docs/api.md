# Bridge HTTP API (macOS)

Base URL: `http://<mac-tailnet-ip>:<port>` (default port 18542). From the dispatcher box:
`curl -q -H @/dev/fd/3 --proxy socks5h://127.0.0.1:10555 --noproxy '' ... 3< <(printf '%s\n' "$AUTH_HEADER")`. Every route requires an allowed source (else 403).

## GET /health
`{"ok": true, "backend": "...", "backends": ["mac-codex","mac-claudecode"], "mode": {...}, "host": "<host_label>", "workspace": "..."}`

## GET /backends
Per backend: `host`, `account`, `engine`, `workspace`, `supports_model`, `supports_effort`; for `mac-codex` also
`models` (`{slug: [efforts]}`), `default` (from `~/.codex/config.toml`) and `selector`.

The following tier contract applies only to the Mac HTTP bridge. SSH submit has
not been aligned with it. `supports_tier` is not exposed: the bridge records a
label and never resolves a tier into a backend or model.

## POST /jobs
Body (JSON, 1..16000 bytes; unknown fields -> 400):
| field | type | notes |
|---|---|---|
| `prompt` | string, required | non-empty |
| `backend` | `mac-codex` (default) \| `mac-claudecode` | |
| `session_id` | string | must be a session this bridge returned for the same backend; 409 if it already has a queued/running job |
| `model` | string | Codex: allowlisted slug, optionally `slug:effort`. Claude: `claude --model` name, or a switch-helper selector when `model_mode = "switch"` |
| `tier` | string or null | optional routing metadata, trimmed; null means omitted; see selection guard below |
| `effort` | `low\|medium\|high\|xhigh\|max\|ultra` | mac-codex only; must not conflict with a `:effort` suffix |

### Routing metadata and explicit model selection

`[tier]` configuration defaults to `allowed_tiers = []` and
`require_explicit_selection = false`. An empty list accepts any nonempty label;
a nonempty list rejects unknown labels with `400 {"error":"Unknown tier; allowed: ..."}`.
Examples use fictional labels such as `example-tier`; no routing policy is bundled.

A non-null `tier` always requires a model from the request or this backend's
session history, even when the guard is false. Otherwise the error is
`400 {"error":"tier must be resolved to an explicit model by the dispatcher side; the bridge does not resolve tiers"}`.
The tier is stored on the job, never inherited or passed to the CLI.

With `require_explicit_selection = true`, every job needs that request/history
model. Effort alone does not count. Session selection is resolved before the guard;
a resume with model history is accepted. Without such a model the error is
`400 {"error":"an explicit model selection or a session with model history is required"}`.
With the default false, a request without tier or model still uses CLI defaults.
A resume with neither model nor effort inherits both; an effort-only override
keeps the historical model, while a model-only override does not inherit effort.

Response `202 {"job_id": "<uuid>", "status": "queued"}`. Errors: `400 {"error": "..."}` (safe to show), `409`.

## GET /jobs/<job_id>
```json
{"job_id": "...", "status": "completed", "backend": "mac-codex", "session_id": "...", "workspace": "...",
 "created_at": "2026-01-01T00:00:00+00:00", "model": "gpt-6-astra", "effort": "medium", "model_source": "request",
 "effective_model": "gpt-6-astra", "effective_effort": "medium", "exit_code": 0, "result": "...",
 "warnings": ["..."], "permission_denials": [], "error": null}
```
New job records also include `tier` (null when omitted); old journal entries load it as null.

`model_source`: `request` | `session` (inherited on resume) | `default`. `404` for unknown ids.

## Webhook push (outbound)
`POST <WEBHOOK_URL>` with `Authorization: <WEBHOOK_AUTH>` and
`{"job_id": "...", "status": "completed|failed|needs_attention|interrupted", "backend": "mac-codex|mac-claudecode", "host": "<host_label>"}`.
5 s timeout, no redirects, no retries (best effort). See webhook-wake.md.

## Limits
`max_workers` concurrent jobs (default 4) across both backends; `job_timeout_seconds` per job (default 3600; the
Claude process group is killed on timeout).

All routes also require a source-bound Bearer credential (or the legacy global fallback) from an owned 0600 token file (SECURITY.md).
POST /jobs requires Content-Type: application/json (415 otherwise), rejects any Origin or Sec-Fetch-Site
header (403), and requires Host exactly equal to the configured bind_host:port (403).

In Bash, set the non-exported header with
`AUTH_HEADER=$(python3 "$HARNESS_HOME/bin/_bridge_auth.py")`, use the descriptor
form above for every route, then `unset AUTH_HEADER`. Never put the token literal
in curl argv or exported environment variables.
