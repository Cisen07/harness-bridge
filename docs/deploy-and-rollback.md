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
