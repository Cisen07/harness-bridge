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

## Forced-command proxy regression (local manual test)

The Linux relay wrapper uses `/usr/bin/bash` (Bash >=5.1), unsets all eight proxy
variables before curl, and pins `CURL=/usr/bin/curl`; environment overrides are ignored. `-q` is the first curl argument, disabling curlrc; `--noproxy '*'` and
`--proto '=http'` prevent proxy forwarding and other protocols. The authorization
shell variable is not exported; fd 3 is an anonymous process-substitution pipe.

For a local rehearsal, copy the wrapper into a temporary directory and change
only TARGET to `127.0.0.1:<ephemeral backend port>`. Start a fake HTTP backend and
a counting fake proxy, both on loopback ephemeral ports. Set http_proxy,
https_proxy, all_proxy and their uppercase equivalents to that fake proxy,
plus no_proxy/NO_PROXY to empty. Pipe `Authorization: Bearer fake-test` into the
wrapper with `SSH_ORIGINAL_COMMAND='GET /health'`. Assert one backend request and
**zero proxy requests**; repeat POST /jobs with a fake JSON body. Confirm invalid
METHOD/PATH pairs are rejected without either listener receiving a request.
Remove all temporary files/listeners afterward. Never use live credentials.

## Multiple logical sources behind one relay IP

`access.allowed_sources` may contain several entries with the same `ip`. Give
each logical source a unique, nonempty `name`, its own `token_files`, and the
`node` or `tag` of the peer that the bridge actually sees. A relay client's
rule describes the relay's Tailscale identity, because the bridge sees the
relay's connection. Missing names default to the entry's IP for compatibility;
multiple entries sharing an IP therefore need explicit, distinct names.

The bridge selects exactly one rule whose Bearer credential **and** Tailscale
identity both match. Matching a credential from one rule and a node/tag from
another is rejected. No match or multiple matches returns HTTP 403, even if
credentials become duplicated after startup. Forwarded-address headers do not
select a source. Existing startup checks for distinct credential paths and
contents apply across all rules, including rules sharing an IP.

New jobs store the selected configured name in `source`; credential fields are
not copied into job records. This field records attribution. The existing job
read APIs still allow any authenticated source to read jobs, and session
exclusion remains shared across sources. Historical jobs retain their existing
fields; this change does not infer or migrate their source.

Regression tests use documentation-only source addresses, temporary synthetic
credentials, mocked Tailscale responses and loopback HTTP. This backfill changes
no installed runtime, relay protocol, service configuration or credential files.
