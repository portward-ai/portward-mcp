"""MCP registry metadata: server.json validates, names the published package, and proves we own it on PyPI.

The schema is vendored (tests/fixtures) so the test runs offline. `mcp-publisher validate` runs too when it is
on PATH or named by MCP_PUBLISHER; nothing is ever published.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from install_facts import FREE, PACKAGE, PINNED, VERSION, launch
from jsonschema import Draft7Validator
from mcp import Client

SERVER_JSON = FREE / "server.json"
SCHEMA = Path(__file__).parent / "fixtures" / "server.schema.json"


def metadata() -> dict[str, Any]:
    return json.loads(SERVER_JSON.read_text())


def test_server_json_validates_against_the_registry_schema_it_names() -> None:
    schema = json.loads(SCHEMA.read_text())
    assert metadata()["$schema"] == schema["$id"]
    errors = [f"{list(e.path)}: {e.message}" for e in Draft7Validator(schema).iter_errors(metadata())]
    assert errors == []


def test_server_json_names_this_package_at_this_version() -> None:
    meta = metadata()
    (package,) = meta["packages"]
    assert meta["version"] == package["version"] == VERSION
    assert (package["registryType"], package["identifier"]) == ("pypi", PACKAGE)
    assert package["transport"] == {"type": "stdio"}


def test_the_readme_carries_the_name_pypi_ownership_is_checked_against() -> None:
    assert f"mcp-name: {metadata()['name']}" in (FREE / "README.md").read_text()


async def test_the_package_runs_as_the_registry_describes_it(wheel: Path, home: Path) -> None:
    (package,) = metadata()["packages"]
    command = [package["runtimeHint"], f"{package['identifier']}=={package['version']}"]
    assert command == PINNED.split()
    async with Client(launch(command, wheel, home)) as client:
        assert client.server_info.version == VERSION


def test_mcp_publisher_validates_server_json() -> None:
    publisher = os.environ.get("MCP_PUBLISHER") or shutil.which("mcp-publisher")
    if not publisher:
        pytest.skip("mcp-publisher is not installed (set MCP_PUBLISHER to its path)")
    done = subprocess.run([publisher, "validate", str(SERVER_JSON)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
