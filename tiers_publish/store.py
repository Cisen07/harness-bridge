"""POSIX single-host publication using flock, fsync and atomic rename.

The shared directory is administrator owned; all writers must use this lock.
No hostile same-UID process may mutate it concurrently. No network filesystem
or distributed-lock guarantees are made.
"""

import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from datetime import datetime, timezone

from .validation import (MAX_BYTES, ValidationError, fields, integer, loads,
                         payload, policy, prose, require, token)


class Conflict(ValidationError):
    status = 409


class CapacityError(ValidationError):
    status = 507


# No automatic GC: retain all published/referenced versions and fail at this cap.
MAX_VERSIONS = 1000


def encoded(value):
    import json
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False) + "\n").encode("utf-8")
    require(len(raw) <= MAX_BYTES, "JSON exceeds 64 KiB")
    return raw


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def safe_path(path):
    """Reject symlinks in every existing component, including parent dirs."""
    for part in (path, *path.parents):
        require(not part.is_symlink(), "symlink path rejected")


def read_file(path):
    safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        require(stat.S_ISREG(os.fstat(handle.fileno()).st_mode), "not a regular file")
        raw = handle.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, "file exceeds 64 KiB")
    return raw


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_file(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


HEAD_FIELDS = ("schema_version", "revision", "payload_path", "payload_sha256",
               "created_at", "publisher", "reason", "previous_revision", "policy_revision")


class Store:
    def __init__(self, shared_dir):
        # abspath preserves symlink components for safe_path to inspect.
        self.root = Path(os.path.abspath(shared_dir))
        safe_path(self.root)
        self.home = self.root / "harness-tiers"
        self.versions = self.home / "versions"
        self.head = self.root / "harness-tiers.json"

    def _config_path(self, revision):
        return self.versions / str(revision) / "config.json"

    def _validate_head(self, head):
        fields(head, HEAD_FIELDS)
        require(type(head["schema_version"]) is int and head["schema_version"] == 1,
                "unsupported pointer schema")
        integer(head["revision"], 1)
        previous = head["previous_revision"]
        if previous is not None:
            integer(previous)
            require(previous < head["revision"], "invalid previous_revision")
        require(type(head["payload_sha256"]) is str and
                re.fullmatch(r"[0-9a-f]{64}", head["payload_sha256"]), "invalid SHA-256")
        token(head["policy_revision"])
        prose(head["publisher"], 128)
        prose(head["reason"])
        require(type(head["created_at"]) is str, "invalid timestamp")
        try:
            stamp = datetime.fromisoformat(head["created_at"].replace("Z", "+00:00"))
            require(stamp.utcoffset() is not None, "timestamp requires timezone")
        except ValueError as exc:
            raise ValidationError("invalid timestamp") from exc
        expected = self._config_path(head["revision"])
        # Accept existing absolute box paths and portable shared-relative paths.
        require(head["payload_path"] in (str(expected), str(expected.relative_to(self.root))),
                "payload_path outside controlled revision")
        safe_path(expected)
        return expected

    def get(self):
        head = loads(read_file(self.head))
        path = self._validate_head(head)
        raw = read_file(path)
        require(digest(raw) == head["payload_sha256"], "payload SHA-256 mismatch")
        config = payload(raw)
        return {"pointer": head, "config": config}

    def _history(self, head):
        """Follow only committed ancestors; tolerate imported legacy manifests.

        Existing versions need not carry this publisher's idempotency metadata.
        Their pointer chain may be incomplete, so lookup stops at that boundary.
        """
        revision = head["revision"]
        while revision:
            path = self.versions / str(revision) / "manifest.json"
            if not path.exists():
                break
            manifest = loads(read_file(path))
            require(type(manifest) is dict, "invalid manifest")
            if "revision" in manifest:
                require(type(manifest["revision"]) is int and manifest["revision"] == revision,
                        "manifest revision mismatch")
            if "idempotency_key" in manifest:
                token(manifest["idempotency_key"])
                require(type(manifest.get("request_sha256")) is str, "missing request fingerprint")
                require(type(manifest.get("pointer")) is dict, "missing publication receipt")
                self._validate_head(manifest["pointer"])
                require(manifest["pointer"]["revision"] == revision, "receipt revision mismatch")
            yield revision, manifest
            previous = manifest.get("previous_revision")
            if previous is None:
                break
            integer(previous)
            require(previous < revision, "invalid history chain")
            revision = previous

    def _publish_locked(self, raw, *, base_revision, publisher, reason,
                        idempotency_key, policy_raw, revision=None, rollback_of=None):
        policy_revision, allowed = policy(policy_raw)
        payload(raw, allowed)
        integer(base_revision)
        if revision is not None:
            integer(revision, 1)
        if rollback_of is not None:
            integer(rollback_of, 1)
        token(idempotency_key)
        prose(publisher, 128)
        prose(reason)
        fingerprint = digest(encoded({
            "payload_sha256": digest(raw), "base_revision": base_revision,
            "publisher": publisher, "reason": reason, "policy_sha256": digest(policy_raw),
            "revision": revision, "rollback_of": rollback_of,
        }))
        current = self.get()["pointer"] if self.head.exists() else None
        current_revision = current["revision"] if current else 0
        # Lost-response retry returns the original receipt even after newer heads.
        if current:
            for _, manifest in self._history(current):
                if manifest.get("idempotency_key") == idempotency_key:
                    if manifest.get("request_sha256") != fingerprint:
                        raise Conflict("idempotency key reused for a different request")
                    receipt = manifest["pointer"]
                    self._validate_head(receipt)
                    saved = read_file(self._config_path(receipt["revision"]))
                    require(digest(saved) == receipt["payload_sha256"], "receipt hash mismatch")
                    payload(saved, allowed)
                    return {"pointer": receipt, "replayed": True}
        if base_revision != current_revision:
            raise Conflict("stale base_revision; reload current pointer")
        allocated = []
        for entry in self.versions.iterdir():
            safe_path(entry)
            if re.fullmatch(r"[1-9][0-9]*", entry.name):
                require(entry.is_dir(), "version is not a directory")
                allocated.append(int(entry.name))
                manifest_path = entry / "manifest.json"
                if manifest_path.exists():
                    manifest = loads(read_file(manifest_path))
                    require(type(manifest) is dict, "invalid manifest")
                    if manifest.get("idempotency_key") == idempotency_key:
                        raise Conflict("idempotency key reserved by an uncommitted version")
        highest = max([current_revision, *allocated])
        if revision is None:
            revision = highest + 1
        if revision <= highest:
            raise Conflict("revision already used or not strictly increasing")
        if len(allocated) >= MAX_VERSIONS:
            raise CapacityError("version capacity reached; reviewed archival required")
        head = {
            "schema_version": 1, "revision": revision,
            "payload_path": str(self._config_path(revision).relative_to(self.root)),
            "payload_sha256": digest(raw),
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "publisher": publisher, "reason": reason,
            "previous_revision": current_revision, "policy_revision": policy_revision,
        }
        manifest = dict(head, pointer=head, idempotency_key=idempotency_key,
                        request_sha256=fingerprint, rollback_of=rollback_of)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.versions))
        pointer_tmp = None
        try:
            write_file(staging / "config.json", raw)
            write_file(staging / "config.sha256", (digest(raw) + "\n").encode("ascii"))
            write_file(staging / "manifest.json", encoded(manifest))
            fsync_dir(staging)
            os.rename(staging, self.versions / str(revision))
            fsync_dir(self.versions)
            fd, name = tempfile.mkstemp(prefix=".harness-tiers-", dir=self.root)
            pointer_tmp = Path(name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded(head))
                handle.flush()
                os.fsync(handle.fileno())
            # Sole commit point. Versions remain immutable, including orphans.
            os.replace(pointer_tmp, self.head)
            fsync_dir(self.root)
            return {"pointer": head, "replayed": False}
        finally:
            if staging.exists():
                shutil.rmtree(staging)
            if pointer_tmp is not None and pointer_tmp.exists():
                pointer_tmp.unlink()

    def _locked(self, action):
        safe_path(self.root)
        require(self.root.is_dir(), "shared directory must already exist")
        for directory in (self.home, self.versions):
            safe_path(directory)
            directory.mkdir(exist_ok=True, mode=0o700)
            fsync_dir(directory.parent)
        lock = self.home / "publish.lock"
        safe_path(lock)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            require(stat.S_ISREG(os.fstat(fd).st_mode), "lock must be regular file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            safe_path(self.head)
            return action()
        finally:
            os.close(fd)

    def publish(self, raw, **options):
        return self._locked(lambda: self._publish_locked(raw, **options))

    def rollback(self, source_revision, **options):
        integer(source_revision, 1)

        def action():
            current = self.get()["pointer"]
            committed = {current["revision"]}
            committed.update(rev for rev, _ in self._history(current))
            require(source_revision in committed, "source is not in committed history")
            path = self._config_path(source_revision)
            # Historical payload remains in the same controlled tree, with its
            # original sidecar digest. It is re-admitted under today's policy.
            raw = read_file(path)
            expected = read_file(path.parent / "config.sha256").decode("ascii").strip()
            require(expected == digest(raw), "historical SHA-256 mismatch")
            manifest_path = path.parent / "manifest.json"
            if manifest_path.exists():
                manifest = loads(read_file(manifest_path))
                require(type(manifest) is dict, "invalid manifest")
                if "payload_sha256" in manifest:
                    require(manifest["payload_sha256"] == expected, "historical manifest hash mismatch")
            if source_revision == current["revision"]:
                require(expected == current["payload_sha256"], "current hash mismatch")
            require(source_revision <= current["revision"], "source is not historical")
            return self._publish_locked(raw, rollback_of=source_revision, **options)

        return self._locked(action)
