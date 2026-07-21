from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

from hermes_cli.artifact_health import build_artifact_health


NOW = datetime(2026, 7, 21, 3, 0, tzinfo=timezone.utc)


def test_artifact_health_is_safe_when_not_configured(tmp_path):
    result = build_artifact_health(now_utc=NOW, hermes_home=tmp_path, config={})

    assert result["enabled"] is False
    assert result["healthy"] is True
    assert result["status"] == "not_configured"
    assert result["checks"] == []


def test_path_contract_checks_relative_file_size_age_and_hash(tmp_path):
    artifact = tmp_path / "outputs" / "report.json"
    artifact.parent.mkdir()
    artifact.write_text('{"status":"complete"}\n', encoding="utf-8")
    stamp = (NOW - timedelta(seconds=30)).timestamp()
    os.utime(artifact, (stamp, stamp))
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    result = build_artifact_health(
        now_utc=NOW,
        hermes_home=tmp_path,
        config={
            "enabled": True,
            "checks": [
                {
                    "name": "daily-report",
                    "type": "path",
                    "path": "outputs/report.json",
                    "kind": "file",
                    "required": True,
                    "min_bytes": 10,
                    "max_age_seconds": 60,
                    "sha256": digest,
                }
            ],
        },
    )

    assert result["healthy"] is True
    assert result["total"] == 1
    assert result["checks"][0]["status"] == "ok"
    assert result["checks"][0]["evidence"]["actual_sha256"] == digest


def test_required_missing_and_optional_missing_are_distinct(tmp_path):
    result = build_artifact_health(
        now_utc=NOW,
        hermes_home=tmp_path,
        config={
            "enabled": True,
            "checks": [
                {"name": "required", "path": "missing-a", "required": True},
                {"name": "optional", "path": "missing-b", "required": False},
            ],
        },
    )

    assert result["healthy"] is False
    assert result["healthy_count"] == 1
    assert [row["status"] for row in result["checks"]] == [
        "missing",
        "optional_missing",
    ]


def test_stale_and_invalid_contracts_fail_closed(tmp_path):
    artifact = tmp_path / "old.txt"
    artifact.write_text("x", encoding="utf-8")
    stamp = (NOW - timedelta(hours=2)).timestamp()
    os.utime(artifact, (stamp, stamp))

    result = build_artifact_health(
        now_utc=NOW,
        hermes_home=tmp_path,
        config={
            "enabled": True,
            "checks": [
                {"name": "old", "path": "old.txt", "max_age_seconds": 30},
                {"name": "bad-kind", "path": "old.txt", "kind": "socket"},
                {"name": "bad-type", "type": "command", "path": "old.txt"},
            ],
        },
    )

    assert result["healthy"] is False
    assert [row["status"] for row in result["checks"]] == [
        "stale",
        "invalid_contract",
        "unsupported_type",
    ]
