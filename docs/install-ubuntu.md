# Ubuntu / Linux hosts (SSH mode)

Ubuntu hosts are driven **in SSH mode only**: nothing is installed or kept running on the host. The dispatcher box
SSHes in per job and runs the native CLI. (The repository does not maintain a Linux bridge; the systemd template in
`bridge/service/` is an unsupported experiment.)

## On the host
1. Tailscale installed and logged in; note its tailnet IP.
2. An account for the agent (a dedicated one is recommended) with Codex and/or Claude Code installed and logged in,
   and `zsh` available (jobs run in `zsh -lic` so the login environment is loaded).
3. Codex permissions come from that account's `~/.codex/config.toml` (e.g. `sandbox_mode`, `approval_policy`).
4. Add the dispatcher's **dedicated** public key to `~/.ssh/authorized_keys`, with `restrict,from="<dispatcher-ip>"`
   and without agent/X11/port forwarding (`no-agent-forwarding,no-port-forwarding,no-X11-forwarding`).

## On the dispatcher box
```sh
export HARNESS_HOME=~/harness
mkdir -p "$HARNESS_HOME/ssh" && chmod 700 "$HARNESS_HOME" "$HARNESS_HOME/ssh"
ssh-keygen -t ed25519 -N '' -f "$HARNESS_HOME/ssh/id_ed25519_ubuntu-a" -C harness-ubuntu-a
# pin the host key (compare the fingerprint out of band before trusting it):
ssh-keyscan -t ed25519 <host-tailnet-ip> >> "$HARNESS_HOME/ssh/known_hosts"
cp dispatcher/targets.example.json "$HARNESS_HOME/targets.json" && chmod 600 "$HARNESS_HOME/targets.json"
python3 dispatcher/direct_ssh_client.py targets
```
With the userspace tailscaled from `dispatcher/tailscale/` the client uses `ProxyCommand=tailscale nc`; if the box has
a normal kernel tailnet interface set `HARNESS_SSH_PROXY=none`.

## Submit
```sh
echo '{"backend":"ubuntu-a-codex","prompt":"reply OK","model":"gpt-6.1-sol:medium",
       "wake":{"url":"<your_endpoint>","key_env":"HARNESS_WAKE_KEY"}}' | python3 dispatcher/direct_ssh_client.py submit
```
Without `wake`, wait with `dispatcher/bin/job_wait.sh <job_id>`. Copy files with `ops/push_file.sh`.

## Optional: Claude model switch helper
If the host selects Claude models through a helper script, set `switch_helper` in the target. The helper must print
shell `export` lines for the given model on `<helper> export <model>` and exit non-zero (listing candidates on
stderr) for unknown models. The selection applies to that one process only.

Use `restrict,from="<dispatcher-ip>" ssh-ed25519 <public-key>` for the dedicated key.
Keep OpenSSH updated and expose sshd only to the tailnet.
