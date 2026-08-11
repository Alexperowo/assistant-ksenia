"""Authentication for the private loopback llama.cpp API."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from butler.config import Settings


def api_key_file(settings: Settings) -> Path:
    return settings.runtime_dir / "llama-api-keys.txt"


def local_api_key(settings: Settings) -> str:
    """Return the persistent local key, creating it once when necessary.

    The file is deliberately kept outside configuration and source control.  It
    protects the loopback API from requests made by arbitrary web pages; it is
    not intended to defend against another program already running as the same
    Windows user.
    """
    path = api_key_file(settings)
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                return value
    except FileNotFoundError:
        pass

    key = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(key + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        # Windows ACLs normally inherit the current user's project access.
        pass
    temporary.replace(path)
    return key
