#!/usr/bin/env python3
"""Register a Codex or other command adapter from a reviewed JSON spec."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.public_edition import register_external_adapter
from hermes_constants import get_hermes_home


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--home", type=Path, default=get_hermes_home())
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--skip-live-health", action="store_true")
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    result = register_external_adapter(
        home=args.home.expanduser().resolve(),
        repo_root=args.repo_root.expanduser().resolve(),
        spec=spec,
        live_health=not args.skip_live_health,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
