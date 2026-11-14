"""The Claude Desktop extension: one `.mcpb` file per OS, built from the compiled server, with no Python needed.

The manifest tests run everywhere. The bundle tests need a built `.mcpb`: set `PORTWARD_MCPB` to its path (CI does,
after `python mcpb/build.py`), or `PORTWARD_BUILD_MCPB=1` to build one here first (a few minutes, needs Nuitka's
C compiler and node).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from install_facts import FREE, VERSION
from mcp import Client, StdioServerParameters
from smoke import smoke_test

from portward_free.paths import HOME_ENV, database_path
from portward_free.storage import LocalStore

BUILD_DIR = FREE / "mcpb"
sys.path.insert(0, str(BUILD_DIR))  # the build script is a tool next to the package, not part of it

import build_mcpb  # noqa: E402

MANIFEST = BUILD_DIR / "manifest.json"


def manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def mcpb(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the pinned mcpb CLI (the one the build packs with)."""
    if shutil.which("npx") is None:
        pytest.skip("needs node (npx) on PATH")
    return subprocess.run(["npx", "--yes", build_mcpb.MCPB_CLI, *args], cwd=cwd, capture_output=True, text=True, timeout=300)


def test_the_manifest_names_the_package_version() -> None:
    assert manifest()["version"] == VERSION


