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

## 4. Webhook
`cp bridge/webhook.conf.example "$BRIDGE_DIR/webhook.conf" && chmod 600 "$BRIDGE_DIR/webhook.conf"`, then fill in the
routine URL and `Bearer <key>` (see webhook-wake.md).

## 5. Verify from the dispatcher
```sh
curl --proxy socks5h://127.0.0.1:10555 --noproxy '' http://<mac-ip>:18542/health
curl ... -X POST -H 'Content-Type: application/json' -d '{"backend":"mac-codex","prompt":"reply OK"}' http://<mac-ip>:18542/jobs
```
Then wait for the push and `GET /jobs/<id>`.

## Upgrades
Follow deploy-and-rollback.md (sha check, `cp -p` backup, py_compile, detached restart, smoke test).
