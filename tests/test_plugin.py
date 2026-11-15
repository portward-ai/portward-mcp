"""The Claude Code / Cowork plugin: it validates, installs from our marketplace, and its server starts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from install_facts import FREE, PACKAGE, VERSION
from mcp import Client, StdioServerParameters
from memory_keyring import server_env

from portward_free.paths import HOME_ENV

PLUGIN = FREE / "plugin"
# The directory accepts a uvx launcher only when it names code it can read: a wheel the plugin ships. A PyPI version
# alone is refused, because a release's files can be added to. The wheel is the one published to PyPI for VERSION.
WHEEL = f"vendor/{PACKAGE.replace('-', '_')}-{VERSION}-py3-none-any.whl"
LAUNCH = ["uvx", "--from", "${CLAUDE_PLUGIN_ROOT}/" + WHEEL, PACKAGE]
needs_claude = pytest.mark.skipif(shutil.which("claude") is None, reason="needs the claude CLI")


def claude(config_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """The claude CLI with a throwaway config folder, so tests never touch the user's own setup."""
    return subprocess.run(["claude", *args], env={**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)},
                          capture_output=True, text=True, timeout=120)


def server_command(plugin_root: Path) -> list[str]:
    server: dict[str, Any] = json.loads((plugin_root / ".mcp.json").read_text())["mcpServers"]["portward"]
    return [server["command"], *server["args"]]


def test_the_plugin_and_marketplace_name_the_package_version() -> None:
    assert json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"] == VERSION
    marketplace = json.loads((FREE / ".claude-plugin" / "marketplace.json").read_text())
    assert [(p["name"], p["version"]) for p in marketplace["plugins"]] == [("portward", VERSION)]


def test_the_plugin_starts_the_server_from_the_wheel_it_ships_and_no_shell() -> None:
    assert server_command(PLUGIN) == LAUNCH
    assert [p.relative_to(PLUGIN).as_posix() for p in (PLUGIN / "vendor").iterdir()] == [WHEEL]


@needs_claude
@pytest.mark.parametrize("path", [PLUGIN, FREE], ids=["plugin", "marketplace"])
def test_claude_validates_the_plugin_and_the_marketplace_strictly(path: Path, tmp_path: Path) -> None:
    done = claude(tmp_path, "plugin", "validate", "--strict", str(path))
    assert done.returncode == 0, done.stdout + done.stderr


@needs_claude
def test_the_plugin_passes_the_directory_checks_claude_can_run(tmp_path: Path) -> None:
    done = claude(tmp_path, "plugin", "validate", "--profile", "directory", str(PLUGIN))
    assert done.returncode == 0, done.stdout + done.stderr


@needs_claude
async def test_the_plugin_installs_from_the_marketplace_and_its_server_starts(
        home: Path, tmp_path: Path) -> None:
    config = tmp_path / "claude"
    added = claude(config, "plugin", "marketplace", "add", str(FREE))
    assert added.returncode == 0, added.stdout + added.stderr
    installed = claude(config, "plugin", "install", "portward@portward")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    (manifest,) = (p for p in config.rglob(".mcp.json") if (p.parent / ".claude-plugin" / "plugin.json").exists())
    command = server_command(manifest.parent)
    assert command == LAUNCH
    launcher, *args = (part.replace("${CLAUDE_PLUGIN_ROOT}", str(manifest.parent)) for part in command)
    started = StdioServerParameters(command=launcher, args=args, env=server_env(**{HOME_ENV: str(home)}))
    async with Client(started) as client:
        assert client.server_info.version == VERSION
