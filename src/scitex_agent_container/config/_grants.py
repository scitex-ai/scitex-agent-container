"""The GRANTS axis — ``spec.grants``: which host authorities an agent holds.

Every SAC agent used to receive a large set of host authorities implicitly
(fleet binds, the sac control plane, host secrets, the host network, the
operator's ``~/.claude`` …). ``spec.grants`` makes each of them an explicit,
named grant. Two states, decided per spec:

  * **absent** — LEGACY. Every implicit authority applies exactly as it did
    before this key existed; the rendered argv / env / home are
    byte-identical. ``sac agents explain`` labels each one
    "implicit (legacy default)", and ``sac agents check`` warns that the
    spec relies on a default that a later release flips to deny.
  * **present** — DENY BY DEFAULT. The agent receives only what the block
    grants; an omitted key is withheld. ``grants: {}`` withholds everything.

The grant set is SMALL on purpose (docs/grants.md maps every inventoried
authority to exactly one key, or documents why it is not an authority):

  host_exec        bool   POST /v1/host_exec (arbitrary host argv)
  sac_control      bool   sac-builtin MCP + server:sac channel + listen
                          URL / bearer / per-agent token
  host_network     bool   share the host network namespace (non-sealed
                          agents REQUIRE it: a non-sealed launch without it
                          is refused — see :func:`network_refusal`)
  host_binds       list   named fleet binds (:data:`HOST_BIND_NAMES`)
  fleet_env        bool   fleet default env, PG + board identity, runtime
                          dir, spec search dirs, genai tunnel, direnv
  host_claude      list   host ~/.claude parts (:data:`HOST_CLAUDE_PARTS`)
  secrets          list   host secret deliveries (:data:`SECRET_PARTS`)
  host_model_auth  bool   host-held model credentials: the unpinned host
                          ~/.claude OAuth dir and host-env API keys.
                          Credentials the spec DECLARES (claude.account,
                          credentials_file, a provider, a codex
                          subscription account) are explicit already.

Pure data + pure functions; imported lazily by every enforcement site so
``sac --help``'s import closure does not grow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "BOOL_GRANTS",
    "GRANTS_KEY",
    "GRANT_KEYS",
    "GrantRefused",
    "Grants",
    "HOST_BIND_NAMES",
    "HOST_EXEC_GROUPS",
    "HOST_CLAUDE_PARTS",
    "LIST_GRANTS",
    "SECRET_PARTS",
    "all_granted",
    "granted",
    "host_exec_state",
    "grants_value_errors",
    "is_explicit",
    "network_refusal",
    "parse_grants",
]

GRANTS_KEY = "grants"

BOOL_GRANTS: tuple[str, ...] = (
    "host_exec",
    "sac_control",
    "host_network",
    "fleet_env",
    "host_model_auth",
)

# Groups whose members the listen server lets broker host argv (developer,
# researcher, privileged — operator rulings 2026-07-01/02). SSOT; the server
# (``_listen._host_exec.ELIGIBLE_GROUPS``) re-exports it.
HOST_EXEC_GROUPS: frozenset[str] = frozenset({"developer", "researcher", "privileged"})

# Named fleet binds (A4-A8 of the authority inventory). The names are the
# grant vocabulary; ``_p3a_default_binds`` owns the actual bind strings.
HOST_BIND_NAMES: tuple[str, ...] = (
    "cards",
    "cct_store",
    "emacs_handoff",
    "host_tmp",
    "quota_cache",
)

# Host ~/.claude materialisation parts (G1-G4, H6).
HOST_CLAUDE_PARTS: tuple[str, ...] = ("commands", "skills", "merge", "baseline")

# Host secret deliveries (G5-G7, H1, H3).
SECRET_PARTS: tuple[str, ...] = ("envrc", "github_token", "cct_token", "operator_pool")

LIST_GRANTS: dict[str, tuple[str, ...]] = {
    "host_binds": HOST_BIND_NAMES,
    "host_claude": HOST_CLAUDE_PARTS,
    "secrets": SECRET_PARTS,
}

GRANT_KEYS: tuple[str, ...] = (
    "host_exec",
    "sac_control",
    "host_network",
    "host_binds",
    "fleet_env",
    "host_claude",
    "secrets",
    "host_model_auth",
)


class GrantRefused(RuntimeError):
    """A launch needs an authority its explicit ``spec.grants`` withholds."""


@dataclass(frozen=True)
class Grants:
    """An EXPLICIT grants block (a legacy spec carries ``None``, not this)."""

    host_exec: bool = False
    sac_control: bool = False
    host_network: bool = False
    host_binds: frozenset[str] = frozenset()
    fleet_env: bool = False
    host_claude: frozenset[str] = frozenset()
    secrets: frozenset[str] = frozenset()
    host_model_auth: bool = False

    def allows(self, key: str, part: str | None = None) -> bool:
        """True when this block grants ``key`` (or ``part`` of a list key)."""
        value = getattr(self, key)
        if key in LIST_GRANTS:
            if part is None:
                raise ValueError(f"grants.{key} is a list grant; name the part")
            return part in value
        return bool(value)

    def to_block(self) -> dict[str, Any]:
        """The YAML-ready ``grants:`` mapping, keys in :data:`GRANT_KEYS` order."""
        out: dict[str, Any] = {}
        for key in GRANT_KEYS:
            value = getattr(self, key)
            if key in LIST_GRANTS:
                out[key] = [p for p in LIST_GRANTS[key] if p in value]
            else:
                out[key] = bool(value)
        return out


def all_granted() -> Grants:
    """The block equivalent to the legacy default: every grant held."""
    return Grants(
        **{key: True for key in BOOL_GRANTS},
        **{key: frozenset(parts) for key, parts in LIST_GRANTS.items()},
    )


def parse_grants(spec: Mapping) -> Grants | None:
    """``spec.grants`` → :class:`Grants`, or ``None`` when the key is absent.

    Callers validate first (:func:`grants_value_errors`); this only
    projects. ``grants: null`` is treated as absent — the same "states
    nothing" identity rule every other optional axis uses.
    """
    raw = spec.get(GRANTS_KEY) if isinstance(spec, Mapping) else None
    if raw is None:
        return None
    block = dict(raw)
    kwargs: dict[str, Any] = {}
    for key in BOOL_GRANTS:
        kwargs[key] = bool(block.get(key, False))
    for key in LIST_GRANTS:
        kwargs[key] = frozenset(str(p) for p in (block.get(key) or ()))
    return Grants(**kwargs)


def _declares_control_plane(spec: Mapping) -> list[str]:
    """Spec lines that wire the sac control plane by hand."""
    found: list[str] = []
    comms = spec.get("comms") if isinstance(spec.get("comms"), Mapping) else {}
    claude = spec.get("claude") if isinstance(spec.get("claude"), Mapping) else {}
    channels = list((comms or {}).get("channels") or []) + list(
        (claude or {}).get("channels") or []
    )
    if any(str(c).strip() == "server:sac" for c in channels):
        found.append("comms.channels names server:sac")
    servers = spec.get("mcp_servers")
    if isinstance(servers, Mapping) and "scitex-agent-container" in servers:
        found.append("mcp_servers declares scitex-agent-container")
    return found


def grants_value_errors(spec: Mapping) -> list[str]:
    """List-form check of ``spec.grants`` for ``validate_raw``.

    Shape errors (unknown key, wrong type, unknown part) plus the
    contradictions that would otherwise resolve SILENTLY one way or the
    other — each is refused so a grant is never implied by a side door.
    """
    if not isinstance(spec, Mapping) or spec.get(GRANTS_KEY) is None:
        return []
    raw = spec[GRANTS_KEY]
    if not isinstance(raw, Mapping):
        return [f"spec.{GRANTS_KEY} must be a mapping (got {type(raw).__name__})"]
    errors: list[str] = []
    unknown = sorted(set(map(str, raw)) - set(GRANT_KEYS))
    if unknown:
        errors.append(
            f"spec.{GRANTS_KEY} has unknown keys {unknown}; known: {list(GRANT_KEYS)}"
        )
    for key in BOOL_GRANTS:
        if key in raw and type(raw[key]) is not bool:
            errors.append(f"spec.{GRANTS_KEY}.{key} must be true or false")
    for key, parts in LIST_GRANTS.items():
        if key not in raw or raw[key] is None:
            continue
        value = raw[key]
        if not isinstance(value, list):
            errors.append(f"spec.{GRANTS_KEY}.{key} must be a list of {list(parts)}")
            continue
        bad = sorted({str(p) for p in value} - set(parts))
        if bad:
            errors.append(
                f"spec.{GRANTS_KEY}.{key} names unknown parts {bad}; known: {list(parts)}"
            )
    if errors:
        return errors
    grants = parse_grants(spec)
    assert grants is not None
    if grants.host_exec and not grants.sac_control:
        errors.append(
            f"spec.{GRANTS_KEY}.host_exec requires spec.{GRANTS_KEY}.sac_control: "
            "host exec is served by the sac control plane"
        )
    if not grants.sac_control:
        for line in _declares_control_plane(spec):
            errors.append(
                f"spec.{line} but spec.{GRANTS_KEY}.sac_control is not granted; "
                "grant it or remove the declaration"
            )
    if spec.get("sandbox") is not None and grants.host_network:
        errors.append(
            f"spec.{GRANTS_KEY}.host_network contradicts spec.sandbox: a sealed "
            "run's network is its egress allowlist"
        )
    layers = spec.get("to_home_layers")
    if "baseline" not in grants.host_claude and isinstance(layers, list):
        shared = sorted({str(x) for x in layers} - {"per-agent"})
        if shared:
            errors.append(
                f"spec.to_home_layers names shared layers {shared} but "
                f"spec.{GRANTS_KEY}.host_claude does not grant 'baseline'"
            )
    return errors


def is_explicit(config: Any) -> bool:
    """True when ``config`` carries an explicit ``grants:`` block."""
    return getattr(config, "grants", None) is not None


def granted(config: Any, key: str, part: str | None = None) -> bool:
    """The ONE question every enforcement site asks.

    A legacy config (no ``grants:`` block) answers True for everything —
    which is what keeps the legacy rendering byte-identical: every gate is
    a no-op until a spec opts in.
    """
    grants = getattr(config, "grants", None)
    if grants is None:
        return True
    return grants.allows(key, part)


def network_refusal(config: Any) -> str | None:
    """Why a launch must be REFUSED for want of ``host_network``, or None.

    A non-sealed apptainer agent always shares the host network namespace —
    there is no curated network short of the sealed run's allowlist gateway.
    Isolating it with ``--network none`` instead would leave the agent
    without its model endpoint: a silently broken agent rather than a
    refused one. So the honest choices are "grant host_network" or
    "declare spec.sandbox", and the launch says exactly that.
    """
    if granted(config, "host_network"):
        return None
    if getattr(config, "sandbox", None) is not None:
        return None
    return (
        f"agent {getattr(config, 'name', '?')!r} declares spec.grants without "
        "host_network, and a non-sealed apptainer agent always shares the host "
        "network namespace. Grant host_network: true, or declare spec.sandbox "
        "(a sealed run with an egress allowlist)."
    )


_LEGACY = "implicit (legacy default; will be denied when the default flips)"


def host_exec_state(config: Any) -> tuple[bool, str]:
    """``(held, why)`` — whether ``config`` can reach host exec, and who decided.

    Explicit block: ``grants.host_exec``. Legacy spec: the eligible groups
    its labels name (the same gate the listen server applies), unless sealed.
    """
    if is_explicit(config):
        if config.grants.host_exec:
            return True, "granted explicitly (spec.grants.host_exec: true)"
        return False, "withheld (spec.grants does not grant it)"
    from ._group_resolver import all_named_groups

    labels = getattr(config, "labels", None) or {}
    named = {str(g).strip().lower() for g in all_named_groups(dict(labels))}
    groups = sorted(named & HOST_EXEC_GROUPS)
    if getattr(config, "sandbox", None) is not None:
        return False, "not reachable (sealed run)"
    if groups:
        return True, f"IMPLICIT via groups {groups} ({_LEGACY})"
    return False, "not held (no eligible group)"
