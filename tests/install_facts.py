"""Facts about the published package, and how tests run its install commands against a locally built wheel."""

from __future__ import annotations

import tomllib
from pathlib import Path

from mcp import StdioServerParameters
from memory_keyring import server_env

from portward_free.paths import HOME_ENV

FREE = Path(__file__).parents[1]
_PROJECT = tomllib.loads((FREE / "pyproject.toml").read_text())["project"]
PACKAGE: str = _PROJECT["name"]
VERSION: str = _PROJECT["version"]
PINNED = f"uvx {PACKAGE}=={VERSION}"  # pinned: the Claude plugin directory refuses unpinned launchers


def from_wheel(command: list[str], wheel: Path) -> list[str]:
    """A documented `uvx portward-free==<version> ...` command, with the local wheel standing in for PyPI."""
    launcher, spec, *rest = command
    assert launcher == "uvx" and spec == f"{PACKAGE}=={VERSION}", command
    return ["uvx", "--isolated", "--from", str(wheel), PACKAGE, *rest]


def launch(command: list[str], wheel: Path, home: Path) -> StdioServerParameters:
    """Server parameters that start a documented command (from the local wheel) with `home` as its app-data folder
    and an in-memory keychain."""
    executable, *args = from_wheel(command, wheel)
    return StdioServerParameters(command=executable, args=args, env=server_env(**{HOME_ENV: str(home)}))
