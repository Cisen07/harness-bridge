# Agent-internal playbook: install Harness Bridge

This document is for the installation agent to read and execute, not a form for the user to fill in.
When a user asks to install this repository, first read and follow this playbook. The complete user
paste is just: 帮我安装 https://github.com/Cisen07/harness-bridge。
Do not require the user to paste question-format rules or instructions to read this document;
the installation agent is responsible for applying the UX below.
[INSTALL](../INSTALL.md) is the human appendix.

**Mandatory first step: if any prerequisite is unresolved, ask before installing. Use multiple-choice
questions with explicit options (single-select or multi-select as appropriate); never hand the user a
long blank form or ask them to paste this whole document.** Reuse answers already provided, present
only unresolved questions in small batches, and ask concise follow-ups for concrete paths or hosts
only when the selected option requires them. Do not infer consent from a recommended default.

## 0. Collect prerequisite choices before taking action

Ask all five questions below unless the answer is already explicit in the conversation:

1. **Where should we install?** (Multi-select if more than one machine is involved.)
   - This machine, using a local path.
   - A remote machine reached over SSH.
   Follow up for the local installation/workspace path or SSH destination, account and remote path
   as needed. Identify the dispatcher and each target, and confirm authorization for each machine.
2. **Which mode?** (Single-select.)
   - Mac HTTP bridge.
   - Linux SSH.
   - Both.
   Map each selected mode to its target and dispatcher; ask about any missing machine details.
3. **Is Tailscale available on the dispatcher and selected targets?** (Single-select per machine
   if their answers differ.)
   - Already available.
   - Not installed or not configured yet.
   - Unsure.
   For an unsure answer, offer an authorized read-only check. For a missing setup, establish the
   approved setup/login path and ACL access before installing. Do not assume tailnet membership or
   permission to modify ACLs from the mode selection.
4. **Where should the private runtime live (`HARNESS_HOME`)?** (Single-select.)
   - Use the recommended dispatcher default, `$HOME/harness`.
   - Use a path I specify.
   Resolve the chosen absolute path on the dispatcher. For Mac HTTP, also confirm a separate Mac
   bridge runtime path (recommend `$HOME/harness-bridge-runtime`). Keep runtime state out of source.
5. **Configure wake during this installation?** (Single-select.)
   - **Recommended default: configure wake now**, after the first successful job and readback.
   - Skip it for now — available only when the webhook URL or key is not yet available.
   Wake is the primary experience path. Polling and compensating readback are mandatory fallbacks,
   not the routine primary path. Record this choice; skipping wake means a degraded experience
   and does not skip compensating job queries. Never ask for URL or key values in chat.

Before phase 1, summarize the selected machines/roles, mode, Tailscale readiness, runtime paths and
wake choice without credential values. Resolve missing or inconsistent answers with further choices.
Reading this repository to prepare the questions is allowed; do not install dependencies, write
runtime configuration, start services or submit jobs until prerequisites are complete.

If access, credentials, ACL permission or native CLI login is missing at any phase, **stop and report
only the missing field names and the blocked phase**, never credential values. Use the actual
configuration field name when available; for access/login prerequisites use names such as
`ssh_access`, `tailscale_login`, `tailscale_acl_permission`, `codex_cli_login` or `claude_cli_login`.
Have the operator supply secrets through the authorized private provisioning channel, not in chat.
A missing credential is a blocked installation, not a request to paste a secret into the conversation.

Install Cisen07/harness-bridge on the selected machines. Execute the following phases **in order** and
record each check before advancing. Use Python **3.11+** everywhere (CI uses **3.12**).
Use only authorized machines; if a machine, account, tailnet or credential is unavailable,
stop at that boundary and report the missing field names. Do not weaken authentication to proceed.
Do not output, decode or quote credential fields. Report field names and whether present only.
Keep real hosts, targets, tokens, webhook URLs, logs and job output outside the public checkout.
Do not start a production cutover or change existing ACLs without authorization.

## 1. Source and directory layout

Use a clean checkout for source and a separate private runtime directory. Recommend pinning a
release commit SHA; record the chosen SHA. Do not invent a tag or treat an arbitrary branch as a
fixed release. Personal use can discover drift and iterate; no fleet-wide hash ritual is needed.

Run each transport-specific step only for the modes selected in phase 0. Execute commands on the
approved machine and adapt the example source checkout path below to the selected local or remote
path; keep the approved runtime path in `HARNESS_HOME`.

On the **dispatcher** (the supplied userspace tailscaled launcher targets Linux amd64), run:

```bash
umask 077
mkdir -p "$HOME/src"
git clone https://github.com/Cisen07/harness-bridge.git "$HOME/src/harness-bridge"
cd "$HOME/src/harness-bridge"
# If already cloned, inspect local changes before fetching/updating; do not overwrite them.
# git checkout --detach <chosen-release-SHA>
# Set HARNESS_HOME to the dispatcher path chosen in phase 0 before running this block.
export HARNESS_HOME="${HARNESS_HOME:?Set the phase-0 approved runtime path first}"
mkdir -p "$HARNESS_HOME" "$HARNESS_HOME/bin" "$HARNESS_HOME/ssh" \
  "$HARNESS_HOME/run" "$HARNESS_HOME/state" "$HARNESS_HOME/direct-jobs" \
  "$HARNESS_HOME/tailscale" "$HARNESS_HOME/results"
chmod 700 "$HARNESS_HOME" "$HARNESS_HOME/ssh" "$HARNESS_HOME/direct-jobs"
cp dispatcher/direct_ssh_client.py "$HARNESS_HOME/"
cp dispatcher/bin/* "$HARNESS_HOME/bin/"
cp dispatcher/tailscale/*.sh "$HARNESS_HOME/tailscale/"
chmod 700 "$HARNESS_HOME/bin/"*.sh "$HARNESS_HOME/tailscale/"*.sh
cp dispatcher/hosts.example.env "$HARNESS_HOME/hosts.env"
chmod 600 "$HARNESS_HOME/hosts.env"
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
```

The layout is: source checkout = shared implementation; `$HARNESS_HOME/bin` = wait scripts plus
`_env.sh`, `_job_summary.py`, **`_bridge_auth.py`**; `$HARNESS_HOME/direct_ssh_client.py` = SSH client;
`targets.json`, `hosts.env`, `ssh/`, tokens, `direct-jobs/`, results = private runtime.
Keep `bin/` and `tailscale/` siblings so the installed launcher can find `_env.sh`.
On a Mac target use a separate bridge runtime (for example `$HOME/harness-bridge-runtime`),
following [install-macos](install-macos.md); never overlay an existing production release.

## 2. Private configuration and credentials BEFORE startup

Read [SECURITY](../SECURITY.md) and [tailnet ACL requirements](tailscale-acl.md).
Configure the private `hosts.env` locally; SOCKS default is **127.0.0.1:10555** throughout.
Source it only after reviewing it, as it is shell code. Export `HARNESS_HOME` in future tool sessions too.

For **Linux SSH targets**, follow [install-ubuntu](install-ubuntu.md):

```bash
cp dispatcher/targets.example.json "$HARNESS_HOME/targets.json"
chmod 600 "$HARNESS_HOME/targets.json"
```

Edit that private file locally: remove unused example targets and supply `host`, `user`, `workspace`,
`entry`, `engine`, `ssh_key`; optional `switch_helper` is only for Claude model selection.
Provision a dedicated SSH key and restricted authorized_keys rule, verify host fingerprints out of band,
and populate `$HARNESS_HOME/ssh/known_hosts`. Confirm the target account has zsh and the native CLI
installed and authenticated. Do not print keys, tokens or credential files to test them.

