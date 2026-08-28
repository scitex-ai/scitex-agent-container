"""Fleet-default env layer — declare a variable ONCE for the WHOLE fleet.

Before this module the ONLY path from config to a container's environment was
``spec.env``: :func:`._apptainer_build_argv.build_run_argv` renders each
``spec.env`` pair as an ``apptainer --env K=V`` flag, and nothing else
contributes. There was NO baseline layer. So rolling ONE variable out to the
whole fleet meant editing N agent specs — and the specs live in the operator's
dotfiles repo, not in sac. A fleet-wide flag was therefore an N-file,
cross-repo change, which is why fleet-wide flags did not happen.

This module supplies the missing LOWEST-precedence layer.

Precedence (lowest → highest)
-----------------------------
1. :data:`FLEET_DEFAULT_ENV` — sac's declared baseline (this module).
2. ``config.yaml`` → ``spec.fleet_default_env`` — the operator's host-scope
   override, resolved through the same SciTeX local-state cascade
   :mod:`..config._host` already uses for ``spec.hostname_aliases``.
3. ``spec.env`` — the per-agent value. **ALWAYS WINS.**

Rule: the more specific layer wins, SILENTLY. This deliberately differs from
:func:`._layer_merge.deep_merge_layers`, which RAISES ``LayerMergeConflict``
when two ``to_home`` layers assign one key two different scalars. That raise is
correct there: those layers are peers, each key is owned by exactly one of
them, and a collision means somebody made a mistake. It is wrong here — a
DEFAULT exists precisely in order to be overridden, so a per-agent ``spec.env``
naming a fleet-default key is the feature, not an error. The idiom this module
follows is the ``.envrc`` cascade (:func:`._envrc.eval_envrc_cascade`: "a later
layer overrides an earlier one"), not the ``to_home`` conflict-raise.

An override is LOGGED (never silent to an operator reading the launch log), the
same visibility :func:`._mcp_merge.merge_mcp_docs` gives a per-agent server
entry that shadows the shared baseline.

Neutrality
----------
The defaults are DATA — a declared mapping. sac's LOGIC never names a consumer:
:func:`merge_fleet_env` merges whatever mapping it is handed, and would behave
identically if :data:`FLEET_DEFAULT_ENV` were empty. Adding or dropping a fleet
default is a one-line data edit here (or a ``config.yaml`` key), never a code
change. Nothing in this module branches on a specific variable name.

Opting out per agent
--------------------
A spec that must NOT receive a fleet default sets the same key in its own
``spec.env`` — to a different value, or to ``""`` to neutralise it. There is no
separate opt-out mechanism because per-agent precedence already is one.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping, MutableMapping

logger = logging.getLogger(__name__)

# ``config.yaml`` key (under ``spec:``) carrying the operator's host-scope
# fleet defaults. Same file + cascade as ``spec.hostname_aliases``.
CONFIG_SECTION = "fleet_default_env"

# The agent-name slot sac's OWN host-side process occupies in the per-agent
# PostgreSQL role scheme. It is not an agent and has no agent name, so it uses
# the reserved name ``cli`` — the cluster holds the role, and compute-04's
# ``.pgpass`` holds its rows. ONLY THE SLOT LIVES HERE: the role name is
# composed by :func:`._pg_identity_env.derive_pg_role`, which is the one place
# that knows the ``<host_user>__<name>`` shape, and the variable it lands in is
# that module's ``PG_USER_ENV``. See
# :func:`apply_fleet_defaults_to_process` for why the host process needs one.
HOST_PROCESS_AGENT_NAME = "cli"

# --------------------------------------------------------------------------
# THE DATA. Fleet-wide environment defaults, declared once, inherited by every
# agent that does not override the key in its own ``spec.env``.
#
# SCITEX_CARDS_READ_BACKEND (2026-07-18, the operator's YAML→DB ruling,
# requested by scitex-cards): pins the board's read path to the SQLite store.
#
# SCITEX_CARDS_DUAL_WRITE was injected alongside it to keep a SQLite mirror
# fresh next to a ~9 MB tasks YAML. That YAML tier was DELETED 2026-07-21, so
# from then on the flag gated nothing while still being injected into every
# container — and scitex-cards' ``health`` FAILS its ``single_write_target``
# check purely on its presence, so every agent reported an unhealthy store for
# a reason that no longer existed. A false alarm that cannot be cleared trains
# people to ignore the real one, and this exact flag shape (a stale toggle
# pointing at a dead store while every call returns success) is what silently
# lost a session of card writes once already. Dropped 2026-07-28 on the store
# owner's explicit decision. Do NOT reintroduce a write-routing flag here
# without an owner ruling — asserted by test_dead_write_routing_key_* in
# tests/scitex_agent_container/runtimes/test__fleet_env.py.
#
# SCITEX_CARDS_READ_BACKEND was dropped 2026-07-29 on the store owner's ruling,
# for the same reason as the dual-write flag above and with the same evidence
# standard — scitex-cards searched their READ PATH from source (positive control
# first: SCITEX_CARDS_DB, 73 hits across 32 files) and found the variable in
# exactly two places, a comment and a key in ``_RETIRED_VARS``. Neither is a
# read-for-behaviour. They also checked the expressive-range trap that bit
# several of us that day: the name is CONSTRUCTED (``prefix + suffix``) rather
# than written literally, so a plain grep could have missed a dynamic read — it
# is built for one purpose, ``environ.get(name)`` followed by ``logger.error``.
# The value is never returned, never branched on, never reaches the store.
#
# It was not merely useless, it was ACTIVELY MISLEADING. In scitex-cards' own
# words, from their retired-vars module: the board's systemd unit carried
# ``SCITEX_CARDS_READ_BACKEND=sqlite`` while the board served 0 cards from a
# database holding 2,654 — "it did nothing, but it STATED a read policy, so
# everyone diagnosing the outage read that line, concluded the read target was
# configured and correct, and looked elsewhere. An inert setting that appears to
# answer the question is worse than no setting at all: it spends the
# diagnostician's trust."
#
# scitex-cards KEEPS its retired-var warning deliberately — that warning is what
# makes a stale config say so out loud, and it is the guard against anyone
# hand-setting these again once the fleet is clean. So expect it to keep firing
# per agent until each restarts onto this cleaned environment; that is correct
# behaviour, not a leftover. Do NOT "fix" it by reintroducing the key.
#
# FLEET_DEFAULT_ENV was EMPTY between 2026-07-29 and 2026-08-19, and that was
# a valid state — the mechanism survives for operator overrides via
# ``config.yaml``'s ``fleet_default_env``. Asserted by
# test_an_empty_fleet_default_env_is_a_valid_state.
#
# SCITEX_STORE_DSN (2026-08-19, the operator's SQLite-eradication order:
# 「sqlite 根絶をしてください」「fail fast, fail loud, no fallbacks」).
#
# READ THE TWO PARAGRAPHS ABOVE BEFORE ADDING ANOTHER KEY HERE. Two store
# variables were declared here and later retired for STATING a routing policy
# nothing enforced, and one of them cost a live diagnosis: an inert setting
# that appears to answer the question spends the diagnostician's trust. So the
# bar for a third store variable is not "it seems right" — it is that the
# variable is demonstrably READ FOR BEHAVIOUR by its consumer.
#
# THAT BAR IS MET, AND MEASURED IN BOTH ARMS rather than argued:
#
#     with SCITEX_STORE_DSN set     scitex_dev.store.host_store() resolves to
#                                   the injected DSN; Store opens; a put/get
#                                   round trip returns typed values, and the
#                                   row is visible in Postgres through a
#                                   SECOND, INDEPENDENT client (raw psycopg,
#                                   plain SQL) rather than through the library
#                                   that chose the backend
#     with it UNSET                 host_store() resolves to a UNIX socket and
#                                   Store() raises StoreTargetError naming the
#                                   missing socket path
#
# The unset arm is the point: the failure is LOUD and there is no SQLite path
# to slip into. That is scitex-dev's design, in their own words at
# ``resolve_target``: "deliberately no SQLite fallback ... a host whose
# Postgres is down must fail loudly rather than start writing to a private
# local file that shares nothing."
#
# test_store_dsn_is_read_for_behaviour_by_its_consumer asserts BOTH arms, so
# if scitex-dev ever stops honouring the variable, sac goes RED here instead
# of quietly injecting an inert string into every container for months. That
# test is the guard the two retired variables never had.
#
# WHY THE DEFAULT IS NEEDED AT ALL — sac containers cannot use scitex-dev's
# own default. ``host_store`` derives a socket path from ``$HOME`` and assumes
# PostgreSQL's default port, giving
# ``/home/agent/.scitex/pg/.s.PGSQL.5432`` inside a container. Two things are
# wrong with that here, and only the second is sac's fault:
#
#   * the fleet's Postgres listens on 55432, and the port is part of the
#     SOCKET FILENAME, so the socket that exists
#     (``~/.scitex/pg/run/.s.PGSQL.55432``) can never be found by a resolver
#     that has no port parameter. Reported to scitex-dev.
#   * ``$HOME`` inside the container is ``/home/agent``, not the operator's
#     home where the socket actually lives.
#
# TCP RATHER THAN THE SOCKET, deliberately: this mirrors SCITEX_CARDS_DB,
# which is the DSN shape already proven fleet-wide in production.
#
# CORRECTED 2026-08-28 — THE 08-19 VALUE WAS RIGHT WHEN WRITTEN AND THE WORLD
# MOVED UNDER IT. It was ``postgresql://scitex_cards@127.0.0.1:55432/scitex``,
# justified here by a ``.pgpass`` entry wildcarding the database
# (``127.0.0.1:55432:*:scitex_cards``). BOTH halves of that justification have
# since expired, and nothing re-checked:
#
#   * ``127.0.0.1`` was this host's own PRIMARY on 08-19. The compute hosts are
#     now STREAMING STANDBYS of nas-03, so the local instance answers
#     ``pg_is_in_recovery() = t`` and refuses every write with "cannot execute
#     ... in a read-only transaction". MEASURED on compute-04, 2026-08-28.
#   * the ``scitex_cards`` pgpass row is GONE — compute-04 holds ZERO entries
#     for that role, so the credential the comment relied on does not exist.
#
# COST: no agent birth was recorded fleet-wide between 2026-08-23 and
# 2026-08-28. The launches happened; their RECORD failed. That history is not
# delayed, it is gone.
#
# THE CORRECTED VALUE follows the working neighbour EXACTLY rather than
# approximately — that is the whole lesson. SCITEX_CARDS_DB names the PRIMARY
# by name and OMITS the user, letting ``PGUSER=ywatanabe__<agent>`` supply the
# identity that ``pg_hba`` already admits over the overlay
# (``host scitex +ywatanabe 100.64.0.0/10 scram-sha-256``). The old value
# diverged on both axes at once — wrong host AND a hardcoded role — which is
# why cards kept working all week while state writes died beside it.
#
# PROVEN END TO END from compute-04 before this edit, as the agent's own role:
# connect -> current_user=ywatanabe__scitex-agent-container,
# pg_is_in_recovery()=f (the PRIMARY), then a scratch table created, one row
# inserted, that row read back, and the table dropped. A full write cycle, not
# a connection test — the failure being fixed is precisely "connects fine,
# cannot write".
#
# (Those five steps are described rather than quoted on purpose: the
# sqlite-footprint guard flags any module carrying table-definition DDL, and it
# reads a COMMENT the same way it reads code. It flagged this file when the
# statements were written out literally, which is the guard being right — a
# module that declares no tables should not contain the words for declaring
# one.)
#
# HOST-SPECIFIC OVERRIDES ARE THE OPERATOR'S LAYER, not a reason to compute
# this value: a host whose Postgres is elsewhere sets
# ``spec.fleet_default_env`` in ``config.yaml``, and a single agent that must
# not receive it sets the key in its own ``spec.env``. A host with no
# Postgres at all gets a loud refusal, which is the requested behaviour.
#
# These are opaque strings to sac. It never reads, parses or branches on them.
# --------------------------------------------------------------------------
FLEET_DEFAULT_ENV: dict[str, str] = {
    "SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex",
}


def _config_path() -> Path:
    """Resolve ``config.yaml`` via the SciTeX local-state cascade.

    Mirrors :func:`..config._host._config_path` — project scope wins over user
    scope. Imported lazily so a missing ``scitex_config`` degrades to "no
    operator overrides" instead of breaking every container launch.
    """
    from scitex_config._ecosystem import local_state as _local_state

    return _local_state.path("agent-container", "config.yaml")


def _operator_overrides(config_path: Path | None = None) -> dict[str, str]:
    """Read ``spec.fleet_default_env`` from ``config.yaml``.

    Returns ``{}`` when the file is absent, unparseable, lacks the section, or
    the section is not a mapping. NEVER raises: an operator typo in an optional
    config file must not stop the fleet from launching — it degrades to sac's
    declared baseline, which is the state that existed before this layer.

    Keys and values are coerced to ``str`` so a YAML ``true`` / ``1`` renders
    as a well-formed ``--env`` value rather than a Python repr.
    """
    try:
        path = config_path if config_path is not None else _config_path()
    except Exception as exc:  # stx-allow: fallback (reason: local-state resolution must never break launch; fall back to the declared baseline)
        logger.debug("fleet_env: could not resolve config.yaml path (%s)", exc)
        return {}
    if not path.exists():
        return {}
    try:
        import yaml  # PyYAML ships with the container; same import sac uses.

        data = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:  # stx-allow: fallback (reason: malformed/unreadable optional config must not break launch)
        logger.warning(
            "fleet_env: could not read %s (%s) — using sac's declared defaults",
            path,
            exc,
        )
        return {}
    section = (data.get("spec") or {}).get(CONFIG_SECTION) or {}
    if not isinstance(section, dict):
        logger.warning(
            "fleet_env: spec.%s in %s is %s, not a mapping — ignored",
            CONFIG_SECTION,
            path,
            type(section).__name__,
        )
        return {}
    return {str(k): str(v) for k, v in section.items()}


def declared_fleet_defaults(config_path: Path | None = None) -> dict[str, str]:
    """The fleet-default env after layer 1 + layer 2 (before ``spec.env``).

    :data:`FLEET_DEFAULT_ENV` overlaid by the operator's
    ``config.yaml`` ``spec.fleet_default_env``, which may both override a
    declared default and add new keys. ``config_path`` is an injection seam for
    tests (a real YAML file on tmp_path — no mocks); production passes ``None``
    and the local-state cascade resolves it.
    """
    merged = dict(FLEET_DEFAULT_ENV)
    for key, val in _operator_overrides(config_path).items():
        if key in merged and merged[key] != val:
            logger.info(
                "fleet_env: config.yaml spec.%s overrides sac default %s (%r -> %r)",
                CONFIG_SECTION,
                key,
                merged[key],
                val,
            )
        merged[key] = val
    return merged


def merge_fleet_env(
    spec_env: Mapping[str, Any] | None,
    *,
    defaults: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Merge the fleet defaults UNDER ``spec_env``. Per-agent always wins.

    ``defaults`` is an injection seam; ``None`` resolves
    :func:`declared_fleet_defaults`. Pure apart from that resolution — it does
    not mutate ``spec_env`` and is idempotent.

    A ``spec_env`` key that shadows a fleet default is logged at INFO so the
    override is visible in the launch log rather than silently effective.
    """
    resolved = declared_fleet_defaults() if defaults is None else dict(defaults)
    merged: dict[str, str] = {str(k): str(v) for k, v in resolved.items()}
    for key, val in (spec_env or {}).items():
        skey, sval = str(key), str(val)
        if skey in merged and merged[skey] != sval:
            logger.info(
                "fleet_env: spec.env overrides fleet default %s (%r -> %r)",
                skey,
                merged[skey],
                sval,
            )
        merged[skey] = sval
    return merged


