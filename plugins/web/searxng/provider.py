"""SearXNG search — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Same JSON
API call (``/search?format=json``), same result normalization. The legacy
in-tree module ``tools.web_providers.searxng`` was removed in the same
commit that moved this code under ``plugins/``; this file is now the
canonical implementation.

Search-only — SearXNG aggregates results from upstream engines but does not
fetch/extract arbitrary URLs. ``supports_extract()`` returns False.

Config keys this provider responds to::

    web:
      search_backend: "searxng"     # explicit per-capability
      backend: "searxng"            # shared fallback

Env var::

    SEARXNG_URL=http://localhost:8080
    SEARXNG_ENGINES=bing,yahoo,github
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from typing import Any, Dict

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


def _searxng_url() -> str:
    """Return SEARXNG_URL from Hermes config-aware env, falling back to process env."""
    try:
        from hermes_cli.config import get_env_value

        val = get_env_value("SEARXNG_URL")
    except Exception:
        val = None
    if val is None:
        val = os.getenv("SEARXNG_URL", "")
    return (val or "").strip()


def _searxng_engines() -> str:
    """Return optional comma-separated SearXNG engine list."""
    try:
        from hermes_cli.config import get_env_value

        val = get_env_value("SEARXNG_ENGINES")
    except Exception:
        val = None
    if val is None:
        val = os.getenv("SEARXNG_ENGINES", "")
    return (val or "").strip()


def _engine_names(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class SearXNGWebSearchProvider(WebSearchProvider):
    """Search via a user-hosted SearXNG instance."""

    @property
    def name(self) -> str:
        return "searxng"

    @property
    def display_name(self) -> str:
        return "SearXNG"

    def is_available(self) -> bool:
        """Return True when ``SEARXNG_URL`` is set."""
        return bool(_searxng_url())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a search against the configured SearXNG instance."""
        import httpx

        base_url = _searxng_url().rstrip("/")
        if not base_url:
            return {"success": False, "error": "SEARXNG_URL is not set"}

        params: Dict[str, Any] = {
            "q": query,
            "format": "json",
            "pageno": 1,
        }
        engines = _searxng_engines()
        if engines:
            params["engines"] = engines

        try:
            resp = httpx.get(
                f"{base_url}/search",
                params=params,
                timeout=15,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("SearXNG HTTP error: %s", exc)
            return {
                "success": False,
                "error": f"SearXNG returned HTTP {exc.response.status_code}",
            }
        except httpx.RequestError as exc:
            logger.warning("SearXNG request error: %s", exc)
            return {
                "success": False,
                "error": f"Could not reach SearXNG at {base_url}: {exc}",
            }

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("SearXNG response parse error: %s", exc)
            return {
                "success": False,
                "error": "Could not parse SearXNG response as JSON",
            }

        raw_results = data.get("results", [])
        engine_order = _engine_names(engines)

        def _score(item: Dict[str, Any]) -> float:
            try:
                return float(item.get("score", 0) or 0)
            except (TypeError, ValueError):
                return 0.0

        if engine_order:
            grouped_results: dict[str, list[Dict[str, Any]]] = defaultdict(list)
            unknown_results = []
            for result in raw_results:
                result_engines = result.get("engines", [])
                engine = (
                    str(result_engines[0])
                    if isinstance(result_engines, list) and result_engines
                    else str(result.get("engine", ""))
                )
                if engine:
                    grouped_results[engine].append(result)
                else:
                    unknown_results.append(result)

            for bucket in grouped_results.values():
                bucket.sort(key=_score, reverse=True)
            unknown_results.sort(key=_score, reverse=True)

            ordered_results = []
            grouped_keys = engine_order + [
                key for key in grouped_results if key not in set(engine_order)
            ]
            while len(ordered_results) < len(raw_results):
                added = False
                for engine in grouped_keys:
                    bucket = grouped_results.get(engine, [])
                    if bucket:
                        ordered_results.append(bucket.pop(0))
                        added = True
                if not added:
                    break
            ordered_results.extend(unknown_results)
        else:
            ordered_results = sorted(raw_results, key=_score, reverse=True)

        deduped_results = []
        seen_urls = set()
        for result in ordered_results:
            url = str(result.get("url", ""))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            deduped_results.append(result)
            if len(deduped_results) >= limit:
                break

        web_results = [
            {
                "title": str(r.get("title", "")),
                "url": str(r.get("url", "")),
                "description": str(r.get("content", "")),
                "source_engines": r.get("engines", []),
                "engine": ", ".join(r.get("engines", []))
                if isinstance(r.get("engines"), list)
                else str(r.get("engine", "")),
                "position": i + 1,
            }
            for i, r in enumerate(deduped_results)
        ]

        logger.info(
            "SearXNG search '%s': %d results (from %d raw, limit %d)",
            query,
            len(web_results),
            len(raw_results),
            limit,
        )

        return {"success": True, "data": {"web": web_results}}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "SearXNG",
            "badge": "free · self-hosted",
            "tag": "Free, privacy-respecting metasearch. Point SEARXNG_URL at your instance.",
            "env_vars": [
                {
                    "key": "SEARXNG_URL",
                    "prompt": "SearXNG instance URL (e.g. http://localhost:8080)",
                    "url": "https://searx.space/",
                },
                {
                    "key": "SEARXNG_ENGINES",
                    "prompt": "Optional comma-separated SearXNG engines (e.g. bing,github)",
                    "url": "https://docs.searxng.org/user/configured_engines.html",
                },
            ],
        }
