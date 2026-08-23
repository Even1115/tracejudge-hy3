"""Locate project data in both source checkouts and built wheels."""

from __future__ import annotations

from pathlib import Path

from tracejudge_hy3.exceptions import ConfigurationError


def data_path(*parts: str) -> Path:
    """Return an existing path below the project's bundled ``data`` directory."""

    module_path = Path(__file__).resolve()
    candidates = [module_path.parent / "data" / Path(*parts)]
    candidates.extend(parent / "data" / Path(*parts) for parent in module_path.parents)

    for candidate in candidates:
        if candidate.exists():
            return candidate

    relative = Path("data", *parts)
    raise ConfigurationError(
        f"could not locate bundled data path {relative}; checked source and installed layouts"
    )
