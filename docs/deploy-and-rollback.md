# Deploy and roll back a bridge change (macOS)

Run these on the Mac (by hand, or as a job on that Mac's own bridge). `D` = bridge directory, `L` = launchd label.

1. **Baseline**: `shasum -a 256 $D/codex_bridge.py $D/agent_adapters.py $D/bridge_config.py` must equal what you
   expect to be running; otherwise stop and find out why.
2. **Backup**: `TS=$(date +%Y%m%d%H%M%S); for f in codex_bridge.py agent_adapters.py bridge_config.py jobs.json; do cp -p $D/$f $D/$f.bak-$TS; done`
3. **Stage + check**: write new files as `*.new`, `python3 -B -m py_compile` them, run the unit tests, compare sha256,
   then `mv` each into place (never edit in place).
4. **Restart detached**: make sure no other job is queued/running (a restart marks them `interrupted` and pushes that
   state). From inside a job use `python3 -B ops/restart_bridge_detached.py $L` - a plain `nohup ... &` is reaped with
   the job's process group. The job issuing the restart ends `interrupted` (expected).
5. **Verify from the dispatcher**: `GET /health`, `GET /backends`, one tiny job per backend, and the webhook wake.

## Rollback
`cp -p $D/<file>.bak-$TS $D/<file>.tmp && mv $D/<file>.tmp $D/<file>` for each file, then the same detached restart.
`jobs.json` stays compatible in both directions (new fields are extra keys).

## Lessons learned
- Use `python3 -B` everywhere so no `__pycache__` is created next to the bridge.
- Never use `launchctl submit` (KeepAlive restarts it forever); use `kickstart -k`.
- Keep `webhook.conf` out of the deploy bundle; it is per machine and contains the key.

## Private logs and retention
The detached restart helper defaults to
`~/Library/Logs/harness-bridge/restart.log`, with a private directory and a 0600
new log file; it refuses symlink log targets. Keep custom `--log` paths in a
private directory rather than shared `/tmp`.

Run a daily retention review/cleanup on the dispatcher. `direct-jobs/*.request`
contains prompts and may contain wake URLs: remove requests for confirmed
terminal jobs after 7 days, once result collection and wake reconciliation are
complete. Never remove requests for queued/running jobs or while a worker may
still need them. Retain result records and webhook deduplication ids for 30 days
by default; extend that window if operational replay requires it.

Watchdog `state.bak-*` directories contain Tailscale node keys, just like the
live `state/` directory. Keep them private (0700 directories, 0600 files), out of
shared backups and archives. Review daily; remove backups older than 7 days only
after verifying the current daemon/state is healthy, retaining the newest known
good backup until then. Never remove live `state/`. Cleanup is an operator task;
these retention periods are recommendations, not an installed automatic job.

## Token channel B (offline encrypted return)

Use a new one-shot RSA-3072 keypair for each transfer. On the box, create a private
0700 directory, then run:

```bash
ops/token-provision/gen_keypair.sh /private/key-dir > public.pem
```

Transfer only `public.pem` to the Mac using the approved channel. On the Mac:

```bash
ops/token-provision/make_token_and_encrypt.sh public.pem /private/source-current.token > envelope.txt
```

The token is generated with Python `secrets.token_urlsafe(32)`, written exclusively
(O_EXCL, 0600); plaintext never enters argv, environment, stdout or stderr.
The first envelope line is `OAEP-SHA256`; LibreSSL installations lacking the digest
option fall back to `OAEP-SHA1` (OAEP default compatibility). The second line is
base64 ciphertext. Transfer this envelope back to the box; there run:

```bash
ops/token-provision/decrypt_store.sh /private/key-dir/private.pem /private/bridge.token --wipe-key < envelope.txt
```

Decryption honors the envelope's explicit OAEP digest and creates the destination
exclusively with 0600 permissions. Existing files are never overwritten. Errors
are generic; if encryption fails after creation, the Mac token file remains for
operator inspection/removal before retry. Keep all paths in private directories.
`--wipe-key` overwrites, fsyncs, then deletes the one-shot private key only after
successful storage; alternatively use `shred -u` where available, or overwrite and
unlink. APFS/SSD snapshots and copy-on-write prevent guaranteed physical erasure;
exclude the one-shot directory from backups and use encrypted storage. Never send
the private key or plaintext token back through the conversation/job output.

## Cutover script usage

`ops/macos/cutover.py` uses Python 3, avoiding Bash 3.2 incompatibilities. Prepare
and inspect both new and snapshot plists (matching Label) in private immutable
release directories; old plist must point to the previous code/config, new plist
to the staged release. This script restores the plist, not overwritten source files.
Ensure no other work is running and the snapshot is known good. Example:

```bash
python3 -B ops/macos/cutover.py --label com.example.bridge \
  --plist-new /private/release/new.plist --plist-snapshot /private/release/old.plist \
  --bind <mac-tailnet-ip>:18542 --state-file /private/release/cutover.json \
  --self-check-url http://<mac-tailnet-ip>:18542/health --delay 5 --dry-run
```

No execution occurs by default. Replace `--dry-run` with `--execute` for the actual
cutover. It double-forks and calls setsid before sleeping, so a job can restart its
own bridge. It uses `launchctl bootout`, atomic copy/rename into
`~/Library/LaunchAgents/<label>.plist`, then `launchctl bootstrap`. In the health
window (60 seconds), lsof must show exactly the expected address/port and one PID,
with two observations at least 30 seconds apart. Optional self-check uses no token,
no proxy, no redirects, and expects 403 (proves the authenticated listener answers).
Any switch/check failure triggers snapshot restore/bootstrap and one further health
window. Poll the JSON state file (0600, atomic writes): `complete`, `rolled_back`,
or `rollback_failed` is terminal; intermediate stages are `delay`, `cutover`, `rollback`.
Only check booleans, phases and timestamps are written; subprocess output is suppressed.
Do not race multiple cutovers, change snapshots during execution, or put secrets in
URL/path/label arguments. This tool is not run automatically during installation.

## Source-bound tokens and rotation

```toml
[access]
allowed_sources = [
  { ip = "<box-tailnet-ip>", node = "box.example.ts.net", token_files = ["/private/box-current", "/private/box-next"] },
  { ip = "<peer-tailnet-ip>", tag = "tag:dispatcher", token_files = ["/private/peer-current"] },
]
```

Each source must pass IP + whois identity + its own Bearer slots. One or two paths
are accepted, each re-read with the same owned-regular-file/no-symlink/0600/content
rules; all slots are compared without stopping on a match. Omitted `token_files`
uses the global `server.token_file` (including its legacy environment override).
Startup requires current; a missing next is normal, but an existing invalid next
is a configuration error. Request failures always return the same 403. Publish a
new next atomically, provision it on the caller using channel B, switch the caller,
then atomically replace current and remove next. To revoke one source, remove its
current file (requests fail closed even if next exists); other source tokens work.
Before the next restart remove/reconfigure that revoked rule, since a missing
current deliberately refuses startup. No production self-source or ACL changes
are required for rotation.
