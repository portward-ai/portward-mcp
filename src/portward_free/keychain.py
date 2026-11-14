"""Secrets live in the OS keychain only (macOS Keychain, Windows Credential Manager, Secret Service).

Two kinds are kept here: a database connection's password (account `connection:<name>`, see `account_for`; the
local database file stores the connection's settings without it) and the key that encrypts stored result rows
(`portward_free.kept`). Nothing in this module logs a secret.
"""

from __future__ import annotations

from typing import Protocol

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "Portward"


class KeychainError(Exception):
    """The keychain can't be used. The message is safe to show."""


class Keychain(Protocol):
    def set(self, account: str, secret: str) -> None: ...

    def get(self, account: str) -> str | None: ...

    def delete(self, account: str) -> None:
        """Remove the entry; no error when there is none."""
        ...


def account_for(connection_name: str) -> str:
    return f"connection:{connection_name}"


class OsKeychain:
    """The real keychain, through `keyring`."""

    def __init__(self, service: str = SERVICE) -> None:
        self._service = service

    def set(self, account: str, secret: str) -> None:
        try:
            keyring.set_password(self._service, account, secret)
        except KeyringError as err:
            raise KeychainError(f"couldn't save the secret in the keychain ({type(err).__name__})") from None

    def get(self, account: str) -> str | None:
        try:
            return keyring.get_password(self._service, account)
        except KeyringError as err:
            raise KeychainError(f"couldn't read the secret from the keychain ({type(err).__name__})") from None

    def delete(self, account: str) -> None:
        try:
            keyring.delete_password(self._service, account)
        except PasswordDeleteError:
            return  # nothing stored
        except KeyringError as err:
            raise KeychainError(f"couldn't remove the secret from the keychain ({type(err).__name__})") from None
