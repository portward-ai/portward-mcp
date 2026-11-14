"""Setting up a wrapped server (`portward-free wrap-add`): test it, save it, say which of its tools are governed and
which stay hidden, and warn when the original, ungoverned server is still set up in an AI app on this computer.

Only a person at a terminal sets one up (it runs a command), on Personal. The login goes to the keychain; the
connection starts on profiles with none applied, so the AI sees nothing of it until the user applies a profile.

The check reads the AI apps' own config files (Claude Desktop, Claude Code, ChatGPT desktop / Codex). Cloud connectors
added in claude.ai or ChatGPT live in the user's account, not on this computer, so the summary always reminds the user
to look there too.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from portward_free.account import Accounts
from portward_free.keychain import Keychain, KeychainError, account_for
from portward_free.profile_tools import ProfileTools
from portward_free.runtime import opener_for
from portward_free.sources import SourceError
from portward_free.storage import LocalStore
from portward_free.wrap.providers import KIND_PREFIX, PROVIDERS, Provider, classify
from portward_free.wrap.server import WrapRefused
from portward_free.wrap.source import open_wrapped

PORTWARD_MARKERS = ("portward-free", "portward_free")
CLOUD_REMINDER = ("Portward can't see connectors added in claude.ai or ChatGPT settings from this computer: if one "
                  "reaches the same database, remove it, or the AI can go around Portward.")


@dataclass(frozen=True)
class ConfiguredServer:
    """One MCP server entry in an AI app's config."""

    client: str
    name: str
    path: Path
    command: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    url: str = ""  # a remote server (Claude Code `"type": "http"`, Codex `url`)
    headers: Mapping[str, str] = field(default_factory=dict)


def _strings(raw: Any) -> dict[str, str]:
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _entry(client: str, path: Path, name: str, raw: Any) -> ConfiguredServer | None:
    if not isinstance(raw, dict):
        return None
    command = raw.get("command")
    args = raw.get("args") or []
    line = ([command] if isinstance(command, str) else []) + [str(a) for a in args if isinstance(a, str | int)]
    url = raw.get("url") if isinstance(raw.get("url"), str) else ""
    headers = {**_strings(raw.get("headers")), **_strings(raw.get("http_headers"))}  # Codex: http_headers
    return ConfiguredServer(client, str(name), path, tuple(line), _strings(raw.get("env")), url, headers)


def _json_servers(client: str, path: Path) -> list[ConfiguredServer]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return []
    blocks = [data.get("mcpServers")]
    projects = data.get("projects")  # Claude Code keeps per-project servers in ~/.claude.json
    if isinstance(projects, dict):
        blocks += [p.get("mcpServers") for p in projects.values() if isinstance(p, dict)]
    found = []
    for block in blocks:
        if isinstance(block, dict):
            found += [e for n, raw in block.items() if (e := _entry(client, path, n, raw)) is not None]
    return found


def _toml_servers(client: str, path: Path) -> list[ConfiguredServer]:
    block = tomllib.loads(path.read_text(encoding="utf-8")).get("mcp_servers")
    if not isinstance(block, dict):
        return []
    return [e for n, raw in block.items() if (e := _entry(client, path, n, raw)) is not None]


@dataclass(frozen=True)
class ClientConfigs:
    """The AI apps' config files to check: (app, path); a missing or unreadable file is skipped."""

    files: tuple[tuple[str, Path], ...]

    @classmethod
    def on_this_computer(cls, home: Path | None = None, cwd: Path | None = None,
                         platform: str = sys.platform, env: Mapping[str, str] = os.environ) -> ClientConfigs:
        home = home or Path.home()
        if platform == "darwin":
            desktop = home / "Library" / "Application Support" / "Claude"
        elif platform.startswith("win"):
            desktop = Path(env.get("APPDATA", home / "AppData" / "Roaming")) / "Claude"
        else:
            desktop = home / ".config" / "Claude"
        return cls((("Claude Desktop", desktop / "claude_desktop_config.json"),
                    ("Claude Code", home / ".claude.json"),
                    ("Claude Code", (cwd or Path.cwd()) / ".mcp.json"),
                    ("ChatGPT desktop / Codex", home / ".codex" / "config.toml")))

    def servers(self) -> tuple[ConfiguredServer, ...]:
        found: list[ConfiguredServer] = []
        for client, path in self.files:
            try:
                found += _toml_servers(client, path) if path.suffix == ".toml" else _json_servers(client, path)
            except (OSError, ValueError, tomllib.TOMLDecodeError):
                continue
        return tuple(found)


