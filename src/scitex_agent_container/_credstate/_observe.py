"""Measure what is actually on THIS node's disk — presence only, never value.

Every read here is safe in the specific sense
:mod:`.._account._keepalive_guards` established: the presence of a field
is read, never the field's value and never its validity. That module
records why, and the reason is not squeamishness — it is that probing
whether a refresh token still works is a WRITE. When a stale refresh
token was rejected with 401, Claude Code CLEARED the ``refreshToken``
field outright. A "health check" that destroys the credential it checks
is worse than no health check, so this module never performs one.

What it can therefore establish, cheaply and without risk:

* does the locator resolve at all (file exists / env var set);
* the file's PERMISSION BITS, which is a real exposure finding in its
  own right and is not recorded anywhere today;
* whether refresh material is PRESENT — the one-bit "am I the origin"
  test, which is what makes the single-refresher invariant checkable;
* the artifact's own declared expiry, which is a fact about the token
  rather than a fact about whether a timer ran.

That last distinction is the whole point. Eight subagents died on
expired credentials at a moment when the refresh timer had fired 23
minutes earlier and was scheduled normally. The timer was green because
timers report on themselves. This module reports on the artifact.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .._account._keepalive_guards import find_refresh_keys

#: Locator schemes this module can resolve. A locator with an unknown
#: scheme is reported as UNRESOLVABLE rather than assumed absent —
#: "I cannot check this" and "this is missing" are different answers and
#: conflating them is how a gap becomes invisible.
SCHEME_FILE = "file"
SCHEME_ENV = "env"


@dataclass(frozen=True)
class LocalObservation:
    """One node's measurement of one locator, at one moment."""

    locator: str
    present: bool
    scheme: str | None = None
    file_mode: str | None = None
    holds_refresh_material: bool | None = None
    artifact_expires_at: datetime | None = None
    world_readable: bool = False
    detail: str | None = None


def parse_locator(locator: str) -> tuple[str | None, str]:
    """Split ``scheme:rest``. Returns ``(None, locator)`` if unschemed.

    An unschemed locator is not guessed at. ADR-0022's posture — raise
    rather than guess — applies with more force here: guessing that a
    bare string is a path could mean reporting a credential PRESENT
    because an unrelated file happened to exist at that name.
    """
    if ":" not in locator:
        return None, locator
    scheme, rest = locator.split(":", 1)
    scheme = scheme.strip().lower()
    if scheme not in {SCHEME_FILE, SCHEME_ENV}:
        return None, locator
    return scheme, rest.strip()


def _find_expiry_ms(payload: Any) -> int | None:
    """Recursively find an ``expiresAt``-shaped field. Never returns a token.

    Recursive for the same reason the refresh-key scan is: the nesting
    shape is exactly what varies between ``.credentials.json`` dialects,
    and a reader pinned to one shape reports "no expiry" on the other —
    which reads as "never expires".
    """
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if isinstance(key, str) and key.lower() in {"expiresat", "expires_at"}:
                if isinstance(value, (int, float)) and value > 0:
                    return int(value)
            found = _find_expiry_ms(value)
            if found is not None:
                return found
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            found = _find_expiry_ms(value)
            if found is not None:
                return found
    return None


def _observe_file(path_str: str, *, home: Path | None) -> LocalObservation:
    locator = f"{SCHEME_FILE}:{path_str}"
    expanded = path_str
    if home is not None and expanded.startswith("~"):
        expanded = str(home) + expanded[1:]
    path = Path(os.path.expanduser(expanded))
    if not path.is_file():
        return LocalObservation(
            locator=locator,
            present=False,
            scheme=SCHEME_FILE,
            detail=f"no file at {path}",
        )

    mode_bits = stat.S_IMODE(path.stat().st_mode)
    world_readable = bool(mode_bits & (stat.S_IROTH | stat.S_IWOTH))
    observation = {
        "file_mode": f"{mode_bits:04o}",
        "world_readable": world_readable,
    }

    holds_refresh: bool | None = None
    expires: datetime | None = None
    detail: str | None = None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # Not JSON, or unreadable. Presence is still a real answer, and
        # it is the answer that matters most; say what could not be
        # established rather than implying it was checked.
        detail = f"present, but structure not readable ({type(exc).__name__})"
    else:
        holds_refresh = bool(find_refresh_keys(payload))
        expiry_ms = _find_expiry_ms(payload)
        if expiry_ms is not None:
            expires = datetime.fromtimestamp(expiry_ms / 1000.0, tz=timezone.utc)

    if world_readable and detail is None:
        detail = f"mode {observation['file_mode']} — readable beyond its owner"

    return LocalObservation(
        locator=locator,
        present=True,
        scheme=SCHEME_FILE,
        file_mode=str(observation["file_mode"]),
        holds_refresh_material=holds_refresh,
        artifact_expires_at=expires,
        world_readable=world_readable,
        detail=detail,
    )


def _observe_env(name: str, *, env: Mapping[str, str] | None) -> LocalObservation:
    source = os.environ if env is None else env
    value = source.get(name)
    present = bool(value)
    return LocalObservation(
        locator=f"{SCHEME_ENV}:{name}",
        present=present,
        scheme=SCHEME_ENV,
        detail=(
            None
            if present
            else f"environment variable {name} is unset or empty in this process"
        ),
    )


def observe_locator(
    locator: str,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> LocalObservation:
    """Measure one locator on this node. Reads presence, never value."""
    scheme, rest = parse_locator(locator)
    if scheme == SCHEME_FILE:
        return _observe_file(rest, home=home)
    if scheme == SCHEME_ENV:
        return _observe_env(rest, env=env)
    return LocalObservation(
        locator=locator,
        present=False,
        scheme=None,
        detail=(
            "unresolvable locator: expected 'file:<path>' or 'env:<VARNAME>'. "
            "Reported as unresolvable rather than absent on purpose — "
            "'I cannot check this' is a different answer from 'this is "
            "missing', and treating them alike is how a gap goes unnoticed."
        ),
    )
