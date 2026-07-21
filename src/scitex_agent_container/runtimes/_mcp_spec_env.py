"""Durable spec-env delivery for MCP server configs (P1, card
sac-env-injection-lost-on-mcp-reconnect-20260721).

THE MECHANISM THIS MODULE CLOSES
================================
sac delivers ``spec.env`` (merged with the fleet defaults —
:func:`._fleet_env.effective_env`) to an agent EXCLUSIVELY as process
environment: :func:`._apptainer_build_argv.build_run_argv` renders each pair
as an apptainer ``--env`` flag, the container process tree inherits it, and
the FIRST spawn of every stdio MCP server receives it by plain parent-process
inheritance from the ``claude`` client.

That inheritance is a VOLATILE channel. The MCP stdio transport Claude Code
bundles spawns a child with ``{...getDefaultEnvironment(), ...config.env}``,
where ``getDefaultEnvironment()`` passes ONLY a sanitized allowlist
(``HOME``/``LOGNAME``/``PATH``/``SHELL``/``TERM``/``USER`` on POSIX — read
out of the deployed claude 2.1.216 binary), and Claude Code's own spawn
wrapper has an allowlist branch with the same shape. A mid-session RESPAWN
(the ``/mcp`` reconnect) that takes any sanitized path therefore delivers
only the config-declared ``env`` block — everything sac injected at boot is
gone, while plain-shell (Bash tool) children still show it. Live symptom
(2026-07-21): scitex-cards' resolve-store flipped to a different store
mid-session and ``store_identity`` mismatch errors appeared.

The one channel that reaches the child on EVERY spawn path — first spawn or
reconnect respawn, inheriting or sanitized — is the server entry's ``env``
block: both spawn shapes spread it LAST. So the fix is to make the spec env
DURABLE there: bake the resolved literal values into each stdio server
entry's ``env`` at config-build time, where the values are still known.

HOW THE VALUES TRAVEL
=====================
The bake must happen IN-CONTAINER (the SDK options builder), because the
container env — not the host launch shell — is the authoritative expansion
context (INCIDENT 2026-07-02: host-side baking stamped the WRONG identity
into a materialized ``.mcp.json``). But in-container nothing can tell a
spec-injected var from ambient env. So the launch argv carries the KEY LIST:
:func:`spec_env_keys_flag` appends ``--env SAC_SPEC_ENV_KEYS=<k1,k2,...>``
next to the value flags, and :func:`resolve_spec_env` reads that list back
from the live environ and materializes ``{key: value}`` from the SAME
environment the first spawn inherited — what is baked can never disagree
with what the container actually launched with.

FAIL-LOUD CONTRACT
==================
A key named by ``SAC_SPEC_ENV_KEYS`` that is ABSENT from the environ means
the launch-time injection contract broke; continuing would silently rebuild
the exact invisible loss this module exists to close, so
:class:`SpecEnvUnresolvedError` is raised naming the key. Every baked value
is validated with :func:`._board_identity_env.assert_expanded` — a value
that still looks like ``${VAR}`` is a substitution that never happened
(INCIDENT 2026-07-19: literal ``${SCITEX_CARDS_AGENT_ID}`` stored as a card
author) and must fail where it is built. An UNSET/EMPTY ``SAC_SPEC_ENV_KEYS``
is a clean no-op: a container launched by an older sac must keep booting
(rolling-deploy safety), it merely keeps today's volatile-only behaviour.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ._board_identity_env import assert_expanded

logger = logging.getLogger(__name__)

#: Env var carrying the comma-separated spec-env KEY LIST into the container.
#: Values are NOT carried here — they ride their own ``--env`` flags; this is
#: only the manifest that lets the in-container baker tell spec env apart
#: from ambient env.
SPEC_ENV_KEYS_VAR = "SAC_SPEC_ENV_KEYS"

#: Server ``type`` values whose children are spawned as stdio subprocesses —
#: the only kind whose spawn env the bake can influence. An entry with no
#: ``type`` defaults to stdio (matching Claude Code / the SDK).
_STDIO_TYPES = frozenset({"stdio"})


class SpecEnvUnresolvedError(RuntimeError):
    """A spec-env key promised by ``SAC_SPEC_ENV_KEYS`` is absent at bake time.

    Raised where the config is built, so a broken injection contract stops
    the launch instead of shipping an MCP config whose respawned servers
    silently lose the agent's store/identity env mid-session.
    """


def spec_env_keys_flag(agent_env: Mapping[str, Any] | None) -> list[str]:
    """Render the ``--env SAC_SPEC_ENV_KEYS=...`` manifest flag.

    ``agent_env`` is the SAME mapping the caller just rendered as ``--env``
    value flags (:func:`._fleet_env.effective_env`), so the manifest can
    never name a key that was not actually injected. Returns ``[]`` when the
    agent declares no env (nothing to make durable). Keys are sorted for a
    deterministic argv (stable across launches → diffable launch records).
    """
    if not agent_env:
        return []
    keys = sorted(str(k) for k in agent_env)
    return ["--env", f"{SPEC_ENV_KEYS_VAR}={','.join(keys)}"]


def resolve_spec_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Materialize the spec env ``{key: value}`` from the live ``environ``.

    Reads the :data:`SPEC_ENV_KEYS_VAR` manifest and resolves each named key
    against ``environ`` — the same environment the first spawn inherited, so
    the baked values reproduce first-spawn semantics exactly.

    Returns ``{}`` when the manifest is unset or empty (pre-manifest launch:
    no-op, rolling-deploy safe). Raises :class:`SpecEnvUnresolvedError` when
    a named key is absent, and
    :class:`._board_identity_env.UnexpandedEnvValueError` when a value still
    carries an unexpanded ``${VAR}`` substitution.
    """
    manifest = (environ.get(SPEC_ENV_KEYS_VAR) or "").strip()
    if not manifest:
        return {}
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for key in (k.strip() for k in manifest.split(",")):
        if not key:
            continue
        if key not in environ:
            missing.append(key)
            continue
        value = environ[key]
        assert_expanded(key, value)
        resolved[key] = value
    if missing:
        agent = environ.get("SAC_NAME") or environ.get(
            "SCITEX_AGENT_CONTAINER_NAME", "<unknown>"
        )
        raise SpecEnvUnresolvedError(
            f"agent '{agent}': {SPEC_ENV_KEYS_VAR} names spec-env key(s) "
            f"absent from the environment: {', '.join(sorted(missing))}. The "
            "launch promised these vars (they were listed at `--env` "
            "injection time), so their absence means the env-injection "
            "contract broke — something between launch and here unset them. "
            "Refusing to build an MCP config without them: a respawned "
            "(reconnected) MCP server only receives env baked into its config "
            "entry, so continuing would silently rebuild the mid-session "
            "store/identity loss this bake exists to close (card "
            "sac-env-injection-lost-on-mcp-reconnect-20260721). Fix: check "
            f"`spec.env` in this agent's YAML for the key(s) named above and "
            "restart the agent; if the spec is correct, find what unset the "
            "var inside the container (.envrc / a wrapper) — or, to launch "
            "degraded anyway, remove the key from spec.env."
        )
    return resolved


