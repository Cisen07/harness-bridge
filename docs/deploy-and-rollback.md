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
ops/token-provision/decrypt_store.sh /private/key-dir/private.pem /private/bridge.token --require-mode sha256 --wipe-key < envelope.txt
```

Decryption honors the envelope's explicit OAEP digest and creates the destination
exclusively with 0600 permissions. Existing files are never overwritten. Errors
are generic; if encryption fails after creation, the Mac token file remains for
operator inspection/removal before retry. Keep all paths in private directories.
`--wipe-key` overwrites with zeroes, fsyncs, truncates to zero, fsyncs again, then deletes the one-shot private key only after
successful storage. Box policy may require retaining the empty inode: use
`--wipe-key=truncate-only` to overwrite, fsync and truncate without unlinking.
Encryption requires an owned 0700 target parent directory. Decryption validates
visible ASCII without whitespace (16..4096 bytes, allowing one final newline as
file framing) before writing. Recommend `--require-mode sha256` on the box to
reject SHA-1 envelopes; on encrypt it disables the compatibility fallback. APFS/SSD snapshots and copy-on-write prevent guaranteed physical erasure;
exclude the one-shot directory from backups and use encrypted storage. Never send
the private key or plaintext token back through the conversation/job output.

## Cutover script usage

`ops/macos/cutover.py` uses Python 3, avoiding Bash 3.2 incompatibilities. Prepare
and inspect both new and snapshot plists (matching Label) in private immutable
release directories; old plist must point to the previous code/config, new plist
to the staged release. This script restores the plist, not overwritten source files.
`--expect-release`, `--plist-new` and `--plist-snapshot` must name real absolute
paths, with no symlink components (including parent directories). Use physical
paths such as `/private/...` rather than a symlink alias; the program/script and
configuration paths inside each plist must also name the real release files.
Ensure no other work is running and the snapshot is known good.

Before `--execute`, read the production configuration without changing it. Check
that `[codex] sandbox` and `approval_policy` are both explicitly present, record
the exact values and configuration path in the private release manifest, and
ensure the staged configuration matches. A missing value must be resolved before
execution; do not infer production behavior from the code defaults. After the new
instance starts, compare its startup log line containing `codex sandbox=` and
`approval_policy=` with the manifest, and record the comparison before confirmation.
Example (replace these real-path placeholders with the reviewed release layout):

```bash
python3 -B ops/macos/cutover.py --label com.example.bridge \
  --plist-new /private/release/new/bridge.plist --plist-snapshot /private/release/old/bridge.plist \
  --bind <mac-tailnet-ip>:18542 --state-file /private/release/cutover.json \
  --self-check-url http://<mac-tailnet-ip>:18542/health --delay 5 \
  --expect-release /private/release/new \
  --confirm-file /private/release/confirmation --confirm-nonce <fresh-nonce> \
  --confirm-timeout 600 --post-confirm-watch 300 --dry-run
```

No execution occurs by default. Replace `--dry-run` with `--execute` for the actual
cutover. It double-forks and calls setsid before sleeping, so a job can restart its
own bridge. It uses `launchctl bootout`, atomic copy/rename into
`~/Library/LaunchAgents/<label>.plist`, then `launchctl bootstrap`. In the health
window (60 seconds), lsof must show exactly the expected address/port and one PID,
stable for at least 30 seconds. That PID must equal `launchctl print gui/<uid>/<label>`'s
pid and `ps -o command= -p PID` must include an absolute program/script path inside
`--expect-release` (a distinct, immutable release directory). A foreign/old listener
or old release argv fails and triggers rollback. Execute requires `--self-check-url`,
`--expect-release`, `--confirm-file`, and `--confirm-nonce`; dry-run does not.
The self-check uses no token, proxy or redirects and expects HTTP 403.

After switching, Air and TW must **each run one slider retest**. Only after
**both pass** may the external box use its **box source token** to submit a small
job that creates the confirmation file. If either retest fails, do not write the
confirmation file; let the confirmation deadline trigger automatic rollback.

The confirmation file must be owned by the bridge user, regular, non-symlink,
mode 0600, containing exactly the nonce and one newline. Use a fresh nonce and a
previously absent private file path for every attempt: startup refuses an existing
path, including a dangling symlink. Have the box job create a temporary file with
mode 0600 **in the same directory**, write the nonce and newline, flush/fsync and
close it, then rename it atomically to the confirmation path. Do not prepare the
confirmation file locally: this step proves the external job path. Missing, empty,
short, newline-incomplete or not-yet-0600 files keep waiting until the deadline.
A symlink, non-regular file, wrong owner, or complete wrong content fails at once.
The default confirmation deadline is 600 seconds; timeout, unsafe file or
wrong content restores the snapshot. While waiting, the listener identity is
checked every two seconds. After confirmation, watch for 300 seconds, checking
every 30 seconds and at the end: changed PID, lost listener or release argv mismatch
restores the snapshot. Both durations are configurable; use production defaults.

Snapshot plists are checked before arming: owned regular non-symlink files, mode
0600 or 0644. Validated snapshot bytes are retained for rollback. Poll the JSON
state file (0600, atomic and fsynced): `armed`, `switched`, `self_check`,
`awaiting_confirm`, `confirmed`, `watching`, `complete`, `rolled_back`,
`rollback_failed`. Only phases, check results, PID and timestamps are stored; no
nonce, token, hashes or command output. Rollback checks launchd/listener identity
and HTTP 403 without requiring the new release path. A no-token old version may
therefore restore successfully but fail that HTTP check: investigate `rollback_failed`.
Do not race multiple cutovers, change snapshots during execution, or put secrets in
URL/path/label arguments. This tool is not run automatically during installation.

Cutover has an approximately **5–70 second service gap**. Choose a window with no
queued/running jobs and notify users beforehand. If a client's POST has a connection
error and it is certain no `job_id` was received, wait 30 seconds and retry once.
If acceptance is uncertain, reconcile the request before resubmission to avoid duplicates.

Rolling back to a version without token authentication is temporary security debt,
limited to **24 hours per rollback**. Never roll back to a no-token version during a
security incident. Record the deadline and owner, and restore authentication before it expires.

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
rules; all slots are compared without stopping on a match.
`server.require_per_source_tokens = true` is the default: every allowed source must
have `token_files`, each an absolute path. Only an explicit `false` enables global
`server.token_file` fallback (including its legacy environment override). Production
must keep the default. Startup rejects shared paths across sources (including legacy fallback) and equal token
contents across sources or slots, comparing with `compare_digest` without logging values.
Startup requires current; a missing next is normal, but an existing invalid next
is a configuration error. Request failures always return the same 403. Publish a
new next atomically, provision it on the caller using channel B, switch the caller,
then atomically replace current and remove next. To revoke one source, remove its
current file (requests fail closed even if next exists); other source tokens work.
Before the next restart remove/reconfigure that revoked rule, since a missing
current deliberately refuses startup. No production self-source or ACL changes
are required for rotation.

At runtime, an invalid existing next slot is ignored and increments the in-memory
`INVALID_NEXT_SLOTS` counter once for that request; current remains usable. No slot
content, command output or exception text is logged. A missing next is normal and
is not counted. Current remains mandatory even when next is valid.

## HMAC challenge (box identity proof)

The **Mac is the verifier**: it generates a fresh challenge and runs `verify`.
The **box only runs `make`**, using its own copy of the box source token and the
Mac-issued nonce; it never issues or verifies this challenge. The Mac saves each
fresh challenge at a previously absent private path (0600). These commands use only credential paths, never values:

```sh
# Mac: generate and retain the challenge; this command prints nothing.
python3 ops/token-provision/hmac_challenge.py verify --token-file /private/box-current \
  --challenge-file /private/hmac-nonce --new-challenge
