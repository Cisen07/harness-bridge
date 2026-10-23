# Human installation appendix

Start with the short paste at the top of [README](README.md). The agent then follows the
[internal installation playbook](docs/agent-install-prompt.md), beginning with prerequisite choices.
Use this appendix when installing manually; follow the same order.

1. **Layout and prerequisites:** Python 3.11+ on the dispatcher and Mac (CI: 3.12),
   separate clean source checkout and private runtime. Install all `dispatcher/bin/*`, including
   `_bridge_auth.py`, and `direct_ssh_client.py`; keep the installed `tailscale/` beside `bin/`.
   The supplied userspace tailscaled launcher targets a Linux amd64 dispatcher, SOCKS port 10555.
2. **Before startup:** review [SECURITY](SECURITY.md) and [tailnet ACLs](docs/tailscale-acl.md).
   Configure private `hosts.env`, SSH `targets.json`/dedicated keys/known_hosts, or Mac source-bound
   API token files (owned, regular, non-symlink, mode 0600). Keep private values out of the checkout.
3. **Start and verify without wake:** follow [Mac](docs/install-macos.md) or
   [Linux SSH](docs/install-ubuntu.md). Start only after private files are provisioned. Submit
   a tiny task, retain its job_id, then use authenticated GET / wait / status to verify completed + result.
4. **Wake (recommended default):** after the prerequisite choice and first successful readback, provision private routine settings using [webhook wake](docs/webhook-wake.md),
   then test receipt **and** real-result readback via the [receiver reference](examples/wake-readback.md).
   Wake is the primary experience path; defer only when the webhook URL or key is unavailable,
   mark wake not installed as a degraded experience, and continue to mandatory compensation validation.
5. **Compensate:** retain polling and periodic reconciliation of submitted job_ids as mandatory fallbacks,
   not the routine primary path. Verify this path with a no-wake task, including when wake is deferred.
   Do not resubmit uncertain jobs; inspect SSH worker loss/remote timeouts.
6. **Record evidence:** chosen source SHA, actual OS/Python/CLI versions, tests and per-transport
   smoke results. Pinning a release SHA is recommended; discover personal deployment drift and iterate.

See the [transport differences](docs/architecture.md#transport-differences) and
[deployment/rollback boundaries](docs/deploy-and-rollback.md). Unit tests do not prove live credentials,
CLI compatibility, webhook delivery or fleet-wide deployment convergence. `push_file.sh` verifies transfer;
it does not provide automatic backups or a complete deployment transaction.
