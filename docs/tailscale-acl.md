# Tailscale ACL (minimum grants)

Tag the dispatcher box (e.g. `tag:dispatcher`) and give it only what it needs. Example policy fragment
(HuJSON, placeholders):

```jsonc
{
  "tagOwners": { "tag:dispatcher": ["autogroup:admin"] },
  "hosts": {
    "mac-a":    "100.x.y.1",
    "ubuntu-a": "100.x.y.3"
  },
  "grants": [
    // dispatcher -> Mac bridge port only
    { "src": ["tag:dispatcher"], "dst": ["mac-a"],    "ip": ["tcp:18542"] },
    // dispatcher -> Ubuntu host SSH only (OpenSSH, not Tailscale SSH)
    { "src": ["tag:dispatcher"], "dst": ["ubuntu-a"], "ip": ["tcp:22"] },
    // dispatcher -> optional relay host SSH (relay_via_host.sh)
    // relay host -> Mac bridge port (only if you use the relay)
    { "src": ["ubuntu-a"], "dst": ["mac-a"], "ip": ["tcp:18542"] }
  ]
}
```

Notes
- Keep the bridge's own `access.allowed_sources` in sync: the ACL decides who can connect, the bridge re-checks who is
  calling via `tailscale whois` (tag for the dispatcher, exact MagicDNS node name for peers).
- Do not grant the dispatcher `*:*`. Do not expose the bridge with Funnel/Serve.
- Prefer key expiry enabled on user devices; the tagged dispatcher node needs an auth key you rotate yourself.
- The webhook push goes from the Mac to the public internet (the routine URL); it does not need any tailnet grant.
