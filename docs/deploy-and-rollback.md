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
