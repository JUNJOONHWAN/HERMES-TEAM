from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path


def _load_module():
    root = Path(__file__).resolve().parents[2]
    path = root / "scripts" / "check_hermes_team_upgrade.py"
    spec = importlib.util.spec_from_file_location("hermes_team_upgrade", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.name=HERMES-TEAM Test",
            "-c",
            "user.email=hermes-team@example.invalid",
            *args,
        ],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _fixture_repo(tmp_path: Path, *, conflict: bool) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "shared.txt").write_text("base\n")
    _git(repo, "add", "shared.txt")
    _git(repo, "commit", "-m", "base")
    baseline = _git(repo, "rev-parse", "HEAD")

    _git(repo, "switch", "-c", "upstream")
    if conflict:
        (repo / "shared.txt").write_text("upstream\n")
        _git(repo, "add", "shared.txt")
    else:
        (repo / "upstream.txt").write_text("upstream\n")
        _git(repo, "add", "upstream.txt")
    _git(repo, "commit", "-m", "upstream")

    _git(repo, "switch", "main")
    if conflict:
        (repo / "shared.txt").write_text("team\n")
        _git(repo, "add", "shared.txt")
    else:
        (repo / "team.txt").write_text("team\n")
        _git(repo, "add", "team.txt")
    _git(repo, "commit", "-m", "team")

    contract = repo / "contract.json"
    contract.write_text(json.dumps({
        "schema": "hermes-team.upgrade-contract.v1",
        "project": "HERMES-TEAM",
        "upstream": {"baseline_commit": baseline},
        "validation_command": "tests",
    }))
    return repo, contract


def test_upgrade_preflight_reports_clean_three_way_merge(tmp_path: Path) -> None:
    module = _load_module()
    repo, contract = _fixture_repo(tmp_path, conflict=False)

    report = module.check_upgrade(repo, contract, "upstream")

    assert report["status"] == "clean"
    assert report["conflict_count"] == 0
    assert report["team_only_commits"] == 1
    assert report["upstream_only_commits"] == 1


def test_upgrade_preflight_reports_every_merge_conflict(tmp_path: Path) -> None:
    module = _load_module()
    repo, contract = _fixture_repo(tmp_path, conflict=True)

    report = module.check_upgrade(repo, contract, "upstream")

    assert report["status"] == "conflicts"
    assert report["conflict_count"] == 1
    assert report["conflict_paths"] == ["shared.txt"]
    assert "CONFLICT" in report["diagnostics"]
