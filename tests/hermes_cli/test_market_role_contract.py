from hermes_cli import kanban_db as kb
from hermes_cli import supervisor_bootstrap as bootstrap
from hermes_cli import supervisor_registry as registry


ROLE_SPECS = bootstrap.ROLE_SPECS


def test_market_role_contract_uses_shared_source_status_taxonomy():
    instructions = ROLE_SPECS["market"]["instructions"]

    for status in (
        "CONFIRMED",
        "PARTIAL_LIMIT",
        "NOT_DUE",
        "EOD_ONLY",
        "ESTIMATE_ONLY",
        "UNVERIFIED_CONTRACT",
        "UNVERIFIED_UNIT",
        "NOT_APPLICABLE",
        "INTENTIONAL_NOT_USED",
        "PAUSED",
        "RECOVERING",
        "FAILED",
    ):
        assert status in instructions

    assert "blank same-day stock investor fields are EOD_ONLY" in instructions
    assert "999/S001 market flow is CONFIRMED" in instructions
    assert "such as S201 is UNVERIFIED_CONTRACT" in instructions
    assert "J/K and J/Q program endpoints may be CONFIRMED independently" in instructions
    assert "report every source with the shared lifecycle taxonomy" in instructions


def test_market_role_contract_does_not_count_normal_lifecycle_states_as_failures():
    instructions = ROLE_SPECS["market"]["instructions"]

    assert (
        "NOT_DUE, EOD_ONLY, PAUSED, NOT_APPLICABLE, or INTENTIONAL_NOT_USED as "
        "failures or warnings"
    ) in instructions


def test_all_role_shells_are_adapter_independent_contracts():
    assert ROLE_SPECS
    for shell_key, spec in ROLE_SPECS.items():
        assert shell_key
        assert str(spec["instructions"]).strip()
        assert set(spec["required"]).issubset(set(spec["allowed"]))


def test_operator_added_adapter_binding_rebinds_to_active_role_shell(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    registry.ensure_schema(conn)
    first = registry.register_shell_version(
        conn,
        shell_key="market",
        name="Market",
        contract={
            "allowed_adapters": ["hermes_profile", "command"],
            "instructions": "market contract v1",
        },
        required_capabilities=("kanban",),
        allowed_capabilities=("kanban", "terminal"),
    )
    executor = registry.upsert_executor(
        conn,
        executor_id="executor_future_adapter",
        name="future adapter",
        adapter_type="command",
        launch_config={
            "argv": ["future-agent", "{prompt_file}"],
            "capability_enforcement": "env",
        },
        capabilities=("kanban", "terminal"),
        capacity=1,
        heartbeat_required=False,
    )
    binding = registry.upsert_binding(
        conn,
        shell_id=first.id,
        executor_id=executor.id,
        priority=17,
        weight=0.75,
        capability_cap=("kanban",),
        constraints={"auto_spawn": True},
        responsibility="candidate",
        assignment_note="operator-owned",
        assigned_by="operator",
        binding_id="binding_market_future_adapter",
    )
    registry.set_binding_enabled(conn, binding.id, False)
    second = registry.ensure_shell_version(
        conn,
        shell_key="market",
        name="Market",
        contract={
            "allowed_adapters": ["hermes_profile", "command"],
            "instructions": "market contract v2",
        },
        required_capabilities=("kanban",),
        allowed_capabilities=("kanban", "terminal"),
    )

    rebound = bootstrap._rebind_existing_bindings_to_active_shells(
        conn, {"market": second}
    )

    assert rebound == [binding.id]
    current = registry.get_binding(conn, binding.id)
    assert current is not None
    assert current.shell_id == second.id
    assert current.executor_id == executor.id
    assert current.priority == 17
    assert current.weight == 0.75
    assert current.capability_cap == ["kanban"]
    assert current.assignment_note == "operator-owned"
    assert current.assigned_by == "operator"
    assert current.enabled is False
