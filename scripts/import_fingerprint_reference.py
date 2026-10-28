#!/usr/bin/env python3
"""Import a maintainer-supplied numeric reference bank without model requests."""
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fingerprint_reference import import_reference_bank


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--source-label", required=True)
    parser.add_argument("--output", required=True, type=Path, help="new directory; existing contents are never replaced")
    args = parser.parse_args()
    try:
        result = import_reference_bank(args.source, args.output, expected_sha256=args.sha256,
                                       source_label=args.source_label)
    except (OSError, ValueError) as error:
        parser.exit(1, f"参考库导入失败：{error}\n")
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