For a **Mac HTTP target**, copy the three bridge Python files and config template as documented in
[install-macos](install-macos.md#1-files). Configure bind address, workspace, CLI paths and allowed sources.
Keep `server.require_per_source_tokens = true`. Provision a distinct API token per source through the
[private provisioning channel](deploy-and-rollback.md#token-channel-b-offline-encrypted-return) before launchd bootstrap.
Each source's `token_files` must point
to an owned regular non-symlink 0600 file; the dispatcher must hold its matching token at
`HARNESS_BRIDGE_TOKEN_FILE` (default `$HARNESS_HOME/bridge.token`). Never copy example token content
as a working credential. Report only field presence, ownership/mode validation and pass/fail.
Leave `webhook.conf` absent in the new runtime until phase 4. Do not remove an existing instance's file.

Only after the private configuration and credentials pass validation:
- On the Linux dispatcher, start `bash "$HARNESS_HOME/tailscale/start-tailscaled.sh"`, then log in once
  using `$HARNESS_HOME/bin/tailscale --socket="$HARNESS_HOME/run/tailscaled.sock" up` through the
  authorized interactive/private channel. Configure the approved dispatcher tag if required by the ACL.
  A kernel tailnet can instead use `HARNESS_SSH_PROXY=none` for SSH; Mac wait scripts still need their SOCKS route.
- On the Mac, render the launchd template with the absolute Python 3.11+ path and runtime paths,
  validate it with `plutil -lint`, then bootstrap as described in install-macos.
- Run the repository checks in phase 6 before using the installation. Do not start `ops/macos/cutover.py`
  as part of a fresh installation.

## 3. Minimal task WITHOUT wake, then authenticated readback

For SSH, from the dispatcher:

```bash
printf '%s\n' '{"backend":"ubuntu-a-codex","prompt":"Reply exactly OK; do not change files."}' \
  | python3 "$HARNESS_HOME/direct_ssh_client.py" submit
# Use your configured backend name, and the returned job_id below.
"$HARNESS_HOME/bin/job_wait.sh" <job_id> --no-ledger
python3 "$HARNESS_HOME/direct_ssh_client.py" status <job_id>
```

For Mac, run in **Bash**, using your configured tailnet address and source token file.
Ensure shell tracing is off; `AUTH_HEADER` must remain a non-exported local shell variable.

```bash
set +x
AUTH_HEADER=$(python3 "$HARNESS_HOME/bin/_bridge_auth.py") || exit 1
curl -q --fail --silent --show-error --proxy socks5h://127.0.0.1:10555 --noproxy '' \
  -H @/dev/fd/3 http://<mac-tailnet-ip>:18542/health 3< <(printf '%s\n' "$AUTH_HEADER")
curl -q --fail --silent --show-error --proxy socks5h://127.0.0.1:10555 --noproxy '' \
  -H @/dev/fd/3 -H 'Content-Type: application/json' \
  --data-binary '{"backend":"mac-codex","prompt":"Reply exactly OK; do not change files."}' \
  http://<mac-tailnet-ip>:18542/jobs 3< <(printf '%s\n' "$AUTH_HEADER")
unset AUTH_HEADER
"$HARNESS_HOME/bin/mac_wait.sh" <mac-tailnet-ip>:18542 <job_id> \
  "$HARNESS_HOME/results/first-mac-job.json" --no-ledger
```

Record job_id, backend, terminal status and result verification. Wait exit 0 means **terminal**, not
necessarily completed: inspect `status` and `error` before declaring success. Keep full output private.
If acceptance is uncertain, query/reconcile instead of blindly resubmitting.

## 4. Configure wake by default, only AFTER the first successful readback

The recommended default is to configure wake as the primary experience path, using the steps below
after the first successful readback and the explicit phase-0 choice. A recommendation is not consent;
missing credentials block this phase if wake was selected.

Only when the webhook URL or key is not yet available may phase 0 select "skip it for now". In that
case, leave wake disabled in the new runtime, skip provisioning and wake tests in this phase, and
continue to phase 5, where configuring and verifying compensating polling remains mandatory.
Record **wake not installed = degraded experience**. Do not request wake credentials in that case.

Follow [webhook-wake](webhook-wake.md). Have the operator provision the routine endpoint and key
privately; do not put either in chat, public files or command arguments.
- Mac: create the private 0600 `webhook.conf` with `WEBHOOK_URL` and `WEBHOOK_AUTH`. It is reread on push.
- SSH: set `HARNESS_WAKE_URL_PREFIX` (trusted HTTPS origin and optional path boundary) **before submit**,
  load `HARNESS_WAKE_KEY` privately into the dispatcher environment, then add
  `wake={url:<private endpoint>,key_env:HARNESS_WAKE_KEY}` to a private request file. Submit that file on stdin.
  The worker adds the Bearer prefix; do not include it in the key variable.

Wake is **best-effort**, may be lost or duplicated, and does not include job output. Test a tiny task
and a longer task. On receipt, use the private producer/backend mapping and saved job_id to read the
real job; follow [receive-then-read reference](../examples/wake-readback.md). A notification alone is
not result verification or proof of one-to-one ownership.

## 5. Mandatory compensation and boundaries

Polling and compensating readback must remain configured and verified as fallbacks even with wake
installed; they must not replace wake as the routine primary path. If wake is deferred because its
URL or key is unavailable, use verified periodic readback as the temporary degraded path.

Persist the returned job_id and trusted producer/backend route at submission. When a wake is overdue,
use the **same readback path** (`mac_wait.sh` or `job_wait.sh` / SSH `status`) with that job_id.
Arrange a bot-platform timer or periodic reconciliation of outstanding jobs; a fallback script sitting
unused is insufficient. Verify compensation with a job submitted without SSH wake (or on a separate
Mac test runtime with no webhook). If wake was selected, also verify that a wake-enabled job
demonstrates receipt and readback.
When wake is deferred, configure and verify periodic readback of outstanding jobs without notifications.
Never resubmit just because a wake was missed.

On SSH wait code 3 (worker gone) or stale queued/running state, inspect the remote CLI and reconcile
before clearing state or retrying. Timeout does not prove a remote process stopped. The SSH session
admission check is scoped to submitters sharing one local `HARNESS_HOME`; other dispatchers are independent.
Full concurrent idempotent ledger accounting/locking is outside this installation; the current wait
ledger helper does not guarantee exactly-once accounting. Use `--no-ledger` for smoke checks.
`push_file.sh` provides SHA-verified Linux SSH transfer, not `.bak` backups, overwrite conflict detection
or a complete deployment transaction. Any backup/rollback policy belongs in your deployment wrapper.

## 6. Checks and handoff

From the source checkout:

```bash
(cd bridge && python3 -B -m unittest discover -s tests -v)
for f in dispatcher/bin/*.sh dispatcher/relay/*.sh dispatcher/tailscale/*.sh ops/*.sh; do
  bash -n "$f" || exit 1
done
bash scripts/scrub_check.sh
```

Report source SHA, Python/OS/CLI versions actually used, installed script paths, private field presence
and permission checks, no-wake job/readback and compensation evidence separately. Report wake
job/readback evidence if selected; otherwise mark wake as deferred, not tested or validated, and
explicitly state **wake not installed = degraded experience** with compensation verification evidence.
Do not claim a live OS/CLI matrix from unit tests: repository CI verifies mocked behavior on Ubuntu /
Python 3.12; live macOS + Codex/Claude and Linux SSH + Codex/Claude require your own smoke results.
If credentials or machine access are missing, report installation as blocked at that phase, not validated.
