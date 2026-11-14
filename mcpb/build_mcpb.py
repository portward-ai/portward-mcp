"""Build the Claude Desktop extension, `portward-<version>-<os>-<cpu>.mcpb`, from the compiled server.

    python mcpb/build_mcpb.py --out dist/        (from free/, with uv and node on PATH)

Runs on the OS and CPU it builds for (macOS or Windows): Nuitka compiles the server into a standalone folder (no
Python needed to run it), the folder is signed when certificates are configured (see `sign_server`), and the pinned
mcpb CLI packs it with the manifest. Nothing is uploaded.
"""

from __future__ import annotations

import argparse
import json
import os
import platform as host
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path

FREE = Path(__file__).resolve().parents[1]
HERE = FREE / "mcpb"
MCPB_CLI = "@anthropic-ai/mcpb@2.1.2"
NUITKA = "nuitka==2.8.10"  # the version the gateway image is compiled with

Run = Callable[[list[str]], None]

MACOS_IDENTITY_ENV = "PORTWARD_MACOS_SIGN_IDENTITY"  # a "Developer ID Application: ..." identity in the keychain
NOTARY_PROFILE_ENV = "PORTWARD_NOTARY_PROFILE"  # `xcrun notarytool store-credentials` profile name
WINDOWS_CERT_ENV = "PORTWARD_WINDOWS_CERT_SHA1"  # thumbprint of the code-signing certificate in the cert store
TIMESTAMP_URL_ENV = "PORTWARD_TIMESTAMP_URL"
DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"

MACHO_MAGIC = {bytes.fromhex(m) for m in ("cffaedfe", "cefaedfe", "cafebabe", "feedfacf", "feedface")}
EXECUTABLE = {"darwin": "portward-free", "win32": "portward-free.exe"}
OS_NAME = {"darwin": "macos", "win32": "windows"}


class SigningConfigError(Exception):
    """The signing settings are half there: fail rather than ship something less signed than intended."""


def _run(command: list[str], env: Mapping[str, str] | None = None) -> None:
    """Run a build step; its output goes to stderr so stdout carries only the built file's path."""
    subprocess.run(command, check=True, env=env, stdout=sys.stderr)


def _is_native(path: Path, platform: str) -> bool:
    with path.open("rb") as f:
        head = f.read(4)
    return head[:2] == b"MZ" if platform == "win32" else head in MACHO_MAGIC


def _native(server: Path, platform: str) -> list[Path]:
    """The folder's native files, libraries first and the executable last (inside-out, as codesign needs)."""
    main = server / EXECUTABLE[platform]
    found = sorted(p for p in server.rglob("*")
                   if p.is_file() and not p.is_symlink() and p != main and _is_native(p, platform))
    return [*found, main]


def _sign_macos(server: Path, identity: str, run: Run) -> None:
    for path in _native(server, "darwin"):
        run(["codesign", "--force", "--timestamp", "--options=runtime", "--sign", identity, str(path)])


def _notarize(server: Path, profile: str, run: Run) -> None:
    """Send the signed folder to Apple's notary service and wait for the verdict (a rejection fails the build).

    A bare executable can't be stapled, so Gatekeeper looks the ticket up online on first launch.
    """
    with tempfile.TemporaryDirectory() as scratch:
        archive = str(Path(scratch) / "portward-server.zip")
        run(["ditto", "-c", "-k", "--keepParent", str(server), archive])
        run(["xcrun", "notarytool", "submit", archive, "--keychain-profile", profile, "--wait"])


def _sign_windows(server: Path, thumbprint: str, timestamp: str, run: Run) -> None:
    run(["signtool", "sign", "/sha1", thumbprint, "/fd", "SHA256", "/tr", timestamp, "/td", "SHA256",
         *map(str, _native(server, "win32"))])


