#!/usr/bin/env python3
"""Read-only HERMES-TEAM/upstream Hermes upgrade compatibility preflight."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


_CONTRACT_SCHEMA = "hermes-team.upgrade-contract.v1"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def _resolve(repo: Path, ref: str) -> str:
    proc = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if proc.returncode != 0:
        raise ValueError(f"cannot resolve git ref {ref!r}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    proc = _git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    if proc.returncode not in (0, 1):
        raise ValueError(proc.stderr.strip() or "git merge-base failed")
    return proc.returncode == 0


def _conflict_paths(output: str) -> list[str]:
    """Extract conflict paths from stable git merge-tree messages."""
    found: set[str] = set()
    patterns = (
        r"CONFLICT \([^)]*\): Merge conflict in (.+)$",
        r"CONFLICT \(modify/delete\): (.+?) deleted in ",
        r"CONFLICT \(rename/delete\): (.+?) renamed to (.+?) in ",
        r"CONFLICT \(file/directory\): directory in the way of (.+?);",
    )
    for line in output.splitlines():
        for pattern in patterns:
            match = re.search(pattern, line)
            if match:
                found.update(part.strip("'\"") for part in match.groups() if part)
    return sorted(found)


def check_upgrade(repo: Path, contract_path: Path, upstream_ref: str) -> dict[str, Any]:
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read upgrade contract {contract_path}: {exc}") from exc
    if contract.get("schema") != _CONTRACT_SCHEMA:
        raise ValueError(f"upgrade contract schema must be {_CONTRACT_SCHEMA!r}")

    upstream = contract.get("upstream")
    if not isinstance(upstream, dict):
        raise ValueError("upgrade contract requires an upstream object")
    baseline_ref = upstream.get("baseline_commit")
    if not isinstance(baseline_ref, str) or not baseline_ref:
        raise ValueError("upgrade contract requires upstream.baseline_commit")

    head = _resolve(repo, "HEAD")
    target = _resolve(repo, upstream_ref)
    baseline = _resolve(repo, baseline_ref)
    if not _is_ancestor(repo, baseline, head):
        raise ValueError("declared baseline is not an ancestor of HERMES-TEAM HEAD")
    if not _is_ancestor(repo, baseline, target):
        raise ValueError("declared baseline is not an ancestor of the upstream target")

    counts = _git(repo, "rev-list", "--left-right", "--count", f"{head}...{target}")
    if counts.returncode != 0:
        raise ValueError(counts.stderr.strip() or "cannot compute divergence")
    team_only, upstream_only = (int(value) for value in counts.stdout.split())

    preview = _git(
        repo,
        "merge-tree",
        "--write-tree",
        "--name-only",
        "--messages",
        head,
        target,
    )
    if preview.returncode not in (0, 1):
        raise ValueError(preview.stderr.strip() or "git merge-tree failed")
    diagnostics = "\n".join(
        part.strip() for part in (preview.stdout, preview.stderr) if part.strip()
    )
    conflicts = _conflict_paths(diagnostics)
    status = "clean" if preview.returncode == 0 else "conflicts"
    return {
        "schema": "hermes-team.upgrade-report.v1",
        "project": contract.get("project", "HERMES-TEAM"),
        "status": status,
        "head": head,
        "upstream_ref": upstream_ref,
        "upstream_commit": target,
        "baseline_commit": baseline,
        "team_only_commits": team_only,
        "upstream_only_commits": upstream_only,
        "conflict_count": len(conflicts),
        "conflict_paths": conflicts,
        "diagnostics": diagnostics if status == "conflicts" else "",
        "next_validation": contract.get("validation_command"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("distribution/upgrade/contract.json"),
    )
    parser.add_argument("--upstream-ref", default="upstream/main")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch the contract's upstream branch into the selected ref first.",
    )
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    repo = args.repo.resolve()
    contract_path = args.contract
    if not contract_path.is_absolute():
        contract_path = repo / contract_path
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        if args.fetch:
            upstream = contract["upstream"]
            destination = args.upstream_ref
            if not destination.startswith("refs/"):
                destination = f"refs/remotes/{destination}"
            proc = _git(
                repo,
                "fetch",
                "--no-tags",
                upstream["url"],
                f"+refs/heads/{upstream['branch']}:{destination}",
            )
            if proc.returncode != 0:
                raise ValueError(proc.stderr.strip() or "upstream fetch failed")
        report = check_upgrade(repo, contract_path, args.upstream_ref)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        print(f"HERMES-TEAM upgrade preflight error: {exc}", file=sys.stderr)
        return 2

    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        output = args.json_out
        if not output.is_absolute():
            output = repo / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "clean" else 1


if __name__ == "__main__":
    sys.exit(main())
