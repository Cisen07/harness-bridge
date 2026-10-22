# Relay shell and credential handling

The relay host must use **Bash 5.1 or newer** for `relay_via_host.sh`'s remote
command. Check `bash --version` on that host and configure the SSH account's
login shell to the absolute path of that Bash. Installing Bash alone does not
change the SSH login shell. Do not run that inline command under zsh or older
Bash: their here-doc implementations can briefly write the expanded Authorization
header to disk. Bash 5.1+ uses a pipe for a small here-doc that fits the pipe
buffer; the bridge token is bounded to 4096 bytes. Prefer the stdin/pipe wrapper
below to avoid relying on here-doc storage behavior at all. Do not use shell
tracing (`set -x`), credential-bearing command arguments, or temporary header files.

## Restricted SSH command wrapper (stdin protocol)

Install [forced-command.example.sh](../dispatcher/relay/forced-command.example.sh)
on the relay as a script owned by the relay account, in a directory other users
cannot write. Set its shebang to the absolute Bash >= 5.1 binary and replace
`TARGET` with the single trusted bridge host/port; chmod the installed script 0700.
Use a dedicated key and pin the
relay host key on the dispatcher. Example `authorized_keys` entry:

```text
restrict,from="<dispatcher-ip>",command="/absolute/private/relay-wrapper.sh" ssh-ed25519 <public-key>
```

This wrapper validates `SSH_ORIGINAL_COMMAND` without evaluating it, then reads
the Authorization header from SSH stdin and passes it to curl through an anonymous
pipe. The POST JSON body follows that first line unchanged. Secrets stay out of
argv and temporary files. Example dispatcher invocation (with private token file
and existing pinned known_hosts; substitute your own non-secret paths):

```bash
set -o pipefail
{ python3 "$HARNESS_HOME/bin/_bridge_auth.py" && cat body.json; } |
  ssh -T -i "$HARNESS_HOME/ssh/relay_key" -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$HARNESS_HOME/ssh/known_hosts" \
    relay@relay-host 'POST /jobs'
```

For GET, send only the header and use e.g. `GET /health`. This minimal wrapper
uses a different SSH command protocol from `relay_via_host.sh`: use the invocation
above (or adapt the caller to emit `METHOD PATH`), rather than pairing it directly
with the inline-command client. The forced command and target cannot be selected
by the caller. Network ACLs must still restrict relay-to-bridge reachability.
