from __future__ import annotations

import json
from pathlib import Path

import yaml

from hermes_cli import public_edition


def test_configure_timeline_catalog_is_portable_and_writes_opencode_bundle(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump({"model": {"default": "test"}}), encoding="utf-8"
    )

    result = public_edition.configure_timeline_catalog(home=home)

    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    timeline = config["mcp_servers"]["hermes-timeline-code-map"]
    assert timeline["args"] == ["-m", "hermes_timeline_code_map.mcp_server"]
    assert timeline["env"]["TIMELINE_CODE_MAP_DB_PATH"].startswith(str(home))
    assert config["supervisor"]["timeline_db"] == result["timeline_db"]
    opencode = json.loads(
        (home / "supervisor" / "mcp" / "opencode.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(opencode["mcp"]) == {"hermes-timeline-code-map"}


def test_neural_plugin_install_has_normal_empty_recall_contract(tmp_path):
    repo_root = tmp_path / "repo"
    source = (
        repo_root
        / "extensions/hermes-timeline-code-map/deploy/hermes_plugin/timeline-neural-link"
    )
    source.mkdir(parents=True)
    (source / "plugin.yaml").write_text("name: timeline-neural-link\n", encoding="utf-8")
    (source / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    home = tmp_path / "home"

    result = public_edition.install_neural_link_plugin(
        home=home, repo_root=repo_root
    )

    assert result["empty_recall_behavior"] == "no_context_injected"
    assert (home / "plugins/timeline-neural-link/plugin.yaml").is_file()


def test_opencode_launch_uses_bridge_timeline_and_live_free_health(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    launch = public_edition._opencode_launch_config(
        home=home, repo_root=repo, opencode_path="/opt/bin/opencode"
    )

    rendered = json.dumps(launch)
    assert "hermes_cli.external_cli_adapter" in rendered
    assert "hermes_cli.opencode_free_router" in rendered
    assert "--health-check" in rendered
    assert "hermes_timeline_cli.py" in rendered
    assert "/home/private-user" not in rendered


def test_adapter_templates_do_not_contain_credentials():
    root = Path(__file__).resolve().parents[2]
    for path in (root / "distribution" / "adapters").glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "API_KEY=" not in text
        assert "token" not in text.lower()


def test_full_setup_dry_run_uses_planned_timeline_catalog_without_writes(
    tmp_path, monkeypatch
):
    root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text("model:\n  default: test-model\n", encoding="utf-8")
    original = config_path.read_bytes()
    monkeypatch.setattr(
        public_edition,
        "ensure_opencode",
        lambda **_kwargs: {
            "status": "planned",
            "argv": ["npm", "install", "-g", "opencode-ai"],
            "installed": False,
        },
    )

    result = public_edition.setup_public_edition(
        home=home,
        repo_root=root,
        live_health=False,
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["bootstrap"]["executor_mcp_catalog"] == [
        "hermes-timeline-code-map"
    ]
    assert config_path.read_bytes() == original
    assert not (home / "profiles").exists()


def test_full_setup_writes_registry_only_to_explicit_home(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    home = tmp_path / "target-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  default: test-model\n", encoding="utf-8"
    )
    wrong_home = tmp_path / "implicit-kanban-home"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(wrong_home))
    monkeypatch.setattr(
        public_edition,
        "ensure_opencode",
        lambda **_kwargs: {
            "status": "present",
            "path": "/usr/bin/true",
            "installed": False,
        },
    )

    result = public_edition.setup_public_edition(
        home=home,
        repo_root=root,
        install_opencode_binary=False,
        install_timeline=False,
        live_health=False,
    )

    assert result["opencode_adapter"]["executor_id"] == "executor_opencode_free"
    with public_edition.kb.connect_closing(home / "kanban.db") as conn:
        shell = public_edition.registry.resolve_shell(conn, "code")
        assert shell is not None
        bindings = public_edition.registry.list_bindings(conn, shell_id=shell.id)
        assert any(binding.executor_id == "executor_opencode_free" for binding in bindings)
    assert not (wrong_home / "kanban.db").exists()
