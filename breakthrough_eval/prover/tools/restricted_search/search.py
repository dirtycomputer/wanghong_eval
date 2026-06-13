"""One configured restricted search entry point."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .providers import PROVIDERS, RestrictedSearchError

RESTRICTED_SEARCH_HOST = "restricted-search-mcp.internal"
DEFAULT_CONFIG = Path(__file__).with_name("config.yaml")


def restricted_search(
    query: str,
    cutoff: date,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    if not query.strip():
        raise RestrictedSearchError("query is required")
    config = _load_config(config_path)
    enabled = config["enabled_providers"]
    provider_configs = config["providers"]

    results = []
    for name in enabled:
        if name not in PROVIDERS:
            raise RestrictedSearchError(f"unknown provider: {name}")
        if name not in provider_configs:
            raise RestrictedSearchError(f"missing config for provider: {name}")
        results.extend(PROVIDERS[name](query, cutoff, provider_configs[name]))

    results.sort(key=lambda item: item["published_date"], reverse=True)
    return {"results": results}


def _load_config(path: str | Path | None) -> dict[str, Any]:
    config_path = Path(path or os.environ.get("RESTRICTED_SEARCH_CONFIG") or DEFAULT_CONFIG)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RestrictedSearchError(f"config must be a mapping: {config_path}")
    if not isinstance(data.get("enabled_providers"), list):
        raise RestrictedSearchError("config.enabled_providers must be a list")
    if not isinstance(data.get("providers"), dict):
        raise RestrictedSearchError("config.providers must be a mapping")
    return data