def sign_server(server: Path, platform: str, env: Mapping[str, str], run: Run = _run) -> str:
    """Sign every native file in the compiled server folder; return "signed" or "unsigned".

    Signing switches on when the certificate settings are in `env` (CI secrets once #31 has the certificates);
    without them the build is unsigned, which is fine for testing.
    """
    if platform == "darwin":
        identity, profile = env.get(MACOS_IDENTITY_ENV), env.get(NOTARY_PROFILE_ENV)
        if profile and not identity:
            raise SigningConfigError(f"{NOTARY_PROFILE_ENV} is set but {MACOS_IDENTITY_ENV} is not")
        if not identity:
            return "unsigned"
        _sign_macos(server, identity, run)
        if profile:
            _notarize(server, profile, run)
        return "signed"
    if platform == "win32" and env.get(WINDOWS_CERT_ENV):
        _sign_windows(server, env[WINDOWS_CERT_ENV], env.get(TIMESTAMP_URL_ENV) or DEFAULT_TIMESTAMP_URL, run)
        return "signed"
    return "unsigned"


def cpu() -> str:
    """This machine's CPU as the file name says it: arm64 or x64."""
    machine = host.machine().lower()
    return {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}[machine]


def version() -> str:
    return tomllib.loads((FREE / "pyproject.toml").read_text())["project"]["version"]


def compile_server(work: Path) -> Path:
    """Compile the server with Nuitka into a standalone folder; return that folder.

    The package is installed non-editable into a throwaway environment, from the lock file, without dev tools, so
    the build has exactly what the published package has (and Nuitka can find the package's own metadata, which
    `portward --version` and the server's reported version read).
    """
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(work / "venv")}
    command = [
        # --reinstall-package: uv would otherwise reuse its cached wheel of this version and compile stale code
        "uv", "run", "--project", str(FREE), "--frozen", "--no-dev", "--no-editable", "--reinstall-package",
        "portward-free", "--with", NUITKA,
        "python", "-m", "nuitka", "--mode=standalone", "--assume-yes-for-downloads", "--remove-output",
        f"--output-dir={work / 'nuitka'}", "--output-filename=portward-free",
        "--include-package=portward_free", "--include-package-data=portward_free",
        "--include-distribution-metadata=portward-free",
        # No GUI toolkit: nothing of ours uses Tk, and Nuitka 2.8.10 stops on uv's Python 3.12.13, whose _tkinter
        # links Tcl 9 by @rpath ("problem with dependency scan of ... libtcl9.0.dylib").
        "--nofollow-import-to=tkinter", "--nofollow-import-to=_tkinter", "--disable-plugin=tk-inter",
        str(HERE / "entry.py"),
    ]
    _run(command, env=env)
    return work / "nuitka" / "entry.dist"


def stage(compiled: Path, bundle: Path, platform: str) -> Path:
    """Lay out the bundle folder: the manifest (narrowed to this OS), the compiled server, the licence."""
    manifest = json.loads((HERE / "manifest.json").read_text())
    manifest["server"]["entry_point"] = f"server/{EXECUTABLE[platform]}"
    manifest["compatibility"]["platforms"] = [platform]
    bundle.mkdir(parents=True)
    shutil.copytree(compiled, bundle / "server", symlinks=True)
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copy2(FREE / "LICENSE", bundle / "LICENSE")
    return bundle


def pack(bundle: Path, out: Path, run: Run = _run) -> None:
    npx = shutil.which("npx") or "npx"  # npx.cmd on Windows
    run([npx, "--yes", MCPB_CLI, "pack", str(bundle), str(out)])


def build(out_dir: Path, *, platform: str = sys.platform, env: Mapping[str, str] = os.environ) -> Path:
    """Compile, stage, sign (when configured) and pack; return the path of the `.mcpb` file."""
    if platform not in OS_NAME:
        raise SystemExit(f"the extension is built on macOS or Windows, not {platform}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"portward-{version()}-{OS_NAME[platform]}-{cpu()}.mcpb"
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        bundle = stage(compile_server(work), work / "bundle", platform)
        signed = sign_server(bundle / "server", platform, env)
        pack(bundle, out)
    print(f"built {out} ({signed})", file=sys.stderr)
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=FREE / "dist", help="folder for the .mcpb (default: free/dist)")
    print(build(parser.parse_args(argv).out))


if __name__ == "__main__":
    main()
