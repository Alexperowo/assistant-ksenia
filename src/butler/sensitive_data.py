from __future__ import annotations

from pathlib import Path


SENSITIVE_FILENAMES = {
    ".env",
    ".env.local",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
}

SENSITIVE_SUFFIXES = {
    ".key",
    ".kdbx",
    ".p12",
    ".pem",
    ".pfx",
}


def is_sensitive_path(path: Path) -> bool:
    """Identify files whose contents must never enter an LLM prompt."""
    name = path.name.casefold()
    if name in SENSITIVE_FILENAMES or name.startswith(".env."):
        return True
    if path.suffix.casefold() in SENSITIVE_SUFFIXES:
        return True
    return any(
        marker in name
        for marker in ("credential", "private-key", "private_key", "secret")
    )
