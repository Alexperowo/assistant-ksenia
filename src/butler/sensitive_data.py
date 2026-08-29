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

SENSITIVE_DIRECTORIES = {
    ".aws",
    ".azure",
    ".docker",
    ".git",
    ".gnupg",
    ".kube",
    ".ssh",
    "gcloud",
}


def is_sensitive_path(path: Path) -> bool:
    """Identify files whose contents must never enter an LLM prompt."""
    if any(part.casefold() in SENSITIVE_DIRECTORIES for part in path.parts):
        return True
    name = path.name.casefold()
    if name in SENSITIVE_FILENAMES or name.startswith(".env."):
        return True
    if path.suffix.casefold() in SENSITIVE_SUFFIXES:
        return True
    return any(
        marker in name
        for marker in ("credential", "private-key", "private_key", "secret")
    )
