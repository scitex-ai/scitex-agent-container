"""Bearer token storage for ``sac listen``.

v1: host-level token, one per host, stored under
``~/.scitex/agent-container/tokens/listen-<hostname>.token``.

Per-agent scoped tokens are a future
extension; this module reserves the path layout to accept them later.
"""

from __future__ import annotations

import os
import secrets
import socket
import stat
from pathlib import Path

_DEFAULT_TOKEN_DIR = Path(".scitex") / "agent-container" / "tokens"


def default_token_path(home: Path | None = None, hostname: str | None = None) -> Path:
    """Canonical token file for this host."""
    _home = home or Path.home()
    _host = hostname or socket.gethostname()
    return _home / _DEFAULT_TOKEN_DIR / f"listen-{_host}.token"


def default_owner_token_path(
    runtime_dir: Path | None = None, hostname: str | None = None
) -> Path:
    """Owner-only credential path that is never under a container-bound HOME."""
    if runtime_dir is not None:
        root = runtime_dir
    else:
        configured = os.environ.get("XDG_RUNTIME_DIR")
        candidate = Path(configured) if configured else Path(f"/run/user/{os.getuid()}")
        root = (
            candidate
            if candidate.is_dir()
            else Path("/dev/shm") / f"scitex-agent-container-{os.getuid()}"
        )
    host = hostname or socket.gethostname()
    return root / "scitex-agent-container" / f"fork-owner-{host}.token"


def _read_owner_token(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 4096
        ):
            raise PermissionError(
                f"owner credential must be an owner-controlled 0600 regular file: {path}"
            )
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise PermissionError("owner credential exceeds the safety limit")
        value = raw.decode("utf-8").strip()
        if len(value) < 32:
            raise PermissionError("owner credential is too short")
        return value
    finally:
        os.close(fd)


def ensure_owner_token(path: Path) -> str:
    """Read or safely create the host-owner credential without following links."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for component in [*reversed(path.parent.parents), path.parent]:
        if os.path.lexists(component) and component.is_symlink():
            raise PermissionError(
                f"owner credential path contains a symlink: {component}"
            )
    parent_info = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.geteuid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise PermissionError(
            f"owner credential directory must be owner-controlled 0700: {path.parent}"
        )
    try:
        return _read_owner_token(path)
    except FileNotFoundError:
        pass
    value = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read_owner_token(path)
    try:
        payload = value.encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("short write while creating owner credential")
            offset += written
        os.fsync(fd)
    except Exception:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    return _read_owner_token(path)


def read_owner_token(path: Path | None = None) -> str:
    """Read the owner credential for a bare-host control-plane client."""
    return _read_owner_token(path or default_owner_token_path())


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
