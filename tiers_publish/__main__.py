"""python3 -m tiers_publish --shared-dir DIR get|publish|rollback."""

import argparse
import json
from pathlib import Path
import sys

from .store import Store, ValidationError, read_file


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local atomic harness tiers publisher (no dispatch)")
    parser.add_argument("--shared-dir", required=True, help="existing private shared directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("get", help="verify and return current pointer + payload")
    for name in ("publish", "rollback"):
        command = commands.add_parser(name)
        if name == "publish":
            command.add_argument("--config", required=True)
        else:
            command.add_argument("--source-revision", required=True, type=int)
        command.add_argument("--base-revision", required=True, type=int)
        command.add_argument("--revision", type=int, help="optional explicit unused revision")
        command.add_argument("--policy", required=True, help="administrator controlled capability file")
        command.add_argument("--publisher", required=True)
        command.add_argument("--reason", required=True)
        command.add_argument("--idempotency-key", required=True)
    args = parser.parse_args(argv)
    try:
        store = Store(args.shared_dir)
        if args.command == "get":
            result = store.get()
        else:
            options = dict(base_revision=args.base_revision, revision=args.revision,
                           policy_raw=read_file(Path(args.policy).absolute()),
                           publisher=args.publisher, reason=args.reason,
                           idempotency_key=args.idempotency_key)
            if args.command == "publish":
                result = store.publish(read_file(Path(args.config).absolute()),
                                       **options)
            else:
                result = store.rollback(args.source_revision, **options)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except ValidationError as exc:
        print(json.dumps({"status": exc.status, "error": str(exc)}), file=sys.stderr)
        return 3 if exc.status == 409 else (2 if exc.status == 422 else 1)
    except (OSError, UnicodeError) as exc:
        # Never log file content, credentials or full argv.
        print(json.dumps({"status": 500, "error": type(exc).__name__,
                          "hint": "I/O failure; inspect head then retry with the same idempotency key"}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
