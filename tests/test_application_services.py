from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from hermes_cli import application_services as services


def _catalog(path: Path) -> None:
    path.write_text(json.dumps({
        "aliases": {"all": ["demo"]},
        "services": {"demo": {"unit": "hermes-managed@demo.service", "desired_state": "running"}},
    }))


def test_explicit_stop_persists_before_systemctl(monkeypatch, tmp_path):
    catalog = tmp_path / "services.json"
    _catalog(catalog)
    calls = []

    def fake_systemctl(*args, check=False):
        calls.append(args)
        if args[0] == "is-active":
            desired = json.loads(catalog.read_text())["services"]["demo"]["desired_state"]
            return SimpleNamespace(stdout="inactive\n" if desired == "stopped" else "active\n", stderr="", returncode=0)
        if args[0] == "is-enabled":
            return SimpleNamespace(stdout="disabled\n", stderr="", returncode=0)
        if args[0] == "show":
            return SimpleNamespace(stdout="MainPID=0\nNRestarts=0\nResult=success\n", stderr="", returncode=0)
        assert json.loads(catalog.read_text())["services"]["demo"]["desired_state"] == "stopped"
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(services, "_systemctl", fake_systemctl)
    report = services.set_state("stop", ["demo"], catalog=catalog)
    assert report["in_sync"] is True
    assert calls[0] == ("disable", "--now", "hermes-managed@demo.service")


def test_reconcile_does_not_restart_explicitly_stopped(monkeypatch, tmp_path):
    catalog = tmp_path / "services.json"
    _catalog(catalog)
    data = json.loads(catalog.read_text())
    data["services"]["demo"]["desired_state"] = "stopped"
    catalog.write_text(json.dumps(data))
    calls = []

    def fake_systemctl(*args, check=False):
        calls.append(args)
        if args[0] == "is-active":
            return SimpleNamespace(stdout="inactive\n", stderr="", returncode=0)
        if args[0] == "is-enabled":
            return SimpleNamespace(stdout="disabled\n", stderr="", returncode=0)
        return SimpleNamespace(stdout="MainPID=0\nNRestarts=0\nResult=success\n", stderr="", returncode=0)

    monkeypatch.setattr(services, "_systemctl", fake_systemctl)
    report = services.reconcile(all_services=True, catalog=catalog)
    assert report["status"] == "ok"
    assert report["actions"] == []
    assert not any(call[0] in {"start", "restart", "enable"} for call in calls)
