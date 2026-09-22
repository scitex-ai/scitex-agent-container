"""Per-agent PostgreSQL identity — inject ``PGUSER`` when absent.

b2 of the pg55432 role rework (operator-approved 2026-08-24): the shared
superuser stopped travelling inside DSN userinfo. Specs now carry a
userinfo-less ``SCITEX_CARDS_DB`` (dotfiles PR #391), the per-agent roles
``<host_user>__<agent>`` exist in the cluster, and libpq resolves the password
from ``PGPASSFILE`` on its own. What remains is the USER: with no userinfo and
no ``PGUSER``, libpq falls back to the OS user — inside a container that is
the invoking host user, whose role is deliberately ``NOLOGIN`` (it is the
permission umbrella, not a login identity). Every agent would fail to connect
at its next start, loudly but pointlessly.

This module supplies the missing name the same way
:mod:`._board_identity_env` supplies the board identity: derived from the
agent's project identity at launch, injected only when NOTHING else declares it.
116 specs therefore need no per-spec ``PGUSER`` line (D15: generate, don't
hardcode), and a spec that DOES declare one — in ``spec.env`` or in
``apptainer.raw_args`` — always wins, silently, because a default exists in
order to be overridden (:mod:`._fleet_env`'s own rule).

The derived name is ``<host_user>__<project_name>`` when the spec declares its
project label, falling back to ``<host_user>__<agent_name>`` for old or
project-less configs.  This distinction is load-bearing for variant agents:
``project-gui`` and ``project-deepseek`` are incarnations of the provisioned
``project`` database principal, not new principals that happen to have similar
names. ``host_user`` is the OS user launching the container
(``getpass.getuser()``), not a constant: the role tree is per-owner
(``ywatanabe`` today, anyone else the day the fleet gains a second human), and
sac's neutrality rule — logic never names a consumer — holds.
"""

from __future__ import annotations

import getpass
import scitex_logging as slogging
import os
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from ._board_identity_env import raw_args_env

logger = slogging.getLogger(__name__)

# The libpq user variable. Everything speaking to PostgreSQL through libpq or
# psycopg honours it; nothing else in the fleet uses the name.
PG_USER_ENV = "PGUSER"


class PgIdentityCredentialError(RuntimeError):
    """The selected PostgreSQL identity has no usable declared credential."""


def _pgpass_fields(line: str) -> tuple[str, ...] | None:
    """Split one libpq passfile row without returning its password."""
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for char in line.rstrip("\r\n"):
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    fields.append("".join(current))
    if len(fields) != 5:
        return None
    return tuple(fields[:4])


def _pgpass_target(dsn: str) -> tuple[str, str, str] | None:
    try:
        parsed = urlsplit(dsn)
        if (
            parsed.scheme not in {"postgresql", "postgres"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        return (
            parsed.hostname,
            str(parsed.port or 5432),
            unquote(parsed.path.lstrip("/").split("/", 1)[0]),
        )
    except ValueError:
        return None


def _pgpass_is_private_regular(path: str | Path) -> bool:
    """Match libpq's private-file boundary and reject symlink redirection."""
    candidate = Path(path).expanduser()
    try:
        metadata = candidate.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and metadata.st_mode & 0o077 == 0
    )


def pgpass_has_role(path: str | Path, role: str, *, dsns: Sequence[str] = ()) -> bool:
    """Return whether a passfile carries ``role`` for every declared target."""
    candidate = Path(path).expanduser()
    if not _pgpass_is_private_regular(candidate):
        return False
    declared_dsns = tuple(dsns)
    targets = tuple(target for dsn in declared_dsns if (target := _pgpass_target(dsn)))
    if len(targets) != len(declared_dsns):
        return False
    matched_targets: set[tuple[str, str, str]] = set()
    try:
        with candidate.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                fields = _pgpass_fields(line)
                if fields is None or fields[3] not in {role, "*"}:
                    continue
                if not targets:
                    return True
                for target in targets:
                    if all(
                        declared in {"*", actual}
                        for declared, actual in zip(fields[:3], target, strict=True)
                    ):
                        matched_targets.add(target)
    except OSError:
        return False
    return bool(targets) and len(matched_targets) == len(set(targets))


def require_pgpass_role(
    path: str | Path, role: str, *, dsns: Sequence[str] = ()
) -> None:
    """Refuse a generated identity that the declared passfile cannot use."""
    candidate = Path(path).expanduser()
    if pgpass_has_role(candidate, role, dsns=dsns):
        return
    raise PgIdentityCredentialError(
        f"PostgreSQL identity {role!r} has no credential in declared "
        f"PGPASSFILE {candidate}; provision the project role or declare a "
        "PGUSER whose credential exists before starting the agent"
    )


def derive_pg_role(
    agent_name: str,
    *,
    project_name: str | None = None,
    host_user: str | None = None,
) -> str:
    """Return the provisioned project role, with an agent-name fallback."""
    user = host_user or getpass.getuser()
    # metadata.labels.project is SAC's exact logical project identity.  It is
    # intentionally NOT compared with Python distribution metadata: variant
    # worktrees legitimately carry a different [project].name, and DB role
    # names are exact strings rather than normalized package names.
    principal = str(project_name or "").strip() or agent_name
    return f"{user}__{principal}"


def apply_pg_identity(
    env: Mapping[str, Any],
    *,
    raw_args: Iterable[Any] | None = None,
    agent_name: str | None = None,
    project_name: str | None = None,
    host_user: str | None = None,
) -> dict[str, str]:
    """Fill in ``PGUSER`` when absent everywhere. Returns a NEW dict.

    Declared-anywhere wins: a ``PGUSER`` in ``env`` (spec.env or a fleet
    default) or in ``raw_args`` ``--env`` form suppresses injection — the
    latter because apptainer appends ``raw_args`` after the rendered ``--env``
    flags and its ``--env`` is last-wins, so injecting here would LOOK
    overridden in the argv while this function believed it decided. Same
    reasoning as :func:`._board_identity_env.apply_board_identity_alias`.

    No ``agent_name`` -> no injection: a derived role must be derived from a
    real identity, and inventing one would put a WRONG login on every
    connection — worse than libpq's own loud fallback failure.
    """
    out: dict[str, str] = {str(k): str(v) for k, v in env.items()}
    declared_raw = raw_args_env(raw_args)
    if PG_USER_ENV in out or PG_USER_ENV in declared_raw:
        return out
    if not agent_name:
        return out
    role = derive_pg_role(
        str(agent_name),
        project_name=project_name,
        host_user=host_user,
    )
    logger.debug("pg_identity: injecting %s=%s", PG_USER_ENV, role)
    out[PG_USER_ENV] = role
    return out


__all__ = [
    "PG_USER_ENV",
    "PgIdentityCredentialError",
    "apply_pg_identity",
    "derive_pg_role",
    "pgpass_has_role",
    "require_pgpass_role",
]
