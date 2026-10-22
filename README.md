# harness-bridge

Run native coding-agent CLIs (**Codex CLI** and **Claude Code**) on your own machines from a cloud bot
("dispatcher"), over a private **Tailscale** tailnet, and get the bot **woken up by a webhook push** the moment a
job finishes - instead of keeping a polling command alive and hoping its exit wakes the bot.

```
                 tailnet (WireGuard, ACL-restricted)
 ┌──────────────┐  POST /jobs, GET /jobs/<id>   ┌──────────────────────────┐
 │ dispatcher   │ ───────────────────────────▶ │ macOS: harness-bridge     │──▶ codex exec / claude -p
 │ box (bot)    │                              │ (HTTP job API, launchd)   │
 │ userspace    │ ◀── webhook push ─────────── │  on terminal state        │
 │ tailscaled   │     {job_id,status,...}      └──────────────────────────┘
 │              │  ssh (tailscale nc)          ┌──────────────────────────┐
 │              │ ───────────────────────────▶ │ Ubuntu: no daemon         │──▶ codex exec / claude -p
 │              │ ◀── webhook push (worker) ── │  (SSH mode)               │
 └──────────────┘                              └──────────────────────────┘
```

| Platform | Mode | Engines | Push wake |
|---|---|---|---|
| macOS | **HTTP bridge** (`bridge/codex_bridge.py` + launchd) | `mac-codex`, `mac-claudecode` | bridge POSTs on every terminal state |
| Ubuntu / Linux | **SSH mode** (`dispatcher/direct_ssh_client.py`, nothing installed on the host) | `<host>-codex`, `<host>-claudecode` | dispatcher worker POSTs when `wake` is set |

Ubuntu is supported in SSH mode only; there is no Linux build of the bridge (an unsupported systemd template is
included for experiments).

## Repository layout
```
bridge/        HTTP job API that runs on each Mac (config.example.toml, webhook.conf.example, service templates, tests)
dispatcher/    runs on the bot's box: SSH-mode client, wait scripts, relay fallback, userspace tailscaled helpers
ops/           push_file.sh (sha-verified copy), detached bridge restart, optional patrol
skills/        bot skill templates: mac-dispatch (HTTP mode), ssh-dispatch (SSH mode)
docs/          architecture, api, webhook-wake, install-macos, install-ubuntu, tailscale-acl, deploy-and-rollback
examples/      request / response / webhook payload samples
scripts/       scrub_check.sh (used by CI together with gitleaks)
```

## Quick start
1. Tailnet + ACL: [docs/tailscale-acl.md](docs/tailscale-acl.md).
2. Dispatcher box: `export HARNESS_HOME=~/harness`, copy `dispatcher/hosts.example.env` -> `$HARNESS_HOME/hosts.env`,
   run `dispatcher/tailscale/start-tailscaled.sh` and log in once.
3. Mac: [docs/install-macos.md](docs/install-macos.md) (config.toml, launchd, webhook.conf).
4. Ubuntu: [docs/install-ubuntu.md](docs/install-ubuntu.md) (SSH key, targets.json).
5. Webhook wake: [docs/webhook-wake.md](docs/webhook-wake.md). Remember: `WEBHOOK_AUTH=Bearer <key>` exactly.

## Security
Read [SECURITY.md](SECURITY.md). Short version: the bridge binds to a tailnet IP only, every caller must be in an
allowlist **and** confirmed by `tailscale whois`; secrets live only in 0600 files / environment variables on the
machine that uses them; webhook payloads never contain job output.

## 中文简介
一套让云端 bot 通过 Tailscale 调度你自己机器上的 Codex / Claude Code 的工具：Mac 走 HTTP bridge（launchd 常驻，
任务到终态主动 webhook 推送唤醒 bot），Ubuntu 走 SSH 直连模式（主机上不装常驻服务）。所有地址、账号、路径都从
配置文件读取，仓库里只有占位符模板。

## License
MIT - see [LICENSE](LICENSE).
