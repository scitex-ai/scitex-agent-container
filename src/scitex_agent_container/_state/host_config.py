"""Sac-local host & peer configuration (F-CS12).

Lives at ``~/.scitex/agent-container/config.yaml`` (or under
``$SCITEX_AGENT_CONTAINER_HOME/config.yaml``). Separate from any orochi
config — sac never reaches out to orochi; orochi is a separate
concern that pulls from sac via ssh.

YAML shape::

    host:
      canonical: $SAC_HOST          # explicit override; takes priority
      aliases:
        Yusukes-MacBook-Air: mba
        spartan-login1:      spartan
        DXP480TPLUS-994:     nas
      fallback: hostname-short      # 'hostname-short' | 'hostname-fqdn'

    peers:
      mba:     { ssh: ywatanabe@mba.local }
      spartan: { ssh: ywatanabe@spartan-login1, via: [mba] }
      bm198:   { ssh: bm198, via: [mba, spartan] }
      nas:     { ssh: admin@192.168.11.22 }

    lead:                           # ADR-0013 Phase 1 — agent→lead push inbox.
      name: lead                    # Target name on the lead's `sac listen`
                                    #   (POST /agents/<name>/message:send).
      host: mba                     # Peer key for transport + peer-tokens.
      a2a_port: 8642                # Lead's sac listen port (host-bound).

Resolution chain for the local canonical hostname (used by every
state.db write so cross-host queries scope correctly):

  1. ``$SAC_HOST`` env var (explicit override)
  2. ``host.canonical`` if set in config.yaml (and not the literal
     ``$SAC_HOST`` placeholder)
  3. ``host.aliases[$(hostname -s)]`` if matching
  4. ``$(hostname -s)`` (or fqdn when fallback=hostname-fqdn)

The config is missing-tolerant: every key is optional. With no
config.yaml at all the chain still produces a sensible canonical name
via ``hostname -s``.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from scitex_config._ecosystem import local_state as _local_state

from .._env import getenv as _sac_env


def _default_config_path() -> Path:
    """Resolve config.yaml via the SciTeX local-state cascade.

    Honours ``$SCITEX_AGENT_CONTAINER_CONFIG`` as an explicit override;
    otherwise project-scope (``<repo>/.scitex/agent-container/config.yaml``)
    wins when it exists, else user-scope under ``$SCITEX_DIR/agent-container/``
    (default ``~/.scitex/...``). See
    ``01_ecosystem_06_local-state-directories.md``.
    """
    override = os.environ.get("SCITEX_AGENT_CONTAINER_CONFIG")
    if override:
        return Path(override)
    return _local_state.path("agent-container", "config.yaml")


@dataclass(frozen=True)
class ResolveSpec:
    """Dispatch-time peer-target resolution descriptor (Phase 1 schema).

    Carried on a :class:`PeerSpec` via the ``resolve`` field. When set,
    the peer's live ``ssh`` target is to be filled in at dispatch time
    by querying ``source`` (currently only ``"scitex-hpc"``) instead of
    being pinned statically in ``peers.yaml``. Phase 1 of the
    label-style-peer migration only *parses and validates* this field —
    no resolver code runs. Phase 2 will wire the lookup into
    ``try_dispatch``. Full plan in the lead's planning doc
    ``sac-dispatch-time-node-resolution.md``.

    Fields:
      source: backend identifier. Phase 1 accepts only ``"scitex-hpc"``;
        any other value raises ``ValueError`` at config-load time so
        operator typos surface immediately, not at dispatch.
      reservation: the scitex-hpc reservation name (used when
        ``source == "scitex-hpc"``). Optional at the schema level so
        future backends can omit it; Phase 2's resolver will enforce
        presence at resolve time for scitex-hpc.
    """

    source: str
    reservation: str | None = None


@dataclass(frozen=True)
class PeerSpec:
    """One peer entry from ``peers:`` in config.yaml.

    ``env_preamble`` is an optional sequence of shell snippets that must
    run on the remote *before* the dispatched command — used for hosts
    that gate tools (apptainer, conda, ...) behind Lmod and friends.
    Spartan is the canonical example: ``apptainer`` is only on $PATH
    after two separate ``module load`` calls (see the Spartan host
    skill doc for the rationale).

    ``resolve`` (Phase 1 schema only — no resolver behavior yet) marks
    the peer as a *label* whose real ssh target is computed at dispatch
    time. When ``resolve`` is set, ``ssh`` may be left empty and will
    be populated by the Phase 2 resolver. When both ``resolve`` and
    ``ssh`` are present, the explicit ``ssh`` is retained verbatim as a
    static fallback — Phase 2 will decide the precedence rule. See
    :class:`ResolveSpec` for the field shape.
    """

    name: str
    ssh: str  # 'user@host[:port]' or just 'host' (assumes ~/.ssh/config)
    via: tuple[str, ...] = ()  # ssh ProxyJump chain by peer name
    env_preamble: tuple[str, ...] = ()  # remote shell snippets joined by &&
    resolve: ResolveSpec | None = None  # dispatch-time target resolution

    @classmethod
    def from_dict(cls, spec: dict, *, name: str = "<anonymous>") -> "PeerSpec":
        """Build a :class:`PeerSpec` from one ``peers:`` YAML mapping.

        Mirrors the per-peer parsing that :func:`load` does inline, so
        unit tests can exercise the schema without needing a config
        file on disk. Raises ``ValueError`` for malformed shapes (same
        rules as ``load``).
        """
        if not isinstance(spec, dict):
            raise ValueError(
                f"peer '{name}': expected a mapping with ssh:/via:/..., "
                f"got {type(spec).__name__}"
            )
        via_raw = spec.get("via") or []
        if not isinstance(via_raw, list):
            raise ValueError(f"peer '{name}': via: must be a list")
        return cls(
            name=name,
            ssh=str(spec.get("ssh") or ""),
            via=tuple(str(x) for x in via_raw),
            env_preamble=_parse_env_preamble(name, spec.get("env_preamble")),
            resolve=_parse_resolve(name, spec.get("resolve")),
        )

    def jump_chain(self, peers: dict[str, "PeerSpec"]) -> list[str]:
        """Resolve ``via`` peer names into their ssh targets in order.

        Used to build the ``-J peer1,peer2`` ProxyJump argument when
        executing on a multi-hop peer. Unknown intermediate names are
        silently dropped — config.yaml is operator-edited and should fail
        loudly elsewhere (see ``Config.validate``).
        """
        return [peers[name].ssh for name in self.via if name in peers]

    def joined_preamble(self) -> str:
        """Return ``env_preamble`` lines joined by ``&&`` (empty if unset).

        ``&&`` rather than ``;`` so a failed ``module load`` short-circuits
        the dispatched command — surfaces config breakage as a clear
        non-zero exit instead of silently running with an unbound PATH.
        """
        return " && ".join(line for line in self.env_preamble if line.strip())


@dataclass(frozen=True)
class HostBlock:
    canonical: str | None = None
    aliases: dict[str, str] = field(default_factory=dict)
    fallback: str = "hostname-short"  # 'hostname-short' | 'hostname-fqdn'


@dataclass(frozen=True)
class LeadConfig:
    """``lead:`` block — agent→lead push inbox target (ADR-0013 Phase 1).

    Identifies the lead's ``sac listen`` instance so an agent can POST a
    typed completion/blocker/status event to it via
    ``/agents/<name>/message:send``. The lead is just another A2A node
    on the existing control plane; this block is the one place every
    agent learns where it lives.

    Fields:
      name: target name on the lead's listen (used as ``<name>`` in
        the POST path and as the ACL identity the lead's listen sees).
      host: peer key (must exist under ``peers:``) used for two
        independent jobs — looking up the per-host bearer token under
        ``peer-tokens/<host>.token`` (the credential the agent
        authenticates with) and naming the destination host for the
        outbound HTTP request. Reusing the peer key keeps the lead
        consistent with how every other cross-host node is addressed.
      a2a_port: TCP port the lead's ``sac listen`` is bound to (the
        host-wide listen, not a per-agent sidecar). Required because
        the lead's listen is not registered in any state.db's
        ``instances`` table — it is not an agent and has no
        ``record_instance_start`` call.

    Loud failure is the only failure mode. Phase 1 ships the helper
    plus CLI; the lead-inbox push refuses to dispatch when any of
    ``name`` / ``host`` / ``a2a_port`` is missing rather than guessing.
    See :mod:`scitex_agent_container._state.lead_inbox`.
    """

    name: str
    host: str
    a2a_port: int


@dataclass(frozen=True)
class Config:
    host: HostBlock = field(default_factory=HostBlock)
    peers: dict[str, PeerSpec] = field(default_factory=dict)
    lead: LeadConfig | None = None
    source_path: Path | None = None

    def canonical_host(self) -> str:
        """Resolve the local canonical hostname (see module docstring)."""
        env_override = _sac_env("HOST")
        if env_override:
            return env_override
        if self.host.canonical and self.host.canonical != "$SAC_HOST":
            return self.host.canonical
        raw_short = socket.gethostname().split(".")[0]
        if raw_short in self.host.aliases:
            return self.host.aliases[raw_short]
        if self.host.fallback == "hostname-fqdn":
            return socket.getfqdn()
        return raw_short

    def peer(self, name: str) -> PeerSpec | None:
        return self.peers.get(name)

    def validate(self) -> list[str]:
        """Return a list of human-readable errors. Empty = valid.

        Peers with a ``resolve:`` block are allowed to leave ``ssh:``
        empty — Phase 2's resolver will populate it at dispatch time.
        Peers without ``resolve:`` still require an explicit ``ssh:``.
        """
        errors: list[str] = []
        for pname, p in self.peers.items():
            # Skip glob-pattern keys — their ssh is synthesized per-query
            # from the matched hostname (see PeersMap.__getitem__).
            is_pattern = any(c in pname for c in "*?[")
            for hop in p.via:
                if hop not in self.peers:
                    errors.append(
                        f"peer '{pname}': via=[..., '{hop}', ...] references "
                        f"an unknown peer (define '{hop}' under peers:)"
                    )
            if not p.ssh and p.resolve is None and not is_pattern:
                errors.append(
                    f"peer '{pname}': ssh: is required (or set resolve: to "
                    f"populate it at dispatch time)"
                )
        valid_fallbacks = {"hostname-short", "hostname-fqdn"}
        if self.host.fallback not in valid_fallbacks:
            errors.append(
                f"host.fallback must be one of {sorted(valid_fallbacks)}, "
                f"got {self.host.fallback!r}"
            )
        return errors


class PeersMap(dict):
    """Dict of ``name -> PeerSpec`` with glob-pattern fallback on lookup.

    Keys may include fnmatch metacharacters (``*``, ``?``, ``[...]``).
    On ``__getitem__``/``__contains__``/``get``, an exact match wins;
    otherwise pattern keys are tried in insertion order and the first
    match returns a synthesized :class:`PeerSpec` carrying the queried
    name (not the pattern) and the pattern's other fields. The
    synthesized peer's ``ssh`` falls back to the queried name when the
    pattern entry left ``ssh`` blank — so ``spartan*: { via: [spartan],
    env_preamble: ... }`` resolves ``spartan-bm043`` to a peer with
    ``ssh=spartan-bm043`` and the shared env_preamble.

    Iteration (``items()``, ``keys()``, ``len()``) is unchanged — it
    enumerates the literal config entries, including pattern keys.
    """

    def _glob_match(self, key):
        if not isinstance(key, str):
            return None
        for pattern in self.keys():
            if any(c in pattern for c in "*?[") and fnmatch.fnmatchcase(key, pattern):
                spec = dict.__getitem__(self, pattern)
                return dataclasses.replace(spec, name=key, ssh=(spec.ssh or key))
        return None

    def __getitem__(self, key):
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        matched = self._glob_match(key)
        if matched is not None:
            return matched
        raise KeyError(key)

    def __contains__(self, key):
        if dict.__contains__(self, key):
            return True
        return self._glob_match(key) is not None

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


def load(path: Path | None = None) -> Config:
    """Read config.yaml; missing file or empty file yields defaults."""
    p = Path(path) if path else _default_config_path()
    if not p.is_file():
        return Config(source_path=p)
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config.yaml must be a mapping at top level: {p}")

    host_raw = raw.get("host") or {}
    if not isinstance(host_raw, dict):
        raise ValueError(f"config.yaml: 'host' must be a mapping: {p}")
    aliases_raw = host_raw.get("aliases") or {}
    if not isinstance(aliases_raw, dict):
        raise ValueError(f"config.yaml: 'host.aliases' must be a mapping: {p}")
    host = HostBlock(
        canonical=host_raw.get("canonical"),
        aliases={str(k): str(v) for k, v in aliases_raw.items()},
        fallback=host_raw.get("fallback") or "hostname-short",
    )

    peers_raw = raw.get("peers") or {}
    if not isinstance(peers_raw, dict):
        raise ValueError(f"config.yaml: 'peers' must be a mapping: {p}")
    peers: PeersMap = PeersMap()
    for name, spec in peers_raw.items():
        if not isinstance(spec, dict):
            raise ValueError(
                f"config.yaml: peer '{name}' must be a mapping with ssh:/via:"
            )
        via_raw = spec.get("via") or []
        if not isinstance(via_raw, list):
            raise ValueError(f"config.yaml: peer '{name}' via: must be a list")
        peers[str(name)] = PeerSpec(
            name=str(name),
            ssh=str(spec.get("ssh") or ""),
            via=tuple(str(x) for x in via_raw),
            env_preamble=_parse_env_preamble(name, spec.get("env_preamble")),
            resolve=_parse_resolve(name, spec.get("resolve")),
        )

    lead = _parse_lead(raw.get("lead"), source_path=p)

    return Config(host=host, peers=peers, lead=lead, source_path=p)


_RESOLVE_ALLOWED_SOURCES = ("scitex-hpc",)


def _parse_lead(raw, *, source_path: Path) -> LeadConfig | None:
    """Normalize the optional ``lead:`` YAML block into a :class:`LeadConfig`.

    Missing block → ``None`` (config-load stays missing-tolerant; the
    lead-inbox helpers raise their own loud error when an agent tries
    to push with no lead configured). Present block → strict
    validation: ``name`` (non-empty string), ``host`` (non-empty
    string), ``a2a_port`` (positive int). Any other shape raises
    ``ValueError`` naming ``source_path`` so operator typos surface
    at config-load time rather than as opaque HTTP failures from the
    push helper.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(
            f"config.yaml at {source_path}: 'lead' must be a mapping with "
            f"name:/host:/a2a_port:, got {type(raw).__name__}"
        )
    name = raw.get("name")
    host = raw.get("host")
    port = raw.get("a2a_port")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"config.yaml at {source_path}: 'lead.name' is required and "
            f"must be a non-empty string (got {name!r})"
        )
    if not isinstance(host, str) or not host.strip():
        raise ValueError(
            f"config.yaml at {source_path}: 'lead.host' is required and "
            f"must be a non-empty string (got {host!r})"
        )
    if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
        raise ValueError(
            f"config.yaml at {source_path}: 'lead.a2a_port' is required "
            f"and must be a positive integer (got {port!r})"
        )
    return LeadConfig(name=name, host=host, a2a_port=port)


