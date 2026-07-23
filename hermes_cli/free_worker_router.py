"""Dynamic strict-free model router for Hermes worker adapters.

Unlike the controller-only adapters, this command runs a normal Hermes
one-shot worker inside a dedicated profile.  The Role Shell therefore remains
authoritative and the selected free model receives exactly the profile's
Hermes tools.  Catalog discovery happens before each task so removed models
drop out and newly eligible models enter the fallback order automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.request import Request, urlopen

from hermes_cli import opencode_free_router as opencode_router
from hermes_cli.openrouter_free_router import rank_openrouter_free_models


OPENROUTER_CATALOG_URL = "https://openrouter.ai/api/v1/models"


class FreeWorkerRouterError(RuntimeError):
    """Raised when no strict-free worker route can run safely."""


@contextmanager
def _working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _openrouter_candidates(*, catalog_url: str, timeout: float) -> list[str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "HermesAgent/free-worker-router",
    }
    secret = str(os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    request = Request(catalog_url, headers=headers)
    try:
        with urlopen(request, timeout=max(0.2, timeout)) as response:
            payload = response.read(2_000_000)
    except Exception as exc:
        raise FreeWorkerRouterError(f"OpenRouter catalog failed: {exc}") from exc
    try:
        parsed = json.loads(payload.decode("utf-8")) if payload else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FreeWorkerRouterError("OpenRouter catalog returned invalid JSON") from exc
    rows = parsed.get("data") if isinstance(parsed, dict) else None
    candidates = rank_openrouter_free_models(rows if isinstance(rows, list) else [])
    if not candidates:
        raise FreeWorkerRouterError(
            "OpenRouter catalog contains no zero-price tool-capable model"
        )
    return candidates


def _opencode_candidates(args: argparse.Namespace) -> list[str]:
    config_path = Path(args.opencode_config).expanduser().resolve()
    if not config_path.is_file():
        raise FreeWorkerRouterError(
            f"OpenCode worker config is unavailable: {config_path}"
        )
    env = opencode_router._command_env(config_path)
    available, _output, metadata = opencode_router._discover_catalog(
        opencode=args.opencode,
        env=env,
        workspace=Path(args.workspace).expanduser().resolve(),
        timeout=args.catalog_timeout_seconds,
        refresh=bool(args.health_check),
    )
    ranked = opencode_router.rank_free_models(
        available,
        verified_free_models=available,
    )
    if not ranked:
        raise FreeWorkerRouterError(
            "OpenCode catalog contains no verified free model"
        )
    state_path = Path(args.state_file).expanduser().resolve()
    state = opencode_router._load_state(state_path)
    state["catalog_metadata"] = opencode_router._catalog_metadata_summary(
        metadata,
        available=available,
    )
    state["effective_priority"] = ranked
    opencode_router._save_state(state_path, state)
    return [model.removeprefix("opencode/") for model in ranked]


def _run_hermes(
    *,
    prompt: str,
    workspace: Path,
    profile_home: Path,
    provider: str,
    candidates: list[str],
    request_overrides: Optional[dict[str, Any]] = None,
) -> str:
    if not candidates:
        raise FreeWorkerRouterError("free worker candidate list is empty")
    os.environ["HERMES_HOME"] = str(profile_home)
    if provider == "opencode-zen":
        # The official free Zen models are anonymous.  The transport removes
        # this SDK placeholder Authorization header only for those models.
        os.environ.setdefault("OPENCODE_ZEN_API_KEY", "no-key-required")
    from hermes_cli.oneshot import _run_agent

    fallback_model = [
        {"provider": provider, "model": model} for model in candidates[1:]
    ]
    with _working_directory(workspace):
        response, result = _run_agent(
            prompt,
            model=candidates[0],
            provider=provider,
            request_overrides=request_overrides,
            fallback_model=fallback_model,
        )
    if result.get("failed") or result.get("partial") or not response.strip():
        raise FreeWorkerRouterError(
            str(result.get("error") or "free Hermes worker produced no complete result")
        )
    return response.strip()


def run(args: argparse.Namespace) -> str:
    workspace = Path(args.workspace).expanduser().resolve()
    profile_home = Path(args.profile_home).expanduser().resolve()
    if not workspace.is_dir() or not profile_home.joinpath("config.yaml").is_file():
        raise FreeWorkerRouterError("worker workspace or profile is unavailable")
    if args.provider == "openrouter":
        candidates = _openrouter_candidates(
            catalog_url=args.catalog_url,
            timeout=args.catalog_timeout_seconds,
        )
        if not str(os.environ.get("OPENROUTER_API_KEY") or "").strip():
            raise FreeWorkerRouterError("OPENROUTER_API_KEY is required")
        request_overrides = {
            "extra_body": {
                "models": candidates[1:],
                "provider": {
                    "allow_fallbacks": True,
                    "require_parameters": True,
                },
            }
        }
        provider = "openrouter"
    else:
        candidates = _opencode_candidates(args)
        request_overrides = None
        provider = "opencode-zen"
    if args.health_check:
        return f"FREE_WORKER_PROVIDER={args.provider}\nFREE_WORKER_PRIORITY={','.join(candidates)}\nREADY"
    prompt_path = Path(args.prompt_file).expanduser().resolve()
    if not prompt_path.is_file():
        raise FreeWorkerRouterError(f"prompt file is unavailable: {prompt_path}")
    return _run_hermes(
        prompt=prompt_path.read_text(encoding="utf-8"),
        workspace=workspace,
        profile_home=profile_home,
        provider=provider,
        candidates=candidates,
        request_overrides=request_overrides,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("opencode", "openrouter"), required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--health-check", action="store_true")
    mode.add_argument("--prompt-file")
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--profile-home", required=True)
    parser.add_argument("--catalog-url", default=OPENROUTER_CATALOG_URL)
    parser.add_argument("--catalog-timeout-seconds", type=float, default=30)
    parser.add_argument("--opencode-config", default="")
    parser.add_argument("--state-file", default="")
    parser.add_argument("--opencode", default=shutil.which("opencode") or "opencode")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        output = run(args)
    except Exception as exc:
        print(f"Free worker router failed: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
