"""Installing: the built package runs from the one-line commands people paste into Claude Code and ChatGPT desktop.

These tests build the wheel and launch it with `uvx`, the launcher the install commands use, so they check the
package as it would be installed, not the source tree.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from install_facts import FREE, PINNED, VERSION, from_wheel, launch
from smoke import smoke_test
from test_storage_migrations import version_1_file

from portward_free.paths import database_path
from portward_free.storage import LATEST_VERSION, schema_version


pytestmark = pytest.mark.skipif(shutil.which("uvx") is None, reason="needs uv (uvx) on PATH")


def launched(wheel: Path, home: Path) -> StdioServerParameters:
    return launch(PINNED.split(), wheel, home)


INSTALL_COMMANDS = {
    "Claude Code": f"claude mcp add --scope user --transport stdio portward -- {PINNED}",
    "ChatGPT desktop": f"codex mcp add portward -- {PINNED}",
}


@pytest.mark.parametrize("app", INSTALL_COMMANDS)
def test_the_readme_gives_one_pinned_install_command_per_app(app: str) -> None:
    lines = [line.strip() for line in (FREE / "README.md").read_text().splitlines()]
    assert INSTALL_COMMANDS[app] in lines


def registered(app: str, config_dir: Path) -> list[str]:
    """Run the README's command for `app` against a throwaway config folder; return the server command it saved."""
    tool, *args = INSTALL_COMMANDS[app].split()
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} is not installed")
    env_var = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}[tool]
    done = subprocess.run([tool, *args], env={**os.environ, env_var: str(config_dir)},
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    if tool == "codex":
        entry = tomllib.loads((config_dir / "config.toml").read_text())["mcp_servers"]["portward"]
    else:
        entry = json.loads((config_dir / ".claude.json").read_text())["mcpServers"]["portward"]
    return [entry["command"], *entry["args"]]


@pytest.mark.parametrize("app", INSTALL_COMMANDS)
def test_each_app_accepts_its_install_command_and_saves_the_pinned_launcher(app: str, tmp_path: Path) -> None:
    assert registered(app, tmp_path) == PINNED.split()


def test_the_chatgpt_config_entry_in_the_readme_runs_the_same_command() -> None:
    readme = (FREE / "README.md").read_text()
    block = readme.split("```toml", 1)[1].split("```", 1)[0]
    entry = tomllib.loads(block)["mcp_servers"]["portward"]
    assert [entry["command"], *entry["args"]] == PINNED.split()


async def test_the_installed_package_starts_the_server_and_reports_its_version(wheel: Path, home: Path) -> None:
    async with Client(launched(wheel, home)) as client:
        assert client.server_info.name == "portward"
        assert client.server_info.version == VERSION
        tools = {t.name for t in (await client.list_tools()).tools}
    assert {"add_source", "list_sources", "query"} <= tools


async def test_the_installed_package_passes_the_smoke_test(wheel: Path, home: Path, shop_db: Path) -> None:
    await smoke_test(launched(wheel, home), home, shop_db, version=VERSION)


def test_the_installed_command_prints_its_version(wheel: Path) -> None:
    done = subprocess.run(from_wheel([*PINNED.split(), "--version"], wheel), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == f"portward {VERSION}"


def test_the_wheel_carries_the_license_and_the_project_links(wheel: Path) -> None:
    # Apache-2.0 asks that every copy carries the license text, and PyPI shows the links on the project page.
    with zipfile.ZipFile(wheel) as built:
        names = built.namelist()
        (metadata,) = [n for n in names if n.endswith(".dist-info/METADATA")]
        text = built.read(metadata).decode()
    assert any(n.endswith(".dist-info/licenses/LICENSE") for n in names), names
    assert "License-File: LICENSE" in text
    assert "Project-URL: Homepage, https://portward.ai" in text
    assert "Project-URL: Documentation, https://docs.portward.ai" in text


async def test_upgrading_from_an_older_release_keeps_its_connections_settings_and_history(
        wheel: Path, home: Path, shop_db: Path) -> None:
    version_1_file(home, shop_db)  # what the first release left in the app-data folder
    async with Client(launched(wheel, home)) as client:
        reply = await client.call_tool("query", {"source": "shop", "question": "Customers",
                                                 "sql": "SELECT name FROM customers"})
        assert not reply.is_error
    # the next install of the package (a new uvx environment) finds the same folder and its history
    async with Client(launched(wheel, home)) as client:
        listed = await client.call_tool("list_sources", {})
        assert "shop" in str(listed.structured_content)
    db = sqlite3.connect(database_path(home))
    assert schema_version(db) == LATEST_VERSION
    assert db.execute("SELECT value FROM settings WHERE key = 'usage_counts'").fetchone() == ("off",)
    assert [r[0] for r in db.execute("SELECT question FROM pulls")] == ["Customers"]
    db.close()
