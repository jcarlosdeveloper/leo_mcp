"""Model registry loader.

Maps Brave Leo model keys (e.g. ``chat-claude-sonnet``) to human-readable
display names (e.g. ``claude sonnet``). The registry lives in ``models.toml``
(project root) so the model lineup can be edited without code changes.

This module was extracted from ``brave_leo_page.py`` where the loader
previously lived as ``_load_model_registry``. ``brave_leo_page.py`` keeps a
thin backward-compatible shim so existing imports and tests continue to work.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Path to the model registry (project root).
MODEL_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "models.toml"

# Built-in fallback used when models.toml is missing or unreadable.
_DEFAULT_MODELS: Dict[str, str] = {
    "chat-automatic": "automatic",
    "chat-claude-sonnet": "claude sonnet",
    "chat-claude-opus": "claude opus",
    "chat-claude-haiku": "claude haiku",
    "chat-qwen": "qwen",
}


def _read_toml(path: Path) -> Optional[object]:
    """Read a TOML file, returning None if tomllib/tomli is unavailable."""
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python < 3.11
        try:
            import tomli as tomllib
        except ImportError:
            tomllib = None
    return tomllib


def load_model_registry(
    path: Path = MODEL_REGISTRY_PATH,
    cache: Optional[Dict[str, str]] = None,
    force_reload: bool = False,
) -> Dict[str, str]:
    """Load model display names from a TOML registry file.

    Pure function: callers may pass an explicit ``path`` (for testing or
    runtime overrides) and a mutable ``cache`` dict to reuse across calls.
    Falls back to a minimal built-in map if the file is missing/unreadable.

    Args:
        path: Path to the registry TOML. Defaults to the project ``models.toml``.
        cache: Optional caller-owned dict used to memoize results.
        force_reload: If True, ignore ``cache`` and re-read the file.

    Returns:
        Mapping of ``model_key`` → ``display_name``.
    """
    if cache is not None and cache and not force_reload:
        return cache

    registry: Dict[str, str] = {}
    tomllib = _read_toml(path)

    if path.exists() and tomllib is not None:
        try:
            with open(path, "rb") as f:
                config = tomllib.load(f)
            for key, entry in config.get("models", {}).items():
                registry[key] = entry.get("name", key)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to load models.toml: {e}")

    if not registry:
        logger.warning("models.toml not found or empty — using built-in fallback")
        registry = dict(_DEFAULT_MODELS)

    if cache is not None:
        cache.clear()
        cache.update(registry)

    return registry