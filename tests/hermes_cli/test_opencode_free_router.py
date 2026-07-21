from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from datetime import datetime, timedelta, timezone

from hermes_cli import opencode_free_router as router


def _completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def _args(tmp_path, *, health_check=False, prompt_file=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config = tmp_path / "opencode.json"
    config.write_text("{}\n", encoding="utf-8")
    return Namespace(
        health_check=health_check,
        prompt_file=str(prompt_file) if prompt_file else None,
        workspace=str(workspace),
        config=str(config),
        state_file=str(tmp_path / "router-state.json"),
        opencode="opencode",
        agent="remote-vibe-build",
        catalog_timeout_seconds=5,
        probe_timeout_seconds=5,
        task_timeout_seconds=5,
        health_ttl_seconds=900,
        cooldown_seconds=900,
    )


def test_rank_free_models_is_provider_locked_and_strongest_first():
    ranked = router.rank_free_models(
        [
            "openrouter/something-free",
            "opencode/north-mini-code-free",
            "opencode/paid-pro",
            "opencode/future-free",
            "opencode/big-pickle",
            "opencode/deepseek-v4-flash-free",
        ]
    )

    assert ranked[:3] == [
        "opencode/deepseek-v4-flash-free",
        "opencode/big-pickle",
        "opencode/north-mini-code-free",
    ]
    assert ranked[-1] == "opencode/future-free"
    assert all(router.is_declared_free_model(model) for model in ranked)
    assert "openrouter/something-free" not in ranked
    assert "opencode/paid-pro" not in ranked


def test_qualified_new_free_model_becomes_top_fallback_without_replacing_primary():
    profiles = {
        "opencode/future-free": {
            "status": "qualified_top_fallback",
            "calibration_version": router.CALIBRATION_VERSION,
            "score": router.CALIBRATION_MAX_SCORE,
            "format_exact": True,
            "latency_ms": 1200,
        }
    }

    ranked = router.rank_free_models(
        [
            "opencode/big-pickle",
            "opencode/future-free",
            "opencode/deepseek-v4-flash-free",
            "openrouter/not-allowed-free",
        ],
        profiles,
    )

    assert ranked == [
        "opencode/deepseek-v4-flash-free",
        "opencode/future-free",
        "opencode/big-pickle",
    ]


def test_verbose_zero_cost_model_without_free_suffix_is_admitted_and_paid_rejected():
    output = """opencode/next-strong
{
  "providerID": "opencode",
  "status": "active",
  "cost": {"input": 0, "output": 0, "cache": {"read": 0, "write": 0}}
}
opencode/paid-pro
{
  "providerID": "opencode",
  "status": "active",
  "cost": {"input": 1, "output": 2}
}
"""

    metadata = router._parse_verbose_model_metadata(output)
    verified = {
        model
        for model, item in metadata.items()
        if router._metadata_declares_zero_cost_free(item)
    }
    ranked = router.rank_free_models(
        metadata,
        verified_free_models=verified,
    )

    assert ranked == ["opencode/next-strong"]
    assert "opencode/paid-pro" not in ranked


def test_health_calibrates_new_free_model_once_and_persists_effective_priority(
    tmp_path, monkeypatch
):
    args = _args(tmp_path, health_check=True)
    calibration_models = []

    def fake_run(argv, **_kwargs):
        if argv[1:3] == ["mcp", "list"]:
            return _completed(argv, stdout="hermes-timeline-code-map connected\n")
        if argv[1:3] == ["models", "opencode"]:
            return _completed(
                argv,
                stdout=(
                    "opencode/deepseek-v4-flash-free\n"
                    "opencode/big-pickle\n"
                    "opencode/future-free\n"
                    "opencode/paid-pro\n"
                ),
            )
        model = argv[argv.index("--model") + 1]
        if argv[-1] == router.CALIBRATION_PROMPT:
            calibration_models.append(model)
            return _completed(
                argv,
                stdout=(
                    "HERMES_CALIBRATION_V1|logic=A|math=24|"
                    "code=10|token=BENCHMARK"
                ),
            )
        return _completed(argv, stdout="READY\n")

    monkeypatch.setattr(router, "_run_command", fake_run)

    first_output = router.health_check(args)
    second_output = router.health_check(args)

    assert calibration_models == ["opencode/future-free"]
    assert "opencode/future-free:qualified_top_fallback" in first_output
    assert "OPENCODE_FREE_CALIBRATED=" not in second_output
    state = json.loads((tmp_path / "router-state.json").read_text(encoding="utf-8"))
    assert state["effective_priority"] == [
        "opencode/deepseek-v4-flash-free",
        "opencode/future-free",
        "opencode/big-pickle",
    ]
    assert state["dynamic_model_profiles"]["opencode/future-free"]["status"] == (
        "qualified_top_fallback"
    )
    assert "opencode/paid-pro" not in state["catalog_available"]


def test_failed_calibration_keeps_new_free_model_available_at_tail(
    tmp_path, monkeypatch
):
    args = _args(tmp_path, health_check=True)

    def fake_run(argv, **_kwargs):
        if argv[1:3] == ["mcp", "list"]:
            return _completed(argv, stdout="hermes-timeline-code-map connected\n")
        if argv[1:3] == ["models", "opencode"]:
            return _completed(
                argv,
                stdout=(
                    "opencode/deepseek-v4-flash-free\n"
                    "opencode/big-pickle\n"
                    "opencode/future-free\n"
                ),
            )
        if argv[-1] == router.CALIBRATION_PROMPT:
            return _completed(argv, returncode=1, stderr="temporary calibration error")
        return _completed(argv, stdout="READY\n")

    monkeypatch.setattr(router, "_run_command", fake_run)

    router.health_check(args)

    state = json.loads((tmp_path / "router-state.json").read_text(encoding="utf-8"))
    assert state["effective_priority"] == [
        "opencode/deepseek-v4-flash-free",
        "opencode/big-pickle",
        "opencode/future-free",
    ]
    profile = state["dynamic_model_profiles"]["opencode/future-free"]
    assert profile["status"] == "calibration_failed"
    assert profile["next_calibration_after_utc"]


def test_health_check_falls_back_between_live_free_models(tmp_path, monkeypatch):
    args = _args(tmp_path, health_check=True)
    attempted_models = []

    def fake_run(argv, **_kwargs):
        if argv[1:3] == ["mcp", "list"]:
            return _completed(argv, stdout="hermes-timeline-code-map connected\nexample-market-data connected\n")
        if argv[1:3] == ["models", "opencode"]:
            return _completed(
                argv,
                stdout=(
                    "opencode/deepseek-v4-flash-free\n"
                    "opencode/big-pickle\n"
                    "opencode/paid-pro\n"
                ),
            )
        assert argv[1] == "run"
        model = argv[argv.index("--model") + 1]
        attempted_models.append(model)
        if model == "opencode/deepseek-v4-flash-free":
            return _completed(argv, returncode=1, stderr="temporary provider error")
        return _completed(argv, stdout="READY\n")

    monkeypatch.setattr(router, "_run_command", fake_run)

    output = router.health_check(args)

    assert attempted_models == [
        "opencode/deepseek-v4-flash-free",
        "opencode/big-pickle",
    ]
    assert "OPENCODE_FREE_MODEL_SELECTED=opencode/big-pickle" in output
    state = json.loads((tmp_path / "router-state.json").read_text(encoding="utf-8"))
    assert state["selected_model"] == "opencode/big-pickle"
    assert state["model_failures"]["opencode/deepseek-v4-flash-free"]
    assert "opencode/paid-pro" not in state["catalog"]


def test_real_task_runs_once_and_does_not_round_robin_prompt(tmp_path, monkeypatch):
    args = _args(tmp_path)
    prompt = tmp_path / "workspace" / "assignment.md"
    prompt.write_text("Make one bounded change.", encoding="utf-8")
    args.prompt_file = str(prompt)
    now = datetime.now(timezone.utc)
    (tmp_path / "router-state.json").write_text(
        json.dumps(
            {
                "schema": router.STATE_SCHEMA,
                "selected_model": "opencode/deepseek-v4-flash-free",
                "healthy_until_utc": (now + timedelta(minutes=10)).isoformat(),
                "model_failures": {},
            }
        ),
        encoding="utf-8",
    )
    task_prompts = []

    def fake_run(argv, **_kwargs):
        if argv[1:3] == ["models", "opencode"]:
            return _completed(
                argv,
                stdout=(
                    "opencode/deepseek-v4-flash-free\n"
                    "opencode/big-pickle\n"
                ),
            )
        assert argv[1] == "run"
        task_prompts.append(argv[-1])
        return _completed(argv, stdout="bounded result")

    monkeypatch.setattr(router, "_run_command", fake_run)

    assert router.run_task(args) == "bounded result"
    assert task_prompts == ["Make one bounded change."]
    state = json.loads((tmp_path / "router-state.json").read_text(encoding="utf-8"))
    assert state["last_task_success"]["prompt_replayed"] is False


def test_all_failed_health_preflights_persist_cooldown_state(tmp_path, monkeypatch):
    args = _args(tmp_path, health_check=True)

    def fake_run(argv, **_kwargs):
        if argv[1:3] == ["mcp", "list"]:
            return _completed(argv, stdout="hermes-timeline-code-map connected\n")
        if argv[1:3] == ["models", "opencode"]:
            return _completed(argv, stdout="opencode/deepseek-v4-flash-free\n")
        return _completed(argv, returncode=1, stderr="provider unavailable")

    monkeypatch.setattr(router, "_run_command", fake_run)

    try:
        router.health_check(args)
    except router.OpenCodeFreeRouterError as exc:
        assert "all catalog-confirmed free" in str(exc)
    else:
        raise AssertionError("failed health check unexpectedly succeeded")

    state = json.loads((tmp_path / "router-state.json").read_text(encoding="utf-8"))
    assert state["last_health_error"]
    assert state["model_failures"]["opencode/deepseek-v4-flash-free"][
        "cooldown_until_utc"
    ]


def test_failed_task_is_cooled_down_without_replaying_on_fallback(
    tmp_path, monkeypatch
):
    args = _args(tmp_path)
    prompt = tmp_path / "workspace" / "assignment.md"
    prompt.write_text("Potentially side-effecting task.", encoding="utf-8")
    args.prompt_file = str(prompt)
    task_models = []

    def fake_run(argv, **_kwargs):
        if argv[1:3] == ["models", "opencode"]:
            return _completed(
                argv,
                stdout=(
                    "opencode/deepseek-v4-flash-free\n"
                    "opencode/big-pickle\n"
                ),
            )
        model = argv[argv.index("--model") + 1]
        if argv[-1] == "Reply exactly READY. Do not call tools.":
            return _completed(argv, stdout="READY")
        task_models.append(model)
        return _completed(argv, returncode=1, stderr="provider failed mid-run")

    monkeypatch.setattr(router, "_run_command", fake_run)

    try:
        router.run_task(args)
    except router.OpenCodeFreeRouterError as exc:
        assert "prompt was not replayed" in str(exc)
    else:
        raise AssertionError("failed task unexpectedly succeeded")

    assert task_models == ["opencode/deepseek-v4-flash-free"]
    state = json.loads((tmp_path / "router-state.json").read_text(encoding="utf-8"))
    failure = state["model_failures"]["opencode/deepseek-v4-flash-free"]
    assert failure["cooldown_until_utc"]
    assert state["last_task_failure"]["prompt_replayed"] is False
