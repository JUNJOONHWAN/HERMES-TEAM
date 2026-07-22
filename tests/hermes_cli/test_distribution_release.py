from __future__ import annotations

import json
from pathlib import Path

import hermes_cli


def test_distribution_release_matches_runtime_versions():
    repo_root = Path(__file__).resolve().parents[2]
    payload = json.loads(
        repo_root.joinpath("distribution/release.json").read_text(encoding="utf-8")
    )

    assert payload["schema"] == "hermes.distribution-release.v1"
    assert payload["distribution_version"] == "0.1.5"
    assert payload["distribution_version"] == hermes_cli.__distribution_version__
    assert payload["engine_version"] == "0.18.0"
