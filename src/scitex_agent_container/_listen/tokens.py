"""Bearer token storage for ``sac listen``.

v1: host-level token, one per host, stored under
``~/.scitex/agent-container/tokens/listen-<hostname>.token``.

Per SAC_OROCHI_SCOPES.md §4.1, per-agent scoped tokens are a future
extension; this module reserves the path layout to accept them later.
"""

from __future__ import annotations

import os
import secrets
import socket
from pathlib import Path

_DEFAULT_TOKEN_DIR = Path(".scitex") / "agent-container" / "tokens"


def default_token_path(home: Path | None = None, hostname: str | None = None) -> Path:
    """Canonical token file for this host."""
    _home = home or Path.home()
    _host = hostname or socket.gethostname()
    return _home / _DEFAULT_TOKEN_DIR / f"listen-{_host}.token"


def ensure_token(path: Path) -> str:
    """Read the token at ``path`` or atomically create one if missing.

    Returns the token string. New tokens are 32 random url-safe bytes
    (256 bits of entropy). File mode is 0600.
    """
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(token, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return token


def read_token(path: Path) -> str | None:
    """Return the token if the file exists, else None. Never raises."""
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
