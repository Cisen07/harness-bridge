# Install on macOS (HTTP bridge mode)

Prerequisites: Tailscale app (logged in, the Mac has a tailnet IP), Python 3.11+ (or 3.8+ with `config.json`),
Codex CLI and/or Claude Code installed and logged in for your user.

## 1. Files
```sh
BRIDGE_DIR="$HOME/harness-bridge"          # any directory you own
mkdir -p "$BRIDGE_DIR" && chmod 700 "$BRIDGE_DIR"
cp bridge/codex_bridge.py bridge/agent_adapters.py bridge/bridge_config.py "$BRIDGE_DIR/"
cp bridge/config.example.toml "$BRIDGE_DIR/config.toml" && chmod 600 "$BRIDGE_DIR/config.toml"
```
Edit `config.toml`:
- `server.bind_host` = `tailscale ip -4`; `server.port` (default 18542); `server.host_label` (e.g. `mac-a`).
- `server.tailscale_bin`: `/usr/local/bin/tailscale` or `/Applications/Tailscale.app/Contents/MacOS/Tailscale`.
- `workspace.path`, `codex.bin`, `claude.bin`, `claude.path_prepend` (e.g. an nvm `bin` dir if claude lives there).
- Optional `claude.env_script` (sourced before claude, e.g. to point Claude Code at a gateway) and
  `claude.model_mode = "switch"` + `claude.switch_helper` if models are selected by a helper (`<helper> export <model>`).
- `access.allowed_sources`: the dispatcher's tailnet IP + `tag:<dispatcher-tag>`, plus any trusted peer by node name.

## 2. Test locally
```sh
cd bridge && python3 -B -m unittest discover -s tests -v
```

## 3. launchd
Fill in `bridge/service/com.example.harness-bridge.plist.template` (label, home, bridge dir, python, workspace),
save as `~/Library/LaunchAgents/<label>.plist`, then:
```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
tail -f "$BRIDGE_DIR/launchd.stdout.log"     # "harness-bridge listening on <ip>:<port> (N allowed sources)"
```
macOS may ask once for permissions (e.g. local network / files) for the python binary; a dedicated copy of python
makes this prompt easy to recognize.

If the log shows `Cannot start ...` and launchd keeps relaunching the job (about every 10s under `KeepAlive`), fix the token file first: it must exist, be non-empty, and be mode 0600. The bridge refuses to start on a missing or invalid token and exits before listening, so the restart loop continues until the token file is corrected.

## 4. Webhook
`cp bridge/webhook.conf.example "$BRIDGE_DIR/webhook.conf" && chmod 600 "$BRIDGE_DIR/webhook.conf"`, then fill in the
routine URL and `Bearer <key>` (see webhook-wake.md).

## 5. Verify from the dispatcher
```sh
curl -q --noproxy '*' -H @/dev/fd/3 3< <(printf '%s\n' "$AUTH_HEADER") http://<mac-ip>:18542/health
curl -q --noproxy '*' -H @/dev/fd/3 3< <(printf '%s\n' "$AUTH_HEADER") ... -X POST -H 'Content-Type: application/json' -d '{"backend":"mac-codex","prompt":"reply OK"}' http://<mac-ip>:18542/jobs
```
Then wait for the push and `GET /jobs/<id>`.

## Upgrades
Follow deploy-and-rollback.md (sha check, `cp -p` backup, py_compile, detached restart, smoke test).

## Required API token

Before starting, provision a distinct random token for every allowed source (see SECURITY.md), provision it separately on the Mac
and dispatcher using `bridge/bridge.token.example` and `dispatcher/bridge.token.example` as format examples,
and chmod both files 600. Set each source's absolute `token_files` paths on the bridge (default
`server.require_per_source_tokens = true`, required in production);
export `HARNESS_BRIDGE_TOKEN_FILE` on the dispatcher. The dispatcher defaults to `$HARNESS_HOME/bridge.token`.
The global bridge token is only a fallback when `require_per_source_tokens = false`. Missing or invalid credentials refuse startup (including non-0600 permissions); provision them before upgrading.
The file is also checked on every request so unsafe rotation fails closed.
For manual curl, use the repository's header helper without exposing the token in argv:

```bash
AUTH_HEADER=$(python3 dispatcher/bin/_bridge_auth.py) || exit 1
curl -q --noproxy '*' -H @/dev/fd/3 3< <(printf '%s\n' "$AUTH_HEADER") http://<mac-ip>:18542/health
# Add -H 'Content-Type: application/json' and --data-binary @request.json for POST /jobs.
unset AUTH_HEADER
```

Apply the same header to the earlier smoke-test commands. Do not add Origin or Sec-Fetch-Site headers;
Host must be the configured bind_host:port. Keep `_bridge_auth.py` alongside installed dispatcher bin scripts.
The userspace tailscaled default is 1.104.2; follow official Tailscale security bulletins and update it promptly.

For the smoke curl commands above, first obtain a non-exported `AUTH_HEADER` using
the header helper shown in Required API token, then unset it after use.

## Preflight self-source (staging only)

Create an independent staging config/release with port **18543** and a separate
journal. Merge the following self entry with the staging caller rules:

```toml
[server]
bind_host = "<本机tailnet IP>"
port = 18543
journal = "/private/stage/jobs.json"
token_file = "/private/stage/bridge.token"
require_per_source_tokens = true

[codex]
# Copy the explicit production values; this example assumes production uses these.
sandbox = "danger-full-access"
approval_policy = "never"

[access]
allowed_sources = [
  { ip = "<本机tailnet IP>", node = "<本机node名>", token_files = ["/private/stage/bridge.token"] },
]
```

The placeholders must be replaced locally; provision only a staging token and
keep staging files separate from production. Do not add this self-source to
production. Do not modify ACLs for the rehearsal. Code rejects wildcard and
non-tailnet bind addresses. `server.allow_loopback_for_tests = true` permits only
127.0.0.1 for isolated tests; it is a test-only option, not a deployment setting.

Set both `[codex]` values explicitly to the production values before rehearsal.
Omitting `sandbox` falls back to `workspace-write`: it cannot access the network
or write the cutover confirmation file outside the workspace, so that rehearsal
would not represent production. Keep source-bound token validation mandatory
(`require_per_source_tokens = true`); never use global-token fallback here.
The `bind_host` restrictions are identical to production: only the machine's
tailnet IPv4 address, never wildcard, LAN or test-loopback deployment settings.

Do not run the 18543 rehearsal instance as a persistent launchd service. Start it
in the foreground or with `nohup` from an independent terminal, using the separate
staging configuration. Stop that instance on the same day after rehearsal, then run
`lsof -nP -iTCP:18543 -sTCP:LISTEN` and verify that no listener remains.

For a box that reaches the Mac through a local SOCKS proxy, use `socks5h` (proxy
DNS resolution). The empty `--noproxy` explicitly overrides bypass settings:

```bash
AUTH_HEADER=$(python3 dispatcher/bin/_bridge_auth.py) || exit 1
curl -q --noproxy '' --proxy socks5h://127.0.0.1:1080 \
  -H @/dev/fd/3 3< <(printf '%s\n' "$AUTH_HEADER") http://<mac-ip>:18542/health
unset AUTH_HEADER
```

Every direct curl example uses `--noproxy '*'` to keep bridge traffic off proxies.
Token paths must be owned regular files, mode 0600, and must not be symlinks.
