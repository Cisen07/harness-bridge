# harness-bridge

**Start here:** paste this one sentence into your Grok Bot / installation agent:

```text
帮我安装 https://github.com/Cisen07/harness-bridge。
```

**For installation agents:** when a user asks to install this repository, first read and follow
[docs/agent-install-prompt.md](docs/agent-install-prompt.md).
The human [INSTALL appendix](INSTALL.md) follows the same order.

Run native coding-agent CLIs (**Codex CLI** and **Claude Code**) on your own machines from a cloud bot
("dispatcher"), over a private **Tailscale** tailnet. Optional webhook wake is **best-effort**:
notifications can be lost or duplicated, so every integration must retain compensating job queries.
Python **3.11+** is required throughout; CI runs Python **3.12**.

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

## Install and releases
Follow the [agent installation playbook](docs/agent-install-prompt.md) in order:
prerequisite choices → directory layout → private targets/tokens before startup → minimal job without wake →
optional wake → compensating readback.
The [INSTALL appendix](INSTALL.md) links the platform details.

Shared implementation comes from this repository; private configuration and instance state stay on each deployment.
We recommend pinning a release commit SHA. For personal use, discover drift while dogfooding and iterate;
no fleet-wide per-file hash ceremony is required, and a merge does not prove every running machine uses that release.

`ops/push_file.sh` verifies a transfer to a Linux SSH target; it is not a deployment transaction with backups.
It does not create `.bak` files or detect concurrent overwrites. See [deployment boundaries](docs/deploy-and-rollback.md#push_file-boundary).

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

Timeouts: the Mac bridge kills the CLI process group, including descendants that remain in that group.
An SSH timeout does not guarantee remote processes have stopped; inspect the host before retrying.
See [webhook and timeout behavior](docs/webhook-wake.md).

Bearer tokens protect the API from other tailnet nodes and network callers.
They do not protect against code running as the same local user and do not isolate
jobs: such code can read that user's token files. Use OS accounts/sandboxing for
local isolation. See [source tokens, channel B and cutover](docs/deploy-and-rollback.md).
For manual calls (Bash, header supplied over an anonymous descriptor):

```bash
AUTH_HEADER=$(python3 "$HARNESS_HOME/bin/_bridge_auth.py")
/usr/bin/curl -q -H @/dev/fd/3 --proxy socks5h://127.0.0.1:10555 --noproxy '' \
  http://<mac-tailnet-ip>:18542/health 3< <(printf '%s\n' "$AUTH_HEADER")
unset AUTH_HEADER
```