def effective_env(
    config: Any,
    *,
    defaults: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The env an agent's container should launch with.

    The entry-point :func:`._apptainer_build_argv.build_run_argv` calls in
    place of iterating ``config.env`` directly. Returns fleet defaults merged
    under ``config.env`` (``spec.env`` wins), then the board-identity alias
    (:mod:`._board_identity_env`) is applied so a value declared under either
    ``SCITEX_TODO_AGENT_ID`` or ``SCITEX_CARDS_AGENT_ID`` — in ``spec.env`` or
    in ``apptainer.raw_args`` — is mirrored onto BOTH names for the
    scitex-cards rename transition window (INCIDENT 2026-07-19: cards were
    written with the literal, unexpanded ``${SCITEX_CARDS_AGENT_ID}`` as
    their author because sac injected only the old name — 7 rows when first
    reported, 15 a few hours later, since every new card added one).

    Every value — not just the identity vars — is validated: an env value
    that still looks like an unexpanded ``${VAR}`` substitution raises
    :class:`._board_identity_env.UnexpandedEnvValueError` here rather than
    being stored three layers downstream in someone else's database.
    """
    from ._board_identity_env import apply_board_identity_alias
    from ._pg_identity_env import apply_pg_identity

    merged = merge_fleet_env(getattr(config, "env", None), defaults=defaults)
    apptainer = getattr(config, "apptainer", None)
    raw_args = getattr(apptainer, "raw_args", None) if apptainer is not None else None
    # The agent's own name is passed so a spec that declares NEITHER identity
    # spelling still launches with one. See the branch in
    # ``apply_board_identity_alias`` for why the name is the right answer and
    # why it cannot override a deliberate alias.
    aliased = apply_board_identity_alias(
        merged, raw_args=raw_args, agent_name=getattr(config, "name", None)
    )
    # Same shape for the PostgreSQL login: ``PGUSER=<host_user>__<name>``
    # unless a spec declares its own (b2 of the pg55432 role rework — see
    # ``_pg_identity_env`` for why specs stopped carrying DSN userinfo).
    return apply_pg_identity(
        aliased, raw_args=raw_args, agent_name=getattr(config, "name", None)
    )


def fleet_env_flags(
    config: Any,
    *,
    defaults: Mapping[str, str] | None = None,
) -> list[str]:
    """:func:`effective_env` rendered as apptainer ``--env K=V`` flags.

    Mirrors :func:`._mcp_reliability.mcp_timeout_env_flags` so every module
    that contributes ``--env`` flags exposes the same shape.
    """
    return [
        flag
        for key, val in effective_env(config, defaults=defaults).items()
        for flag in ("--env", f"{key}={val}")
    ]


def apply_fleet_defaults_to_process(
    environ: "MutableMapping[str, str] | None" = None,
    *,
    config_path: Path | None = None,
) -> dict[str, str]:
    """Give sac's OWN process the fleet defaults its containers get.

    Everything above renders the defaults into an agent's ``apptainer --env``
    flags and stops there. But the host-side ``sac`` process opens the SAME
    stores the containers do — ``persist_acl_policy`` on every start,
    instances, dispatches, the diary — and it received none of them. On a
    shell with no ``SCITEX_STORE_DSN``, ``scitex_dev.store.host_store()`` fell
    through to its UNIX-socket fallback, ``pg_hba`` asked the socket for a
    password, and ``.pgpass`` — keyed by ``127.0.0.1`` / ``localhost`` /
    ``scitex-primary``, never by a socket directory — had nothing to offer:

        Cannot connect to Postgres store 'node_comms_policy' ...
        socket "~/.scitex/pg/run/.s.PGSQL.55432" failed:
        fe_sendauth: no password supplied

    MEASURED on compute-04, 2026-08-28, on ``sac agents restart``. A
    ``.pgpass`` row for the socket directory is the WRONG fix: that socket is
    the local cluster, which is now a streaming STANDBY
    (``pg_is_in_recovery() = t``), so the row would only trade a loud connect
    refusal for a quiet read-only write failure. The right store is the one
    :data:`FLEET_DEFAULT_ENV` already names — the gap was that only the
    containers were told.

    Same precedence as everything else in this module: a key already present
    in ``environ`` wins, untouched — a default exists in order to be
    overridden, and an operator who exported ``SCITEX_STORE_DSN`` in their
    shell has overridden it. ``environ`` defaults to ``os.environ`` and is an
    injection seam for tests. Returns the keys it actually set.

    ``PGUSER`` — THE OTHER HALF OF THE SAME IDENTITY (2026-08-28)
    ------------------------------------------------------------
    The DSN above is ROLELESS on purpose. ``scitex-primary:55432/scitex``
    names a host, a port and a database and NO user, because in this fleet the
    login identity is per-agent and travels in a SECOND variable: containers
    receive the roleless DSN *and* ``PGUSER=<host_user>__<agent>``, injected by
    :mod:`._pg_identity_env` (see that module for why specs stopped carrying
    DSN userinfo at all). Two halves, one scheme.

    Giving the host process only the FIRST half fixed which server it talked
    to and left it unable to say who it was:

        Cannot connect to Postgres store 'node_comms_policy' ...
        connection to server at "100.64.0.5", port 55432 failed:
        fe_sendauth: no password supplied

    With no ``PGUSER``, libpq falls back to the OS user — bare ``ywatanabe``
    — and ``.pgpass`` matches on (host, port, database, USER). compute-04's
    file holds 522 rows and NOT ONE names the bare OS user: every row is a
    service role or a per-agent ``<host_user>__<agent>``. So the OS-user
    fallback can never authenticate, whichever host the DSN names — the
    password lookup fails before the server is ever asked. MEASURED on
    compute-04 2026-08-28 against this branch: with the DSN alone, the read
    above; with ``PGUSER=ywatanabe__cli`` beside it, the same read returns
    the policy. COST while it was missing: every ``sac agents start`` that
    publishes ACL policy died, taking scitex-hub (compute-03) and business
    (compute-01) down.

    THE ROLE IS DELIBERATELY NOT PUT IN THE DSN, and that constraint is the
    whole reason this lives here rather than in :data:`FLEET_DEFAULT_ENV`.
    That mapping is the CONTAINERS' baseline too, so userinfo in its DSN — or
    a ``PGUSER`` key in it — would reach all 132 agent containers and override
    every per-agent role, collapsing 132 distinct logins into one shared
    identity and taking the per-agent audit trail and the ``pg_hba`` grant
    structure with it. The host process gets its own half of the pair
    injected HERE, in the one function nothing container-bound calls.

    ``cli`` (:data:`HOST_PROCESS_AGENT_NAME`) is the agent-name slot; the
    composition is :func:`._pg_identity_env.derive_pg_role`'s, so exactly one
    module knows the ``<host_user>__<name>`` shape and the libpq variable
    name. ``ywatanabe__cli`` is verified to authenticate against
    scitex-primary from compute-01, compute-03, compute-04 and nas-03.
    compute-02 was NOT tested — it carries neither psycopg nor any agent
    specs — and is no worse off than before, where it had no host-side login
    either.

    Declared-anywhere wins here too, and the check runs AFTER the cascade
    above so all three declaring layers are covered by one lookup: a
    ``PGUSER`` exported in the operator's shell, one in
    :data:`FLEET_DEFAULT_ENV`, and one in ``config.yaml``'s
    ``spec.fleet_default_env`` each land in ``target`` first and suppress the
    injection.
    """
    import os

    from ._pg_identity_env import PG_USER_ENV, derive_pg_role

    target = os.environ if environ is None else environ
    injected: dict[str, str] = {}
    for key, val in declared_fleet_defaults(config_path).items():
        if key in target:
            continue
        target[key] = val
        injected[key] = val
    if PG_USER_ENV not in target:
        role = derive_pg_role(HOST_PROCESS_AGENT_NAME)
        target[PG_USER_ENV] = role
        injected[PG_USER_ENV] = role
    if injected:
        logger.debug("fleet_env: process env gained %s", sorted(injected))
    return injected


__all__ = [
    "CONFIG_SECTION",
    "FLEET_DEFAULT_ENV",
    "HOST_PROCESS_AGENT_NAME",
    "apply_fleet_defaults_to_process",
    "declared_fleet_defaults",
    "effective_env",
    "fleet_env_flags",
    "merge_fleet_env",
]
