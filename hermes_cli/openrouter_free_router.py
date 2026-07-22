"""Strict free-only model ranking for the OpenRouter controller adapter.

OpenRouter's ``openrouter/free`` router is intentionally random after feature
filtering.  Hermes instead sends an ordered ``models`` list so the strongest
known free model is tried first and OpenRouter can move to the next compatible
free model inside the same request.  The live catalog remains authoritative:
removed or no-longer-free models are dropped and newly discovered, tool-capable
free models are appended for health-gated use.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


OPENROUTER_FREE_MODEL_PRIORITY = (
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "poolside/laguna-s-2.1:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "poolside/laguna-m.1:free",
    "cohere/north-mini-code:free",
    "google/gemma-4-26b-a4b-it:free",
    "openai/gpt-oss-20b:free",
    "poolside/laguna-xs-2.1:free",
)
MIN_CONTEXT_LENGTH = 32_768


def _zero_price(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        return Decimal(str(value)) == 0
    except (InvalidOperation, ValueError):
        return False


def is_eligible_openrouter_free_model(row: Any) -> bool:
    """Return whether a catalog row is a zero-cost text model with tool use."""
    if not isinstance(row, dict):
        return False
    model_id = str(row.get("id") or "").strip()
    if not model_id.endswith(":free") or model_id == "openrouter/free":
        return False
    pricing = row.get("pricing")
    if not isinstance(pricing, dict):
        return False
    if not _zero_price(pricing.get("prompt")):
        return False
    if not _zero_price(pricing.get("completion")):
        return False
    supported = {
        str(value or "").strip()
        for value in (row.get("supported_parameters") or [])
        if str(value or "").strip()
    }
    if not {"tools", "tool_choice"}.issubset(supported):
        return False
    architecture = row.get("architecture")
    if not isinstance(architecture, dict):
        return False
    outputs = {
        str(value or "").strip().lower()
        for value in (architecture.get("output_modalities") or [])
        if str(value or "").strip()
    }
    if "text" not in outputs:
        return False
    try:
        context_length = int(row.get("context_length") or 0)
    except (TypeError, ValueError):
        return False
    return context_length >= MIN_CONTEXT_LENGTH


def rank_openrouter_free_models(rows: Iterable[Any]) -> list[str]:
    """Rank the current strict-free catalog with a stable proven-first anchor.

    New eligible models are incorporated automatically at the tail.  They do
    not displace the proven primary from catalog metadata alone; a later
    versioned priority update can promote them after real repair/tool-use
    certification.
    """
    eligible = {
        str(row.get("id") or "").strip()
        for row in rows
        if is_eligible_openrouter_free_model(row)
    }
    known = [
        model_id
        for model_id in OPENROUTER_FREE_MODEL_PRIORITY
        if model_id in eligible
    ]
    known_set = set(known)
    discovered = sorted(eligible - known_set)
    return known + discovered
