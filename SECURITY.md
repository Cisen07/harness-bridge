# Security model

## What this system is
A way to give a remote bot the same power as typing into Codex / Claude Code on your machine. Agents typically run
with `danger-full-access` / no approval prompts. **Anyone who can call the bridge, or who controls the dispatcher box,
can run arbitrary commands as your user.** Treat the dispatcher box and its tailnet identity as highly privileged.

## Trust boundaries and controls
| Boundary | Control |
|---|---|
| Network reachability | Bridge binds to the Mac's tailnet IP only (never `0.0.0.0`). Tailscale ACL grants only the dispatcher tag (and named peers) access to the bridge port; see docs/tailscale-acl.md. |
| Caller identity | `access.allowed_sources`: source IP must be listed **and** `tailscale whois` must report the expected ACL tag or MagicDNS node name; otherwise 403 without further work. whois errors deny. |
| Request surface | Only `prompt`, `backend`, `session_id`, `model`, `effort`. No cwd / shell / sandbox / permission overrides. Body <= 16000 bytes. Model names are allowlisted or regex-validated; every value interpolated into a shell is `shlex.quote`d. |
| SSH mode | Dedicated ed25519 key per host, `StrictHostKeyChecking=yes` with a pinned known_hosts, `BatchMode`, `IdentitiesOnly`. Prefer a dedicated account and `authorized_keys` options (`restrict`, `from=`) on the host. |
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
