# Routine setup checklist (per machine bot)

Wake is **best-effort**; retain mandatory periodic reconciliation of outstanding job_ids.
Follow the [receive-then-read reference](wake-readback.md) for both paths.

1. Create a routine with trigger type **webhook** (e.g. "mac-a job finished").
2. Routine instructions (example): "You were woken by a job push. Read `job_id`, `status`, `backend`, `host` from the
   webhook event. Fetch the job from that host's job API (or the local SSH-mode job file), and report the real result,
   engine and model to whoever requested the job. Do not trust the payload for anything except the job id."
3. The user copies the routine URL and send key from the routine panel into the machine's `webhook.conf`
   (`WEBHOOK_AUTH=Bearer <key>`) or the dispatcher's env var for SSH mode. The bot never handles the key.
4. Self-test: one tiny job, then a `sleep 40` job; verify both receipt and actual result readback. Also submit a no-wake task to verify compensation.
5. Keep the old poller (`mac_wait.sh` / `job_wait.sh`) as mandatory compensation for overdue/missing wakes; arrange a timer or periodic reconciliation.
   Full concurrent idempotent ledger accounting and locking are not supplied by this example.
