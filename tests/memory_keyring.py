"""A keyring backend held in memory, for the stdio server process the tests start (PYTHON_KEYRING_BACKEND)."""

import json
import os
from pathlib import Path

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class MemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self._secrets: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._secrets.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._secrets[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self._secrets.pop((service, username), None) is None:
            raise PasswordDeleteError(username)


SEED_ENV = "PORTWARD_TEST_KEYRING_SEED"  # a JSON file {account: secret} of the Portward service, read at start


class SeededKeyring(MemoryKeyring):
    """MemoryKeyring holding, from the start, what a test set up in process (e.g. a wrapped server's login)."""

    def __init__(self) -> None:
        super().__init__()
        seed = os.environ.get(SEED_ENV)
        if seed:
            for account, secret in json.loads(Path(seed).read_text(encoding="utf-8")).items():
                self.set_password("Portward", account, secret)


def server_env(**extra: str) -> dict[str, str]:
    """The environment for a server process a test starts: this backend instead of the real OS keychain, and usage
    counts off, so a test never posts to the real collection endpoint, no web browser (the setup page and open_page
    never open a real tab from a server process), and a share service address that answers
    nothing (port 9 on this machine), so a test never reaches the real share service."""
    path = os.pathsep.join(filter(None, (str(Path(__file__).parent), os.environ.get("PYTHONPATH"))))
    return {**os.environ, "PYTHON_KEYRING_BACKEND": f"{__name__}.MemoryKeyring", "PYTHONPATH": path,
            "PORTWARD_USAGE": "off", "PORTWARD_BROWSER": "off",
            "PORTWARD_SHARE_URL": "http://127.0.0.1:9", **extra}
