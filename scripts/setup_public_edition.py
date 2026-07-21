#!/usr/bin/env python3
"""Install the portable Hermes governance edition on an initialized Hermes home."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.public_edition import setup_public_edition
from hermes_constants import get_hermes_home


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=get_hermes_home())
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--skip-opencode-install", action="store_true")
    parser.add_argument("--skip-timeline-install", action="store_true")
    parser.add_argument("--skip-live-health", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = setup_public_edition(
        home=args.home,
        repo_root=args.repo_root,
        install_opencode_binary=not args.skip_opencode_install,
        install_timeline=not args.skip_timeline_install,
        live_health=not args.skip_live_health,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
