# Security model

## What this system is
A way to give a remote bot the same power as typing into Codex / Claude Code on your machine. The example Codex configuration uses `workspace-write` with no approval prompts.
Opting into `danger-full-access` removes filesystem sandboxing and permits access outside the workspace. **Anyone who can call the bridge, or who controls the dispatcher box,
can run arbitrary commands as your user.** Treat the dispatcher box and its tailnet identity as highly privileged.

## Trust boundaries and controls
| Boundary | Control |
|---|---|
| Network reachability | Bridge binds to the Mac's tailnet IP only (never `0.0.0.0`). Tailscale ACL grants only the dispatcher tag (and named peers) access to the bridge port; see docs/tailscale-acl.md. |
| Caller identity | `access.allowed_sources`: source IP must be listed **and** `tailscale whois` must report the expected ACL tag or MagicDNS node name; otherwise 403 without further work. whois errors deny. |
| Request surface | Only `prompt`, `backend`, `session_id`, `model`, `effort`. No cwd / shell / sandbox / permission overrides. Body <= 16000 bytes. Model names are allowlisted or regex-validated; every value interpolated into a shell is `shlex.quote`d. |
| SSH mode | Dedicated ed25519 key per host, `StrictHostKeyChecking=yes` with a pinned known_hosts, `BatchMode`, `IdentitiesOnly`. Prefer a dedicated account and `authorized_keys` options (`restrict,from="<dispatcher-ip>" ssh-ed25519 <public-key>`) on the host. |
| Relay fallback | Separate key, restricted with `restrict,command=...` on the relay host; method allowlist (GET/POST), path regex, no `..`, length limit. |
| Webhook push | URL + key live only in `webhook.conf` (0600) on the Mac, or in an environment variable on the dispatcher (SSH mode). The bridge never logs the URL, headers, config or exception text (only the exception class). Redirects are refused so `Authorization` cannot leak. Payload = job_id / status / backend / host (+ model/effort/exit_code in SSH mode) - **never the result text**. The bot must fetch results through the authenticated job API. |
| Logs / journals | No request logging. `jobs.json` and `direct-jobs/*.json` are 0600 and contain prompts and results: keep them out of git and backups you share. |

## Secrets: where they may live
- Webhook key: `webhook.conf` (0600) or an env var. Never in git, prompts, chat, job records or logs.
- SSH private keys: `$HARNESS_HOME/ssh/` (0600). Never in git.
- tailscaled state (contains node keys): `$HARNESS_HOME/state/`. Never in git or shared archives.
- Agent CLI credentials stay where the CLIs keep them; the bridge never reads them.

## Before publishing changes
- `scripts/scrub_check.sh` and gitleaks run in CI on every push/PR. Run `scripts/scrub_check.sh` locally too, and add
  your own private denylist (real IPs, tailnet name, usernames) in `.scrub-denylist.local` (git-ignored).

## Reporting
Please open a private security advisory on the repository instead of a public issue.

## Shared API credential and local trust boundary

Every HTTP route requires both the allowed Tailscale identity and `Authorization: Bearer <token>`.
Provision the same random value in separate owned regular files on the bridge and dispatcher, mode **0600**;
see `bridge/bridge.token.example` and `dispatcher/bridge.token.example`. Generate a fresh value with
`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` into a private file (`umask 077`).
Placeholders must be replaced. Credentials never belong in logs, command arguments, job records or git.
Bridge: `[server] token_file` (default `bridge.token` beside the bridge), overridden by
`HARNESS_BRIDGE_TOKEN_FILE`. Dispatcher: exported `HARNESS_BRIDGE_TOKEN_FILE`, default
`$HARNESS_HOME/bridge.token`. Missing, empty, oversized, symlinked, incorrectly owned or non-0600 files
fail closed. Startup validates the token before listening and refuses invalid credentials. The bridge rereads on each request to permit rotation and uses `hmac.compare_digest`.
Explicit legacy opt-out: bridge `[server] require_token = false`, dispatcher
`HARNESS_BRIDGE_REQUIRE_TOKEN=0`; default is required on both sides. Direct SSH jobs do not call
this HTTP API and continue to use their dedicated SSH key. Relay reads the dispatcher file and sends
its header over SSH stdin; polling uses a private header descriptor, never a token in argv.

The userspace SOCKS listener **127.0.0.1:10555 has no authentication**. Any local process able to reach
it can use the dispatcher's tailnet network identity. Treat the dispatcher OS/users and any containers
with access to that listener as trusted; keep it on loopback and protect the separate API token file.
POST `/jobs` additionally rejects Origin / Sec-Fetch-Site headers, requires the exact configured
`bind_host:port` Host and `application/json` media type; handler reads time out after 30 seconds.

Webhook URLs require HTTPS except literal loopback addresses or `100.64.0.0/10` over HTTP.
Bridge `[server] webhook_url_prefix` optionally pins the exact origin and path boundary, matching
SSH mode's `HARNESS_WAKE_URL_PREFIX` policy. Both reject userinfo, query, fragment and ambiguous
path segments; redirects never forward credentials. Plain HTTP inside the allowed network still
relies on that network's confidentiality. Prefer HTTPS when possible.

Use `restrict,from="<dispatcher-ip>" ssh-ed25519 <public-key>` in `authorized_keys`.
Keep OpenSSH updated and expose sshd only to the tailnet. Follow official Tailscale security advisories
([security bulletins](https://tailscale.com/security-bulletins)); update the pinned default version promptly.

## Relay shell requirement
Relay hosts must use Bash 5.1+; zsh and older Bash may briefly write here-doc
Authorization headers to disk. Prefer stdin and anonymous pipes; see the
[restricted SSH wrapper example](docs/relay-security.md).