def originals_still_set_up(provider: Provider, command: Sequence[str], secret: str,
                           servers: Sequence[ConfiguredServer], url: str = "") -> tuple[str, ...]:
    """A warning for each app entry that reaches the wrapped data without Portward: the same command or URL, the
    provider's server by another command line or URL, or the same login."""
    said = []
    for server in servers:
        if any(m in part for part in server.command for m in PORTWARD_MARKERS):
            continue
        line = (*server.command, server.url) if server.url else server.command
        same_login = bool(secret) and (secret in server.env.values() or secret in server.command
                                       or any(secret in v for v in server.headers.values()))
        same = ((command and tuple(server.command) == tuple(command)) or (url and url in line)
                or provider.looks_like(line) or same_login)
        if same:
            said.append(f"{server.client} still has the original {provider.label} server {server.name!r} set up "
                        f"({server.path}): remove it there, or the AI can go around Portward.")
    return tuple(said)


@dataclass(frozen=True)
class Added:
    name: str
    label: str
    governed: tuple[str, ...]
    hidden: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]

    def summary(self) -> str:
        lines = [f"Wrapped {self.label} as {self.name!r}. Portward applies your profiles to it; until you apply one, "
                 "the AI sees nothing of it.",
                 f"Governed (your profiles apply): {', '.join(self.governed)}."]
        if self.hidden:
            lines.append("Hidden from the AI (Portward can't govern them):")
            lines += [f"  - {tool}: {why}" for tool, why in self.hidden]
        lines.append(f"In your AI app, use `portward-free wrap {self.name}` in place of the original server.")
        lines += [f"Warning: {w}" for w in self.warnings]
        lines.append(CLOUD_REMINDER)
        return "\n".join(lines)


def add_wrapped(store: LocalStore, keychain: Keychain, accounts: Accounts, name: str, provider_name: str,
                command: Sequence[str], secret: str, *, clients: ClientConfigs,
                env: Mapping[str, str] | None = None, url: str | None = None) -> Added:
    """Test the server, then save it as the connection `name` (WrapRefused, nothing saved, if it won't run). A
    local server is started by `command`; a remote one (Snowflake's) is reached at `url`."""
    _, why = ProfileTools(store, keychain, accounts, opener_for(keychain)).personal_email()
    if why:
        raise WrapRefused(f"wrap mode is part of Portward Personal. {why}")
    provider = PROVIDERS.get(provider_name)
    if provider is None:
        raise WrapRefused(f"Portward wraps these servers: {', '.join(sorted(PROVIDERS))}")
    existing = store.connection(name)
    if existing is not None and not existing.kind.startswith(KIND_PREFIX):
        raise WrapRefused(f"there is already a source named {name!r}; pick another name")
    kind = f"{KIND_PREFIX}{provider.name}"
    try:
        settings = provider.settings(command, url, env or {})
        source = open_wrapped(kind, settings, secret)
        try:
            found = classify(provider, source.tools)
            source.tables()  # the catalog query works with this login
        finally:
            source.close()
        keychain.set(account_for(name), secret)
    except (SourceError, KeychainError) as err:
        raise WrapRefused(f"{name}: {err}") from None
    store.add_connection(name, kind, settings)
    store.profiles.switch_to_profiles(name)
    warnings = originals_still_set_up(provider, command, secret, clients.servers(), str(settings.get("url") or ""))
    return Added(name, provider.label, found.governed, found.hidden, warnings)
