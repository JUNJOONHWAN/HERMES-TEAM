from hermes_cli import free_worker_router as router


def _profile(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    profile.joinpath("config.yaml").write_text("model: test\n", encoding="utf-8")
    return profile


def test_openrouter_worker_uses_live_ordered_fallbacks(tmp_path, monkeypatch):
    workspace = tmp_path / "work"
    workspace.mkdir()
    prompt = workspace / "prompt.md"
    prompt.write_text("do the task", encoding="utf-8")
    profile = _profile(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    candidates = ["strong:free", "backup:free", "new:free"]
    monkeypatch.setattr(
        router,
        "_openrouter_candidates",
        lambda **_kwargs: candidates,
    )
    captured = {}

    def fake_run_hermes(**kwargs):
        captured.update(kwargs)
        return "done"

    monkeypatch.setattr(router, "_run_hermes", fake_run_hermes)
    args = router.build_parser().parse_args(
        [
            "--provider", "openrouter",
            "--prompt-file", str(prompt),
            "--workspace", str(workspace),
            "--profile-home", str(profile),
        ]
    )

    assert router.run(args) == "done"
    assert captured["provider"] == "openrouter"
    assert captured["candidates"] == candidates
    assert captured["request_overrides"] == {
        "extra_body": {
            "models": candidates[1:],
            "provider": {
                "allow_fallbacks": True,
                "require_parameters": True,
            },
        }
    }


def test_opencode_worker_uses_live_catalog_for_hermes_tools(tmp_path, monkeypatch):
    workspace = tmp_path / "work"
    workspace.mkdir()
    prompt = workspace / "prompt.md"
    prompt.write_text("do the task", encoding="utf-8")
    profile = _profile(tmp_path)
    config = tmp_path / "opencode.json"
    config.write_text("{}\n", encoding="utf-8")
    state = tmp_path / "state.json"
    candidates = ["deepseek-v4-flash-free", "new-model-free"]
    monkeypatch.setattr(router, "_opencode_candidates", lambda _args: candidates)
    captured = {}
    monkeypatch.setattr(
        router,
        "_run_hermes",
        lambda **kwargs: captured.update(kwargs) or "done",
    )
    args = router.build_parser().parse_args(
        [
            "--provider", "opencode",
            "--prompt-file", str(prompt),
            "--workspace", str(workspace),
            "--profile-home", str(profile),
            "--opencode-config", str(config),
            "--state-file", str(state),
        ]
    )

    assert router.run(args) == "done"
    assert captured["provider"] == "opencode-zen"
    assert captured["candidates"] == candidates
    assert captured["request_overrides"] is None


def test_openrouter_health_requires_key_after_catalog_check(tmp_path, monkeypatch):
    workspace = tmp_path / "work"
    workspace.mkdir()
    profile = _profile(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        router,
        "_openrouter_candidates",
        lambda **_kwargs: ["strong:free"],
    )
    args = router.build_parser().parse_args(
        [
            "--provider", "openrouter",
            "--health-check",
            "--workspace", str(workspace),
            "--profile-home", str(profile),
        ]
    )

    try:
        router.run(args)
    except router.FreeWorkerRouterError as exc:
        assert "OPENROUTER_API_KEY" in str(exc)
    else:
        raise AssertionError("missing key must fail closed")
