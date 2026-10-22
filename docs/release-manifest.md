# Release manifest template

Fill in locally for each release; never put credential values or token hashes here.
Store the completed topology-sensitive manifest privately, outside the public repo.

- Release commit (full SHA): `<commit>`
- Prepared by / time / review: `<owner, UTC, reviewer>`
- Release directory (no symlink components): `<absolute physical path>`
- Source rules: `<source IP placeholder, whois tag/node, allowed identity>`
- Token paths (no values): `<source: current absolute path, optional next absolute path>`
- Token validation (HMAC challenge; only 一致 or 不一致): `<一致 / 不一致>`
- Token rotation date: `<YYYY-MM-DD>`
- Real production configuration path (read-only review): `<absolute physical path>`
- Explicit production Codex sandbox: `<read-only / workspace-write / danger-full-access>`
- Explicit production Codex approval_policy: `<actual value>`
- Staged values match production / new startup `codex sandbox=` and approval_policy match: `<values / reviewer / outcome>`
- Relay key public fingerprint: `<SHA256 public-key fingerprint>`
- Rollback snapshot location: `<absolute path>`
- Rollback target commit / authentication enabled: `<commit / yes or no>`
- No-token rollback owner / deadline (max 24 hours; forbidden during incident): `<owner / UTC>`
- No-task cutover window / user notification: `<window / notification time>`
- Confirmation file path / external box job_id / outcome: `<path / job_id / outcome>`
- Post-confirm watch / terminal state: `<duration / outcome>`

| Artifact | Absolute installed path | SHA-256 |
| --- | --- | --- |
| Every release file (repeat for each file) | `<path>` | `<sha256>` |
| launchd plist | `<path>` | `<sha256>` |
| Forced-command wrapper | `<path>` | `<sha256>` |
| Each client script (repeat, including auth helper) | `<path>` | `<sha256>` |
| Rollback snapshot plist | `<path>` | `<sha256>` |

Generate file hashes locally with `shasum -a 256 <file>`; exclude token files, private
keys and credential-bearing config. Register the relay public key fingerprint with
`ssh-keygen -lf <public-key-file>`. Record the TW/box shared TW-identity credential
risk and the exact source rules approved for this release; keep tokens and their
hashes out of command output and this manifest.
