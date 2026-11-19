"""Per-agent caller credentials for ``sac listen`` (host_exec identity).

WHY THIS EXISTS
===============
The listen bearer is HOST-WIDE: every agent container on a host holds the
same one. A request authenticated with it proves only "some process on this
host", never WHICH agent sent it. Any identity carried in the request body
is therefore a claim, not a credential. Endpoints that authorise by agent
identity (``POST /v1/host_exec``'s group gate) need an identity bound to a
credential the caller cannot choose.

This module supplies that credential: one random token per agent launch,
delivered to the container as the secret env var :data:`AGENT_TOKEN_ENV`.
The in-container client presents it in the :data:`AGENT_TOKEN_HEADER`
header beside the host bearer; the auth middleware resolves it to the agent
name and stores that on ``request.state.authenticated_agent``.

STAGE, THEN COMMIT
==================
The env emitters (``listen_env_flags``, Hermes ``_sac_profile_env``) run
inside argv / profile assembly, which ``sac agents explain`` and
``--dry-run`` also execute without starting anything. So emitting and
activating are separate steps:

* :func:`stage_agent_token` (called by the emitters) returns the agent's
  PENDING token, creating one if none is pending. A pending token
  authenticates nothing. Repeated calls within one launch — the Hermes
  profile and the argv are rendered separately — return the same value.
* :func:`commit_agent_token` (called on the REAL start path only, past the
  dry-run return and the already-running guard, immediately before the
  container is launched) writes the pending token's hash as the agent's
  record — replacing the previous incarnation's, which stops working — and
  discards the pending copy. If nothing was staged, the stale record is
  removed instead, so a launch without a token never inherits an old one.

An ``explain`` therefore never rotates or revokes a running agent's token.

STORAGE
=======
* SERVER RECORD — ``<runtime base>/.agent-tokens/<name>.json`` (dir 0700,
  file 0600): ``{"name", "sha256", "started_at"}``. Only the SHA-256 is
  stored, never the token. ``runtime_base_dir()`` is the root the listen
  daemon already shares with the launcher (its single-instance lock lives
  there); the dot prefix keeps it out of the per-agent ``<name>/`` dirs.
* PENDING COPY — ``<agent state dir>/secret-env/agent.token.pending``
  (0600), beside the 0600 apptainer env-file the token ends up in.

:func:`resolve_agent_token` never raises: an unreadable record is logged
and skipped, so a damaged file can cost one agent its identity but cannot
break the listen daemon's request path.

LIMITS (stated, not hidden)
===========================
The token is as strong as the filesystem permissions protecting these
files. A process running as the same uid that can see the host home can
read or write them; this closes "identity taken from a request field", not
"same-uid filesystem access".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from .._logging import get_logger
from .._runtime_paths import runtime_base_dir

#: Env var that carries the per-agent token into the container. The name is
#: secret-shaped on purpose (``*_TOKEN``) so the apptainer secret lift moves
#: it out of the world-readable launcher argv into the 0600 env-file.
AGENT_TOKEN_ENV = "SAC_AGENT_TOKEN"

#: Request header the in-container client presents the token in.
AGENT_TOKEN_HEADER = "X-SAC-Agent-Token"

_REGISTRY_SUBDIR = ".agent-tokens"
_PENDING_SUBDIR = "secret-env"
_PENDING_FILENAME = "agent.token.pending"

# Agent names become file names here; refuse anything that could traverse.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

logger = get_logger(__name__)


def default_registry_dir() -> Path:
    """Server-side record dir: ``<runtime base>/.agent-tokens``."""
    return runtime_base_dir() / _REGISTRY_SUBDIR


def pending_token_path(state_dir: Path) -> Path:
    """The 0600 pending copy of the token under the agent's state dir."""
    return Path(state_dir).expanduser() / _PENDING_SUBDIR / _PENDING_FILENAME


def token_sha256(token: str) -> str:
    """Hex SHA-256 of ``token`` — the only form the server keeps."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _record_path(name: str, registry_dir: Path | None) -> Path:
    if not isinstance(name, str) or not _SAFE_NAME.match(name):
        raise ValueError(f"agent name {name!r} is not usable as a token record name")
    base = registry_dir if registry_dir is not None else default_registry_dir()
    return Path(base) / f"{name}.json"


def _write_owner_only(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` as 0600 inside a 0700 dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _read_pending(state_dir: Path) -> str | None:
    try:
        return pending_token_path(state_dir).read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def stage_agent_token(state_dir: Path) -> str:
    """Return the agent's pending token, creating one if none is pending.

    Never activates anything; see the module docstring. Callers must never
    log the return value.
    """
    existing = _read_pending(state_dir)
    if existing:
        return existing
    token = secrets.token_urlsafe(32)
    _write_owner_only(pending_token_path(state_dir), token)
    return token


def commit_agent_token(
    name: str,
    *,
    state_dir: Path,
    registry_dir: Path | None = None,
    now: float | None = None,
) -> bool:
    """Activate the staged token as ``name``'s credential.

    Replaces any previous record (the previous incarnation's token stops
    authenticating) and discards the pending copy. Returns ``True`` when a
    token was committed, ``False`` when none was staged — in which case the
    previous record is revoked, never left in force for a launch that does
    not carry its token.
    """
    record_path = _record_path(name, registry_dir)
    token = _read_pending(state_dir)
    if token is None:
        revoke_agent_token(name, registry_dir=registry_dir)
        return False
    record = {
        "name": name,
        "sha256": token_sha256(token),
        "started_at": time.time() if now is None else now,
    }
    _write_owner_only(record_path, json.dumps(record, sort_keys=True) + "\n")
    pending_token_path(state_dir).unlink()
    return True


def _load_record(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"agent token record {path} unreadable, skipped: {exc}")
        return None
    if not isinstance(data, dict):
        logger.warning(f"agent token record {path} is not an object, skipped")
        return None
    return data


def resolve_agent_token(token: str, *, registry_dir: Path | None = None) -> str | None:
    """Return the agent name ``token`` was committed for, or ``None``.

    Compares hashes in constant time against every record. A record whose
    ``name`` disagrees with its file name is ignored (it was not written by
    :func:`commit_agent_token`). Never raises.
    """
    if not token:
        return None
    base = Path(registry_dir) if registry_dir is not None else default_registry_dir()
    want = token_sha256(token)
    try:
        candidates = sorted(base.glob("*.json"))
    except OSError as exc:
        logger.warning(f"agent token registry {base} unreadable: {exc}")
        return None
    for path in candidates:
        record = _load_record(path)
        if record is None:
            continue
        got = record.get("sha256")
        if not isinstance(got, str) or record.get("name") != path.stem:
            continue
        if hmac.compare_digest(got, want):
            return path.stem
    return None


def revoke_agent_token(name: str, *, registry_dir: Path | None = None) -> None:
    """Delete ``name``'s server record. Revoking twice is a no-op."""
    try:
        _record_path(name, registry_dir).unlink()
    except FileNotFoundError:
        pass


__all__ = [
    "AGENT_TOKEN_ENV",
    "AGENT_TOKEN_HEADER",
    "commit_agent_token",
    "default_registry_dir",
    "pending_token_path",
    "resolve_agent_token",
    "revoke_agent_token",
    "stage_agent_token",
    "token_sha256",
]