# Transfer only the nonce from that file to the maker over an authenticated channel.
# Box only: make the response using the nonce received from the Mac.
python3 ops/token-provision/hmac_challenge.py make --token-file /private/box-current --nonce <verifier-nonce>
# Transfer the returned JSON mac to the Mac. Its saved challenge is consumed.
python3 ops/token-provision/hmac_challenge.py verify --token-file /private/box-current \
  --challenge-file /private/hmac-nonce --mac <response-mac>
```

The nonce is 32 random bytes; the proof is HMAC-SHA256(token, nonce). The make
command requires the verifier's nonce and cannot select its own. Final verification
prints only `一致` (exit 0) or `不一致` (exit 1); no token or token hash is exposed.
Each verification consumes its local challenge, even on mismatch; generate a new
challenge before retrying. This proves possession, not network origin. Token and
challenge paths must be owned regular non-symlink files, mode 0600.
On the Mac, add `--truncate-only` to `verify --mac ...` to consume the saved
challenge by truncating it to zero and fsyncing without deleting its inode, on
both match and mismatch. It cannot be replayed; use a new absent path next time.
This option is not valid with `--new-challenge`.

## Relay identity risk

Requests relayed through TW appear to the MBP as TW source traffic and use the
TW source token. TW and box each hold a copy. The credential's impact is limited
to the TW identity; it does not authorize the independent box source identity.
Record this accepted risk and the source rules in the release manifest.

## Client argv/environ token self-check

Use only `-H @/dev/fd/3` with an anonymous pipe for manual curl headers. Keep
`AUTH_HEADER` a non-exported shell variable, disable shell tracing, and unset it
after use. Never interpolate token file contents into a command argument.
For rehearsal, use a unique **fake** marker and a slow loopback HTTP server so curl
remains alive. Inspect `ps -o command= -p <curl-pid>` (and the parent shell) for
absence of the marker; inspect `/proc/<pid>/environ` on Linux, or `ps eww -p <pid>`
on macOS, for the same absence. Automate the comparison in memory and emit only
pass/fail; never dump a real process environment or use a real token for this check.
Also check the dispatcher and relay process chain. Header bytes should exist only
in private token files, process memory and fd 3, never argv, exported variables or logs.

## Codex sandbox migration

The code default is `codex.sandbox = "workspace-write"`, with
`approval_policy = "never"`. If the migrated deployment depends on
`danger-full-access` (for example browser or slider jobs), production configuration
must explicitly set `codex.sandbox = "danger-full-access"`. Review that choice
before switching; the startup log reports the actual sandbox and approval policy.
Token paths (current and next) must not be symlinks.

Cutover retries lsof command errors twice before treating the third consecutive
failure as a failed listener observation, with a one-second pause between retries.
Unsafe or mismatched output fails immediately; failed observations reset PID stability. An old `/health` returning
200 cannot pass the tokenless 403 self-check or mark rollback healthy in state.

Both new and rollback check records store `http_status` separately (null until an
HTTP response is received). Only HTTP 403 passes; 200 is recorded but fails.
