"""Where the local database file lives: the user's app-data folder."""

from __future__ import annotations

import os
import sys
from pathlib import Path

HOME_ENV = "PORTWARD_HOME"  # overrides the app-data folder (tests, portable installs)
DATABASE_FILE = "portward.db"
APP_FOLDER = "Portward"


def app_data_dir() -> Path:
    """The per-user app-data folder: macOS Application Support, Windows %APPDATA%, else XDG_DATA_HOME."""
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_FOLDER
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_FOLDER
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_FOLDER.lower()


def database_path(home: Path | None = None) -> Path:
    return (home or app_data_dir()) / DATABASE_FILE
