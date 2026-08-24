#!/usr/bin/env python3
"""Write a content-addressed request for the privileged Palace materializer."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))

from palace_snapshot_request import (  # noqa: E402
    write_palace_execution_snapshot_request,
)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("config_manifest")
    parser.add_argument("--executable", required=True)
    parser.add_argument("--build-manifest", required=True)
    parser.add_argument("--processes", type=int, default=1)
    parser.add_argument("--snapshot-id")
    args = parser.parse_args(argv)
    try:
        path, _ = write_palace_execution_snapshot_request(
            args.config_manifest,
            executable=args.executable,
            build_manifest_path=args.build_manifest,
            processes=args.processes,
            snapshot_id=args.snapshot_id,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