def bake_spec_env_values(
    servers: Mapping[str, Any] | None,
    spec_env: Mapping[str, str] | None,
) -> None:
    """Bake ``spec_env`` literals into every stdio entry's ``env`` block.

    Mutates the entries of ``servers`` in place — this runs at the single
    chokepoint where the config the RESPAWNER reads is assembled, so every
    spawn (first or reconnect) receives the same spec env.

    Precedence: an entry-declared ``env`` key always WINS (``setdefault``),
    matching the spawn-time ``{...inherited, ...entry.env}`` order where the
    declared block overrides inheritance. Non-dict entries and non-stdio
    transports (http/sse — no child process to env) are left untouched.
    """
    if not servers or not spec_env:
        return
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        entry_type = str(entry.get("type") or "stdio").lower()
        if entry_type not in _STDIO_TYPES:
            continue
        env_block = entry.get("env")
        if not isinstance(env_block, dict):
            env_block = {}
            entry["env"] = env_block
        added = [k for k in spec_env if k not in env_block]
        for key in added:
            env_block[key] = spec_env[key]
        if added:
            logger.debug(
                "mcp spec-env bake: server '%s' env += %s (declared keys win)",
                name,
                ", ".join(sorted(added)),
            )


def bake_spec_env_into_servers(
    servers: Mapping[str, Any] | None,
    environ: Mapping[str, str],
) -> None:
    """Container-side bake: resolve the manifest, then bake the values.

    The composition :func:`resolve_spec_env` → :func:`bake_spec_env_values`
    the SDK options builder calls once, after ALL server entries (registry
    ``spec.mcp_servers``, ``$HOME/.mcp.json``, channel sidecars) have been
    assembled. No-op when no manifest is present; fail-loud when the
    manifest cannot be honoured (see :func:`resolve_spec_env`).
    """
    if not servers:
        return
    bake_spec_env_values(servers, resolve_spec_env(environ))


__all__ = [
    "SPEC_ENV_KEYS_VAR",
    "SpecEnvUnresolvedError",
    "bake_spec_env_into_servers",
    "bake_spec_env_values",
    "resolve_spec_env",
    "spec_env_keys_flag",
]
