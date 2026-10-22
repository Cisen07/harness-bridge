# Webhook push wake

Goal: the bot ends its turn right after submitting a job and is woken by an HTTP push when the job reaches a
terminal state. Then it reads the real result from the job API. The push itself carries no output.

## 1. Create the routine (once per machine bot)
Create a routine with a **webhook trigger** in your bot platform. Its panel shows a URL and a send key; only the
user copies them. The bot never needs to see the key.

## 2a. macOS bridge: webhook.conf
On the Mac, next to `codex_bridge.py` (or at `server.webhook_conf`):
```sh
cp webhook.conf.example webhook.conf && chmod 600 webhook.conf
# edit with a local editor; do not paste the key into chats, prompts or shell history
```
```ini
WEBHOOK_URL=<your_endpoint>
WEBHOOK_AUTH=Bearer <your_key>
```
> **The #1 failure: 401.** `WEBHOOK_AUTH` is sent verbatim as the `Authorization` header value. It must be exactly
> `Bearer <key>`. Writing `Authorization: Bearer <key>` produces the header `Authorization: Authorization: Bearer ...`
> and the endpoint answers 401. A bare `<key>` without `Bearer ` fails the same way.

The file is re-read on every push, so no restart is needed after editing it. Only `WEBHOOK_URL` and `WEBHOOK_AUTH`
are accepted; anything else disables the push (log line `Terminal webhook failed (ValueError)`).

## 2b. SSH mode: wake field
Store the key in an environment variable of the dispatcher process (e.g. `HARNESS_WAKE_KEY`), set
`HARNESS_WAKE_URL_PREFIX` to your webhook host prefix, and submit with
`"wake": {"url": "<your_endpoint>", "key_env": "HARNESS_WAKE_KEY"}`. The worker adds the `Bearer ` prefix itself.

## 3. Self-test
1. Submit a tiny job (`"prompt": "reply OK"`), end the turn.
2. Expect a wake within seconds of completion. Then `GET /jobs/<id>`.
3. A deliberately slow job (`sleep 40 && echo READY`) proves you are woken by the push and not by a poller.
4. Check `launchd.stdout.log` / `launchd.stderr.log` on the Mac: no `Terminal webhook failed` lines.

## Troubleshooting
| Symptom | Cause |
|---|---|
| `Terminal webhook failed (HTTPError)` | 401: wrong `WEBHOOK_AUTH` format or rotated key; 404: wrong URL |
| `Terminal webhook failed (ValueError)` | unknown key in webhook.conf, non-http(s) URL, or a 3xx redirect (refused on purpose) |
| `Terminal webhook failed (URLError/TimeoutError)` | Mac has no outbound internet, or a proxy is needed (see launchd template) |
| No log line, no wake | `webhook.conf` missing or a value empty (push silently disabled) |
| Wake arrives, job still running | impossible by design; check you read the job id from the payload |

## Payloads
Bridge: `{"job_id","status","backend","host"}`. SSH worker: `{"job_id","backend","status","exit_code","model","effort","finished_at"}`.
