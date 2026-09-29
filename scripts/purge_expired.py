#!/usr/bin/env python3
"""Enforce the retention policy (CLAUDE.md, docs/compliance.md).

data/raw/ and data/llm/: 180 days. data/media/: 90 days. Age is file mtime.
Dry run by default; pass --apply to delete.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from common import DATA_DIR

RETENTION_DAYS = {"raw": 180, "llm": 180, "media": 90}


def expired(data_dir: Path, now: float) -> list[Path]:
    out = []
    for sub, days in RETENTION_DAYS.items():
        root = data_dir / sub
        if root.exists():
            cutoff = now - days * 86400
            out += [p for p in root.rglob("*") if p.is_file() and p.stat().st_mtime < cutoff]
    return sorted(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="actually delete")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = ap.parse_args(argv)
    files = expired(args.data_dir, time.time())
    for f in files:
        print(("deleting " if args.apply else "would delete ") + str(f))
        if args.apply:
            f.unlink()
    print(f"{len(files)} expired file(s)" + ("" if args.apply else " (dry run; --apply to delete)"),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
