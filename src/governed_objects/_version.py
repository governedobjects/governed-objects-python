# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Single version source: ``pyproject.toml`` (``[project].version``).

An installed distribution reports it through package metadata. When the package is
imported from a source checkout (for example ``PYTHONPATH=src``), the version is read
from the adjacent ``pyproject.toml`` instead. Nothing else defines a version.
"""
from __future__ import annotations

from importlib import metadata
from pathlib import Path

DISTRIBUTION_NAME = "governed-objects"


def _from_source_checkout() -> str | None:
    import tomllib

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
    except (OSError, ValueError):
        return None
    if project.get("name") != DISTRIBUTION_NAME:
        return None
    version = project.get("version")
    return version if isinstance(version, str) else None


def _resolve() -> str:
    try:
        return metadata.version(DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError:
        return _from_source_checkout() or "unknown"


__version__ = _resolve()