def _parse_resolve(name: str, raw) -> ResolveSpec | None:
    """Normalize a peer's ``resolve:`` YAML field into a :class:`ResolveSpec`.

    Phase 1 of the dispatch-time-resolution architecture (full plan at
    ``~/proj/scitex-lead/GITIGNORED/FUTURE/sac-dispatch-time-node-resolution.md``).
    This helper only *parses* the field; no network / scitex-hpc lookup
    happens here.

    Accepted shapes:

    * Missing / ``None`` → returns ``None`` (peer has no resolver).
    * A mapping with at minimum ``source:``. Phase 1 accepts only
      ``source: scitex-hpc``; any other value raises ``ValueError``
      naming the peer, so operator typos surface at config-load time
      rather than at dispatch.

    Any other shape (scalar, list, ...) raises ``ValueError``.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(
            f"config.yaml: peer '{name}' resolve: must be a mapping with "
            f"source: (and source-specific keys), got {type(raw).__name__}"
        )
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError(
            f"config.yaml: peer '{name}' resolve.source is required and "
            f"must be a non-empty string"
        )
    if source not in _RESOLVE_ALLOWED_SOURCES:
        raise ValueError(
            f"config.yaml: peer '{name}' resolve.source={source!r} is "
            f"unknown; allowed values: {list(_RESOLVE_ALLOWED_SOURCES)}"
        )
    reservation = raw.get("reservation")
    if reservation is not None and not isinstance(reservation, str):
        raise ValueError(
            f"config.yaml: peer '{name}' resolve.reservation must be a "
            f"string if set, got {type(reservation).__name__}"
        )
    return ResolveSpec(source=source, reservation=reservation)


def _parse_env_preamble(name: str, raw) -> tuple[str, ...]:
    """Normalize a peer's ``env_preamble:`` YAML field into a tuple.

    Accepts three shapes:

    * Missing / ``None`` → ``()``.
    * A scalar string (possibly multi-line via YAML's ``|`` literal block).
      Split on newlines; blank lines and ``#``-only lines are dropped.
    * A list of strings — each element is one shell snippet.

    Any other shape (dict, list-of-non-strings, ...) raises ``ValueError``
    with the peer name so the operator's typo surfaces at config-load
    time, not as an opaque ssh failure mid-dispatch.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        lines = [line.strip() for line in raw.splitlines()]
        return tuple(line for line in lines if line and not line.startswith("#"))
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise ValueError(
                    f"config.yaml: peer '{name}' env_preamble entries must "
                    f"be strings, got {type(item).__name__}"
                )
            stripped = item.strip()
            if stripped and not stripped.startswith("#"):
                out.append(stripped)
        return tuple(out)
    raise ValueError(
        f"config.yaml: peer '{name}' env_preamble must be a string or list "
        f"of strings, got {type(raw).__name__}"
    )


# ssh ControlMaster option rendering lives in its own module so the test
# file mirror is 1:1 (project rule PS-204). This re-export keeps the
# existing import surface (`from ..._state.host_config import
# ssh_control_options`) working unchanged for downstream callers.
from .ssh_control_options import (  # noqa: E402,F401
    ssh_control_options,
    ssh_control_options_str,
)

# Transport rendering (``build_ssh_argv``) and interface inventory
# (``host_interfaces``) live in focused sibling modules so the config
# schema file stays under the per-file line cap. Re-exports preserve
# the existing import path
# ``from scitex_agent_container._state.host_config import build_ssh_argv``
# / ``host_interfaces``. Tests for those functions sit unchanged
# against the old import path.
from ._host_ssh import build_ssh_argv  # noqa: E402,F401
from ._host_interfaces import host_interfaces  # noqa: E402,F401
