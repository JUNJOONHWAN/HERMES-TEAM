from hermes_cli.openrouter_free_router import (
    OPENROUTER_FREE_MODEL_PRIORITY,
    is_eligible_openrouter_free_model,
    rank_openrouter_free_models,
)


def _row(model_id, *, prompt="0", completion="0", tools=True, text=True, context=100_000):
    parameters = ["tools", "tool_choice"] if tools else ["temperature"]
    return {
        "id": model_id,
        "pricing": {"prompt": prompt, "completion": completion},
        "supported_parameters": parameters,
        "architecture": {
            "input_modalities": ["text"],
            "output_modalities": ["text"] if text else ["audio"],
        },
        "context_length": context,
    }


def test_openrouter_free_router_is_strict_zero_price_text_and_tools():
    primary = _row(OPENROUTER_FREE_MODEL_PRIORITY[0])
    assert is_eligible_openrouter_free_model(primary) is True
    assert is_eligible_openrouter_free_model(_row("vendor/paid", prompt="0.1")) is False
    assert is_eligible_openrouter_free_model(_row("vendor/free:free", tools=False)) is False
    assert is_eligible_openrouter_free_model(_row("vendor/audio:free", text=False)) is False
    assert is_eligible_openrouter_free_model(_row("vendor/tiny:free", context=8192)) is False
    assert is_eligible_openrouter_free_model(_row("openrouter/free")) is False


def test_openrouter_free_router_keeps_proven_order_and_discovers_new_models():
    primary = OPENROUTER_FREE_MODEL_PRIORITY[0]
    secondary = OPENROUTER_FREE_MODEL_PRIORITY[2]
    rows = [
        _row("vendor/new-strong-model:free"),
        _row(secondary),
        _row(primary),
        _row("vendor/removed-paid:free", completion="0.2"),
    ]
    assert rank_openrouter_free_models(rows) == [
        primary,
        secondary,
        "vendor/new-strong-model:free",
    ]
