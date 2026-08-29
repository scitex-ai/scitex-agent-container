"""One ``--env KEY`` per key, and never a scitex DSN on port 5432.

Why this module exists
----------------------
``build_run_argv`` collects ``--env`` flags from several independent
layers. Two of them can legitimately name the SAME key:

* the fleet/spec env layer — ``_fleet_env.effective_env`` (sac's
  declared defaults, the operator's ``config.yaml``
  ``spec.fleet_default_env``, then ``spec.env``), rendered early; and
* ``spec.apptainer.raw_args`` — the §1 escape hatch, appended verbatim
  and therefore LATE.

Nothing reconciled them. The argv simply carried the key twice and the
launch was correct only because apptainer's ``--env`` is last-wins, so
the later ``raw_args`` occurrence masked the earlier one.

Measured on scitex-compute-04 (2026-08-11), the fleet was running on
exactly that accident::

    --env SCITEX_CARDS_DB=postgresql://…@127.0.0.1:5432/scitex_cards
    …
    --env SCITEX_CARDS_DB=postgresql://…@127.0.0.1:55432/scitex_cards

The agent resolved to ``:55432`` — the right database — purely because
that occurrence sorted later in the list. Reorder the assembly for any
unrelated reason and every agent on the host silently starts writing
its cards somewhere else. Correctness that depends on argv ordering is
not correctness, so this module resolves the duplicate BEFORE apptainer
ever sees it: :func:`collapse_duplicate_env` leaves exactly one
occurrence per key, and which one wins is a decision made here, in
sac, with a log line naming it.

The winner is still the LAST occurrence. That is deliberate: it is the
value the fleet runs on today, and it is the same precedence
``_board_identity_env.apply_board_identity_alias`` already documents
and relies on (``raw_args`` overrides ``spec.env``). This module makes
that rule explicit and structural rather than positional and implicit
— it does not change which value an agent receives.

The port rule
-------------
ADR-0022 (``docs/adr/0022-state-in-postgres-configuration-in-git.md``)
rules that **port 5432 is never used for scitex** — the containerised
PostgreSQL runs on ``55432`` on every node. A scitex DSN pointing at
5432 is therefore always wrong, and "always wrong" deserves a check
rather than a comment: :func:`assert_no_forbidden_scitex_dsn` refuses
to launch rather than handing a container a store address that no peer
reads. A DSN with no port at all is caught too, because an omitted
port IS 5432 — the most invisible way to hit the same wall.

Both passes are pure functions over the flag argv (a new list is
returned; the input is never mutated) and neither ever logs an env
VALUE — a DSN can carry a password, and this argv is world-readable
until :mod:`._apptainer_secret_env` lifts secrets out of it.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: apptainer's env flag, in both spellings that occur in real specs.
_ENV_FLAG = "--env"
_ENV_GLUED_PREFIX = "--env="

#: The port ADR-0022 rules out for every scitex service, on every host.
FORBIDDEN_SCITEX_PG_PORT = 5432

#: The port the containerised PostgreSQL actually listens on.
CANONICAL_SCITEX_PG_PORT = 55432

_PG_SCHEMES = frozenset({"postgres", "postgresql"})


class ForbiddenScitexDsnError(ValueError):
    """Raised when a built argv would hand a container a scitex DSN on 5432.

    Not a style complaint: nothing listens on 5432 on a scitex node, so
    the agent would either fail its first card access with an opaque
    connection error, or — worse, if something ever does listen there —
    write its cards into a database no peer reads.
    """


def env_pair_at(argv: list[str], index: int) -> tuple[str, str, int] | None:
    """``(key, value, width)`` for the ``--env`` pair starting at ``index``.

    ``width`` is how many argv tokens the pair occupies (2 for the split
    spelling, 1 for the glued one). Returns ``None`` when ``index`` does
    not start a well-formed ``--env`` pair — including a bare trailing
    ``--env`` and a value with no ``=``, both of which are left exactly
    where they are for :mod:`._apptainer_argv_guard` to report.

    ``--env-file`` is NOT an ``--env`` pair and never matches here.

    THE SINGLE RECOGNISER, and public for that reason. Every pass that
    walks this argv asking "is this an ``--env`` pair, and which one?"
    must ask HERE — because a pass that answers it independently answers
    it DIFFERENTLY, and the fleet has already paid for that:
    :func:`._apptainer_secret_env.redact_secret_env_to_file` used to
    match only the split spelling, so a spec writing the glued
    ``--env=ANTHROPIC_API_KEY=…`` (a form live in real specs — see
    ``raw_args`` across the fleet) walked straight past the secret sweep
    and into the world-readable launcher argv. Two modules disagreeing
    about what counts as the same flag IS the vulnerability; sharing this
    function is what makes them unable to disagree.
    """
    token = argv[index]
    if token == _ENV_FLAG and index + 1 < len(argv):
        pair = argv[index + 1]
        width = 2
    elif token.startswith(_ENV_GLUED_PREFIX):
        pair = token[len(_ENV_GLUED_PREFIX) :]
        width = 1
    else:
        return None
    key, sep, value = pair.partition("=")
    if not sep or not key:
        return None
    return key, value, width


def _env_pair_spans(argv: list[str]) -> list[tuple[int, int, str, str]]:
    """Every ``--env`` pair as ``(start, width, key, value)``, in argv order."""
    spans: list[tuple[int, int, str, str]] = []
    index = 0
    while index < len(argv):
        found = env_pair_at(argv, index)
        if found is None:
            index += 1
            continue
        key, value, width = found
        spans.append((index, width, key, value))
        index += width
    return spans


def collapse_duplicate_env(
    argv: list[str],
    *,
    agent: str | None = None,
) -> list[str]:
    """Return ``argv`` with each ``--env KEY`` appearing exactly once.

    When a key is declared more than once the LAST occurrence is kept —
    the same value apptainer would have resolved — and the earlier ones
    are dropped, so the argv handed to apptainer can no longer depend on
    flag ordering to be correct. Every other token keeps its position.

    Pure: a NEW list is returned and the input is not mutated. A no-op
    (a fresh copy) when no key is declared twice.

    Call this AFTER every ``--env`` contributor — including
    ``spec.apptainer.raw_args`` — and BEFORE the SIF is appended, so the
    list it walks is the flag region only.

    Only KEY names are logged. A value may be a DSN or a token, and this
    argv is world-readable until the secret sweep lifts it out.
    """
    spans = _env_pair_spans(argv)
    last_start: dict[str, int] = {}
    for start, _width, key, _value in spans:
        last_start[key] = start

    superseded = {
        start
        for start, _width, key, _value in spans
        if last_start[key] != start
    }
    if not superseded:
        return list(argv)

    dropped: dict[str, int] = {}
    kept: list[str] = []
    index = 0
    while index < len(argv):
        found = env_pair_at(argv, index)
        if found is None:
            kept.append(argv[index])
            index += 1
            continue
        key, _value, width = found
        if index in superseded:
            dropped[key] = dropped.get(key, 0) + 1
            index += width
            continue
        kept.extend(argv[index : index + width])
        index += width

    who = f" for agent {agent!r}" if agent else ""
    for key in sorted(dropped):
        logger.info(
            "apptainer argv%s declared --env %s %d extra time(s); kept the "
            "LAST declaration and dropped the earlier one(s). Two layers "
            "name this key — spec.apptainer.raw_args wins over the "
            "fleet/spec env layer, as it already did via apptainer's "
            "last-wins rule.",
            who,
            key,
            dropped[key],
        )
    return kept


def _forbidden_scitex_dsn(key: str, value: str) -> str | None:
    """Why ``KEY=VALUE`` is a banned scitex DSN, or ``None`` if it is fine.

    A value qualifies when it parses as a ``postgres(ql)://`` URL that
    belongs to scitex — by variable name, connecting user, or database
    name — and resolves to :data:`FORBIDDEN_SCITEX_PG_PORT`, whether the
    port is written out or merely defaulted to by omission.
    """
    if "://" not in value:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in _PG_SCHEMES:
        return None
    try:
        port = parts.port
    except ValueError:
        # An unparseable port is a different fault; the connection will
        # name it. Do not guess.
        return None
    database = (parts.path or "").lstrip("/")
    is_scitex = (
        key.upper().startswith("SCITEX_")
        or "scitex" in (parts.username or "").lower()
        or "scitex" in database.lower()
    )
    if not is_scitex:
        return None
    if port is None:
        return (
            "names no port, so it resolves to PostgreSQL's default "
            f"{FORBIDDEN_SCITEX_PG_PORT}"
        )
    if port == FORBIDDEN_SCITEX_PG_PORT:
        return f"names port {FORBIDDEN_SCITEX_PG_PORT}"
    return None


def assert_no_forbidden_scitex_dsn(
    argv: list[str],
    *,
    agent: str | None = None,
) -> None:
    """Refuse an argv carrying a scitex PostgreSQL DSN on port 5432.

    Raises :class:`ForbiddenScitexDsnError` naming the offending
    variable, where such a DSN comes from, and the canonical port. The
    DSN itself is NOT included in the message — it can carry a password,
    and this message reaches logs.

    A no-op for every other argv, including SQLite ``SCITEX_*_DB``
    paths and non-scitex PostgreSQL URLs.
    """
    for _start, _width, key, value in _env_pair_spans(argv):
        why = _forbidden_scitex_dsn(key, value)
        if why is None:
            continue
        who = f" for agent {agent!r}" if agent else ""
        raise ForbiddenScitexDsnError(
            f"--env {key}{who} is a scitex PostgreSQL DSN that {why}. "
            f"Port {FORBIDDEN_SCITEX_PG_PORT} is never used for scitex on "
            "any host (ADR-0022); the containerised PostgreSQL listens on "
            f"{CANONICAL_SCITEX_PG_PORT} on every node. Launching would "
            "point this agent's store at an address no peer reads.\n"
            f"  Fix the {key} declaration in the agent's spec "
            "(spec.env / spec.apptainer.raw_args) or in the operator's "
            "config.yaml spec.fleet_default_env — whichever names it. "
            "The DSN value is withheld here because it may carry a "
            "credential."
        )


__all__ = [
    "CANONICAL_SCITEX_PG_PORT",
    "FORBIDDEN_SCITEX_PG_PORT",
    "ForbiddenScitexDsnError",
    "assert_no_forbidden_scitex_dsn",
    "collapse_duplicate_env",
    "env_pair_at",
]
