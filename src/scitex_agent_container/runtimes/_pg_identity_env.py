"""Per-agent PostgreSQL identity — inject ``PGUSER`` when no layer declares it.

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
agent's own name at launch, injected only when NOTHING else declares it.
116 specs therefore need no per-spec ``PGUSER`` line (D15: generate, don't
hardcode), and a spec that DOES declare one — in ``spec.env`` or in
``apptainer.raw_args`` — always wins, silently, because a default exists in
order to be overridden (:mod:`._fleet_env`'s own rule).

The derived name is ``<host_user>__<agent_name>``. ``host_user`` is the OS
user launching the container (``getpass.getuser()``), not a constant: the
role tree is per-owner (``ywatanabe`` today, anyone else the day the fleet
gains a second human), and sac's neutrality rule — logic never names a
consumer — holds.
"""

from __future__ import annotations

import getpass
import logging
from typing import Any, Iterable, Mapping

from ._board_identity_env import raw_args_env

logger = logging.getLogger(__name__)

# The libpq user variable. Everything speaking to PostgreSQL through libpq or
# psycopg honours it; nothing else in the fleet uses the name.
PG_USER_ENV = "PGUSER"


def derive_pg_role(agent_name: str, *, host_user: str | None = None) -> str:
    """The cluster role an agent authenticates as: ``<host_user>__<agent>``."""
    user = host_user or getpass.getuser()
    return f"{user}__{agent_name}"


def apply_pg_identity(
    env: Mapping[str, Any],
    *,
    raw_args: Iterable[Any] | None = None,
    agent_name: str | None = None,
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
    if PG_USER_ENV in out:
        return out
    if PG_USER_ENV in raw_args_env(raw_args):
        return out
    if not agent_name:
        return out
    role = derive_pg_role(str(agent_name), host_user=host_user)
    logger.debug("pg_identity: injecting %s=%s", PG_USER_ENV, role)
    out[PG_USER_ENV] = role
    return out


__all__ = [
    "PG_USER_ENV",
    "apply_pg_identity",
    "derive_pg_role",
]
