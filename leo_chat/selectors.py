"""Selector registry loader for Brave Leo automation.

Selectors live in ``selectors.toml`` (project root) so that Brave UI changes
never require code edits. This module loads that file once, validates its
schema version, and exposes the selectors as a plain dict plus typed
accessors.

Backward-compatible fallbacks (the selectors that were previously hardcoded in
``brave_leo_page.py``) are used when the TOML file is missing, unreadable, or
declares an unsupported schema version. This guarantees that the POM never
breaks on upgrade.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Path to the selector registry (project root).
SELECTORS_TOML_PATH = Path(__file__).resolve().parent.parent / "selectors.toml"

# Schema versions this loader understands.
_SUPPORTED_SCHEMA_VERSIONS = {1}

# Fallback selectors: the exact values that were previously hardcoded in
# BraveLeoPage. Kept here as the safety net and as a single, obvious place to
# inspect the defaults.
_DEFAULT_SELECTORS: Dict[str, str] = {
    "model_button": 'leo-button[data-testid="anchor-button"]',
    "model_item": "leo-menu-item[data-key='{model_key}']",
    "input_field": '[data-testid="leo-input"]',
    "send_button": '[data-testid="leo-submit-button"]',
    "show_all_models": 'leo-menu-item[data-testid="show-all-models-button"]',
    "response_selector": (
        '[data-testid="assistant-response"], .assistant-message, '
        '.leo-response, [class*="response"]'
    ),
    "conversation_iframe": '[data-testid="conversation-entries-iframe"]',
}

_cache: Optional[Dict[str, str]] = None


def _read_toml(path: Path) -> Optional[dict]:
    """Read and parse a TOML file.

    Uses the standard-library ``tomllib`` when available, falling back to the
    ``tomli`` backport on Python versions earlier than 3.11.

    Args:
        path: Path to the TOML file to read.

    Returns:
        The parsed TOML content as a dict, or ``None`` if no TOML parser is
        available or the file cannot be read or parsed.
    """
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python < 3.11
        try:
            import tomli as tomllib
        except ImportError:
            tomllib = None
    if tomllib is None:
        return None
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:  # noqa: BLE001 - any parse/IO error → fallback
        logger.warning(f"Failed to load selectors.toml: {e}")
        return None


def load_selectors(force_reload: bool = False) -> Dict[str, str]:
    """Load selectors from ``selectors.toml``, caching them after the first load.

    Falls back to ``_DEFAULT_SELECTORS`` when the file is missing, unreadable,
    or declares an unsupported schema version. This keeps the POM functional
    and backward-compatible with any prior hardcoded-selector behavior.

    Args:
        force_reload: If True, bypass the in-memory cache and re-read the file.

    Returns:
        Mapping of selector key to selector string.
    """
    global _cache

    if _cache is not None and not force_reload:
        return _cache

    selectors = dict(_DEFAULT_SELECTORS)

    if SELECTORS_TOML_PATH.exists():
        config = _read_toml(SELECTORS_TOML_PATH)
        if config is not None:
            schema_version = config.get("schema_version", 1)
            if schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
                logger.warning(
                    f"selectors.toml schema_version={schema_version} unsupported; "
                    f"using built-in defaults (supported: {sorted(_SUPPORTED_SCHEMA_VERSIONS)})"
                )
            else:
                entries = config.get("selectors", {})
                for key, value in entries.items():
                    if isinstance(value, str) and value.strip():
                        selectors[key] = value
                logger.debug(
                    f"Loaded {len(entries)} selectors from selectors.toml "
                    f"(schema v{schema_version})"
                )
    else:
        logger.debug("selectors.toml not found — using built-in defaults")

    _cache = selectors
    return selectors


def get_selector(key: str, force_reload: bool = False) -> str:
    """Return a single selector by key, loading the registry if needed.

    Args:
        key: The selector key to look up.
        force_reload: If True, bypass the in-memory cache and re-read the file.

    Returns:
        The selector string associated with ``key``.
    """
    return load_selectors(force_reload=force_reload)[key]


# Convenience accessors matching the historical attribute names on BraveLeoPage.
def model_button() -> str:
    """Return the selector for the model-picker button."""
    return get_selector("model_button")


def model_item() -> str:
    """Return the selector template for a model menu item."""
    return get_selector("model_item")


def input_field() -> str:
    """Return the selector for the Leo chat input field."""
    return get_selector("input_field")


def send_button() -> str:
    """Return the selector for the Leo submit (send) button."""
    return get_selector("send_button")


def show_all_models() -> str:
    """Return the selector for the "show all models" menu item."""
    return get_selector("show_all_models")


def response_selector() -> str:
    """Return the selector matching assistant response elements."""
    return get_selector("response_selector")


def conversation_iframe() -> str:
    """Return the selector for the conversation entries iframe."""
    return get_selector("conversation_iframe")