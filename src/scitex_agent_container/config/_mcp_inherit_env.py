"""``spec.mcp_servers.<name>.inherit_env`` — per-entry env inheritance.

By default SAC makes the agent's env DURABLE in every stdio MCP entry: the
Claude ``.mcp.json`` bake (:mod:`..runtimes._mcp_spec_env`) copies
``spec.env`` into each entry, and the Hermes profile refills declared store /
identity names from the launch env (:mod:`..runtimes._hermes_mcp_env`). That
is right for store-backed servers and wrong for a server that must NOT see
the store: once a PostgreSQL DSN lands in an entry, SAC also generates
``PGPASSFILE`` for it and refuses the launch unless a host ``.pgpass`` row
backs the project role (live pilot 3, paper-scitex-clew, 2026-11-19).

``inherit_env`` is the per-entry switch, a SAC-only key that never reaches
the harness's own MCP config:

* ``true`` (default) — today's behaviour, unchanged;
* ``false`` — the entry receives ONLY the env it declares itself (its own
  ``env:`` block): no spec-env bake, no launch-env refill, no SAC-generated
  ``PGPASSFILE``;
* a list of env NAMES — an allowlist: only those spec/launch env names are
  baked/refilled, plus the entry's own ``env``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, TypeVar, Union

#: The SAC-only key on a ``spec.mcp_servers`` entry.
INHERIT_ENV_KEY = "inherit_env"

#: ``True`` (inherit all), ``False`` (inherit none) or an allowlist of names.
InheritMode = Union[bool, frozenset]

_V = TypeVar("_V")


def parse_inherit_env(server: str, value: Any) -> InheritMode:
    """Validate one ``inherit_env`` value; raise ``ValueError`` on any other type."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple)):
        names = [item.strip() for item in value if isinstance(item, str)]
        if len(names) == len(value) and all(names):
            return frozenset(names)
    raise ValueError(
        f"spec.mcp_servers.{server}.{INHERIT_ENV_KEY}: must be true, false, or "
        f"a list of non-empty env names; got {value!r}"
    )


def inherit_mode(server: str, entry: Any) -> InheritMode:
    """The entry's mode; an entry without the key inherits everything."""
    if not isinstance(entry, Mapping) or INHERIT_ENV_KEY not in entry:
        return True
    return parse_inherit_env(server, entry[INHERIT_ENV_KEY])


def server_modes(servers: Mapping[str, Any] | None) -> dict[str, InheritMode]:
    """``{name: mode}`` for every entry of ``servers`` (validates each)."""
    return {
        str(name): inherit_mode(str(name), e) for name, e in (servers or {}).items()
    }


def inherits(mode: InheritMode, name: str) -> bool:
    """True when an entry in ``mode`` may receive the inherited env ``name``."""
    if isinstance(mode, bool):
        return mode
    return name in mode


def select_inherited(mode: InheritMode, env: Mapping[str, _V]) -> dict[str, _V]:
    """The subset of ``env`` an entry in ``mode`` inherits (order kept)."""
    return {k: v for k, v in env.items() if inherits(mode, k)}


def without_sac_keys(entry: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of ``entry`` with SAC-only keys removed (harness-facing shape)."""
    return {k: v for k, v in entry.items() if k != INHERIT_ENV_KEY}


def describe_mode(mode: InheritMode) -> str:
    """Human label for ``sac agents explain``."""
    if isinstance(mode, bool):
        return "true" if mode else "false"
    return "[" + ", ".join(sorted(mode)) + "]"


def received_env_names(
    mode: InheritMode, declared: Iterable[str], inherited: Iterable[str]
) -> list[str]:
    """Sorted env NAMES an entry receives: its own ``env`` + what it inherits."""
    return sorted(set(declared) | {n for n in inherited if inherits(mode, n)})


__all__ = [
    "INHERIT_ENV_KEY",
    "InheritMode",
    "describe_mode",
    "inherit_mode",
    "inherits",
    "parse_inherit_env",
    "received_env_names",
    "select_inherited",
    "server_modes",
    "without_sac_keys",
]