def test_the_mcpb_tool_validates_the_manifest(tmp_path: Path) -> None:
    done = mcpb("validate", str(MANIFEST), cwd=tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_extension_keeps_its_data_in_the_users_app_data_folder_not_the_bundle() -> None:
    # Claude Desktop replaces the unpacked bundle on every update; history must outlive that.
    config = manifest()["server"]["mcp_config"]
    envs = [config.get("env", {}), *(o.get("env", {}) for o in config.get("platform_overrides", {}).values())]
    assert all(HOME_ENV not in env for env in envs)


# --- Signing: off until the certificates from #31 are configured, then every native file is signed. ---

MACHO = bytes.fromhex("cffaedfe") + b"\0" * 28  # a 64-bit Mach-O header
PE = b"MZ" + b"\0" * 62


def compiled_server(root: Path, files: dict[str, bytes]) -> Path:
    server = root / "server"
    for name, content in files.items():
        (server / name).parent.mkdir(parents=True, exist_ok=True)
        (server / name).write_bytes(content)
    return server


class Recorder:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> None:
        self.commands.append(command)


MAC_SERVER = {"portward-free": MACHO, "duckdb.so": MACHO, "libpython3.12.dylib": MACHO,
              "portward_free/panel/results.html": b"<html></html>"}
WIN_SERVER = {"portward-free.exe": PE, "python312.dll": PE, "duckdb.pyd": PE,
              "portward_free/panel/results.html": b"<html></html>"}


@pytest.mark.parametrize("platform, files", [("darwin", MAC_SERVER), ("win32", WIN_SERVER)])
def test_without_certificates_the_build_signs_nothing_and_reports_unsigned(
        platform: str, files: dict[str, bytes], tmp_path: Path) -> None:
    run = Recorder()
    outcome = build_mcpb.sign_server(compiled_server(tmp_path, files), platform, env={}, run=run)
    assert run.commands == []
    assert outcome == "unsigned"


MAC_IDENTITY = "Developer ID Application: Portward (TEAM123456)"


def test_with_a_developer_id_every_mach_o_file_is_signed_libraries_first(tmp_path: Path) -> None:
    server = compiled_server(tmp_path, MAC_SERVER)
    run = Recorder()
    outcome = build_mcpb.sign_server(server, "darwin", env={"PORTWARD_MACOS_SIGN_IDENTITY": MAC_IDENTITY}, run=run)
    assert outcome == "signed"
    signed = [Path(c[-1]).name for c in run.commands]
    assert sorted(signed[:-1]) == ["duckdb.so", "libpython3.12.dylib"]
    assert signed[-1] == "portward-free"  # the executable last, after what it loads
    for command in run.commands:
        assert command[:2] == ["codesign", "--force"]
        assert ["--sign", MAC_IDENTITY] == command[command.index("--sign"):command.index("--sign") + 2]
        assert {"--timestamp", "--options=runtime"} <= set(command)  # what notarization requires


def test_with_a_notary_profile_the_signed_server_is_notarized_and_the_build_waits(tmp_path: Path) -> None:
    run = Recorder()
    env = {"PORTWARD_MACOS_SIGN_IDENTITY": MAC_IDENTITY, "PORTWARD_NOTARY_PROFILE": "portward-notary"}
    build_mcpb.sign_server(compiled_server(tmp_path, MAC_SERVER), "darwin", env=env, run=run)
    zipped, notarize = run.commands[-2:]
    assert zipped[:3] == ["ditto", "-c", "-k"]
    assert notarize[:3] == ["xcrun", "notarytool", "submit"]
    assert notarize[3] == zipped[-1]
    assert {"--wait", "--keychain-profile"} <= set(notarize) and "portward-notary" in notarize
    assert all(c[0] == "codesign" for c in run.commands[:-2])


WIN_THUMBPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"


def test_with_a_windows_certificate_every_exe_dll_and_pyd_is_signed_and_timestamped(tmp_path: Path) -> None:
    run = Recorder()
    outcome = build_mcpb.sign_server(compiled_server(tmp_path, WIN_SERVER), "win32",
                                     env={"PORTWARD_WINDOWS_CERT_SHA1": WIN_THUMBPRINT}, run=run)
    assert outcome == "signed"
    (command,) = run.commands
    assert command[:2] == ["signtool", "sign"]
    assert ["/sha1", WIN_THUMBPRINT] == command[command.index("/sha1"):command.index("/sha1") + 2]
    assert ["/fd", "SHA256"] == command[command.index("/fd"):command.index("/fd") + 2]
    assert "/tr" in command and "/td" in command
    assert sorted(Path(p).name for p in command if Path(p).is_file()) == [
        "duckdb.pyd", "portward-free.exe", "python312.dll"]


def test_signing_settings_for_the_other_os_leave_this_build_unsigned(tmp_path: Path) -> None:
    run = Recorder()
    env = {"PORTWARD_WINDOWS_CERT_SHA1": WIN_THUMBPRINT}
    assert build_mcpb.sign_server(compiled_server(tmp_path, MAC_SERVER), "darwin", env=env, run=run) == "unsigned"
    assert run.commands == []


def test_a_notary_profile_without_a_signing_identity_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(build_mcpb.SigningConfigError, match="PORTWARD_MACOS_SIGN_IDENTITY"):
        build_mcpb.sign_server(compiled_server(tmp_path, MAC_SERVER), "darwin",
                               env={"PORTWARD_NOTARY_PROFILE": "portward-notary"}, run=Recorder())


# --- The built bundle, unpacked the way Claude Desktop installs it. ---

BUNDLE_ENV = "PORTWARD_MCPB"
BUILD_ENV = "PORTWARD_BUILD_MCPB"


@pytest.fixture(scope="session")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The built `.mcpb` for this OS: the one CI built (`PORTWARD_MCPB`), or one built now (`PORTWARD_BUILD_MCPB=1`)."""
    if os.environ.get(BUNDLE_ENV):
        return Path(os.environ[BUNDLE_ENV])
    if os.environ.get(BUILD_ENV) != "1":
        pytest.skip(f"set {BUNDLE_ENV} to a built .mcpb, or {BUILD_ENV}=1 to build one (minutes)")
    return build_mcpb.build(tmp_path_factory.mktemp("mcpb-dist"))


@pytest.fixture(scope="session")
def installed(bundle: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The bundle unpacked by the mcpb CLI, as Claude Desktop does on install."""
    folder = tmp_path_factory.mktemp("extension")
    done = mcpb("unpack", str(bundle), str(folder), cwd=folder)
    assert done.returncode == 0, done.stdout + done.stderr
    return folder


NO_KEYCHAIN = {"PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"}  # keyring's own do-nothing backend


def desktop_launch(folder: Path, home: Path, extra: dict[str, str] | None = None) -> StdioServerParameters:
    """What Claude Desktop runs: the manifest's command for this OS, with `${__dirname}` filled in.

    The environment carries no Python: the bundle must run on a machine without one. The test's in-memory keyring
    can't be loaded into a compiled program, so tests that don't check the keychain itself pass `NO_KEYCHAIN`:
    a pull seals what the AI was sent with a key from the keychain, and the real one may ask the user to allow it.
    """
    config = json.loads((folder / "manifest.json").read_text())["server"]["mcp_config"]
    config = {**config, **config.get("platform_overrides", {}).get(sys.platform, {})}
    fill = lambda s: s.replace("${__dirname}", str(folder))  # noqa: E731
    kept = ("HOME", "USER", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
    bare = {k: v for k, v in os.environ.items() if k in kept}
    env = {**bare, "PATH": os.pathsep.join(SYSTEM_PATH), HOME_ENV: str(home), "PORTWARD_USAGE": "off", "PORTWARD_BROWSER": "off",
           "PORTWARD_SHARE_URL": "http://127.0.0.1:9",
           **(extra or {})}  # usage counts off and no share service: a test never reaches portward.ai. No browser:
    # add_source would open the setup page on this computer and wait for a person to press Save there
    return StdioServerParameters(command=fill(config["command"]), args=[fill(a) for a in config.get("args", [])],
                                 env=env)


SYSTEM_PATH = [r"C:\Windows\System32"] if sys.platform == "win32" else ["/usr/bin", "/bin"]


def test_the_bundle_is_named_for_its_version_and_os(bundle: Path) -> None:
    # one file per OS and CPU: the manifest can't say which CPU a binary is for, so the name has to
    os_name = {"darwin": "macos", "win32": "windows"}[sys.platform]
    assert bundle.name == f"portward-{VERSION}-{os_name}-{build_mcpb.cpu()}.mcpb"


def test_the_bundle_is_for_this_os_only_and_its_entry_point_is_there(installed: Path) -> None:
    packed = json.loads((installed / "manifest.json").read_text())
    assert packed["version"] == VERSION
    assert packed["compatibility"]["platforms"] == [sys.platform]
    entry = installed / packed["server"]["entry_point"]
    assert entry.is_file() and os.access(entry, os.X_OK)


def test_the_bundle_carries_compiled_code_not_the_python_source(installed: Path) -> None:
    assert not list(installed.rglob("*.py"))


async def test_the_installed_extension_passes_the_smoke_test(installed: Path, home: Path, shop_db: Path) -> None:
    await smoke_test(desktop_launch(installed, home, NO_KEYCHAIN), home, shop_db, version=VERSION)


REAL_KEYCHAIN_ENV = "PORTWARD_TEST_REAL_KEYCHAIN"


@pytest.mark.skipif(os.environ.get(REAL_KEYCHAIN_ENV) != "1",
                    reason=f"uses this computer's real keychain: set {REAL_KEYCHAIN_ENV}=1 (CI does)")
async def test_the_installed_extension_finds_the_os_keychain(installed: Path, home: Path) -> None:
    # A compiled build can lose keyring's backend discovery and fall back to "no keychain", which would only show
    # when someone adds a database. Look up a password that was never stored: a working keychain says it isn't
    # there, a missing one says it can't be read.
    # Opt-in because every tool call also seals its log entry with Portward's key from the real keychain (creating
    # it if missing). On a machine where another build made that key, macOS asks the user to allow access, and the
    # test would wait on that dialog. On a fresh CI runner this build makes the key itself, so nothing asks.
    name = f"keychain-check-{uuid.uuid4().hex[:8]}"
    store = LocalStore.open(database_path(home))
    store.add_connection(name, "postgres", {"host": "127.0.0.1", "port": 1, "database": "none", "user": "none"})
    store.close()
    async with Client(desktop_launch(installed, home)) as client:
        reply = await client.call_tool("query", {"source": name, "question": "Check", "sql": "SELECT 1"})
    text = " ".join(getattr(c, "text", "") for c in reply.content)
    assert reply.is_error
    assert "isn't in the keychain" in text, text


def test_the_mcpb_tool_reads_the_bundle(bundle: Path, tmp_path: Path) -> None:
    done = mcpb("info", str(bundle), cwd=tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert VERSION in done.stdout
