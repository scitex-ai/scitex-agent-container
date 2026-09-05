"""Sac-local host & peer configuration (F-CS12).

Lives at ``~/.scitex/agent-container/config.yaml`` (or under
``$SCITEX_AGENT_CONTAINER_HOME/config.yaml``). Separate from any external
orchestrator's config — sac never reaches out to one; an orchestrator is a
separate concern that pulls from sac via ssh.

YAML shape::

    host:
      canonical: $SAC_HOST          # explicit override; takes priority
      aliases:
        Yusukes-MacBook-Air: mba
        spartan-login1:      spartan
        DXP480TPLUS-994:     nas-03
      fallback: hostname-short      # 'hostname-short' | 'hostname-fqdn'

    peers:
      mba:     { ssh: ywatanabe@mba.local }
      spartan: { ssh: ywatanabe@spartan-login1, via: [mba] }
      bm198:   { ssh: bm198, via: [mba, spartan] }
      nas-03:  { ssh: nas-03, reverse_ssh: ywata-note-win }

Peer keys must be STABLE names. ``nas-03``, not ``nas`` — the bare name is
re-pointed to the next machine as hardware is replaced, so it keeps resolving
while addressing a different host. :mod:`.moving_alias` holds the registry and
:meth:`Config.validate` refuses one as a key. Convenience aliases belong in
``~/.ssh/config``, where "the current NAS" is what a human means.

    lead:                           # ADR-0013 Phase 1 — agent→lead push inbox.
      name: lead                    # Target name on the lead's `sac listen`
                                    #   (POST /agents/<name>/message:send).
      host: mba                     # Peer key for transport + peer-tokens.
      a2a_port: 8642                # Lead's sac listen port (host-bound).

    scratch_root: /scratch          # ADR-0024 — where every agent's /uvwork is
                                    #   bound from (<root>/sac/agents/<agent>/
                                    #   uvwork). Absent: /scratch if it exists,
                                    #   else REFUSE to start. The literal
                                    #   `none` keeps /uvwork in the overlay and
                                    #   requires `scratch_root_reason:`.

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

# ``LeadConfig`` / ``ResolveSpec`` live in ._host_config_blocks but are
# re-exported here: ``lead_inbox`` and the config tests import them from
# this module, and the extraction must not move a public import path.
from ._host_config_blocks import (  # noqa: F401
    LeadConfig,
    ResolveSpec,
    ScratchBlock,
    _parse_lead,
    _parse_resolve,
    _parse_scratch,
)
from .moving_alias import MovingAliasError, moving_alias_hint


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

    ``login_shell`` (default False) says the peer's login profile may be
    sourced by an agent start/restart dispatched to it. On this fleet the
    profile is the ONLY carrier of ~/.bash.d/secrets (measured 2026-09-05
    on scitex-compute-01: zero CCT_* / no gateway key under a bare or
    ``bash -c`` command, all of them under ``bash -lc``), so a peer that
    also needs an ``env_preamble`` for PATH must opt in here or every
    engine with an ``auth_token_env`` is refused there as "unset". HPC
    peers stay False: sourcing their profile kills the login.

    ``reverse_ssh`` names the PEER's ssh route back to the master; the
    push-config renderer falls back to the master's name when empty.
    """

    name: str
    ssh: str  # 'user@host[:port]' or just 'host' (assumes ~/.ssh/config)
    via: tuple[str, ...] = ()  # ssh ProxyJump chain by peer name
    env_preamble: tuple[str, ...] = ()  # remote shell snippets joined by &&
    resolve: ResolveSpec | None = None  # dispatch-time target resolution
    # A preamble peer whose LOGIN profile is safe to source and carries the
    # fleet secrets (the compute hosts). False keeps the preamble branch on a
    # plain `bash -c` -- the HPC compute-node bashrc kill (see _host_ssh).
    login_shell: bool = False
    reverse_ssh: str = ""  # peer→master ssh target (sac host push-config)

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
            reverse_ssh=str(spec.get("reverse_ssh") or ""),
            login_shell=bool(spec.get("login_shell", False)),
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
class Config:
    host: HostBlock = field(default_factory=HostBlock)
    peers: dict[str, PeerSpec] = field(default_factory=dict)
    lead: LeadConfig | None = None
    scratch: ScratchBlock | None = None  # scratch_root: / scratch_root_reason:
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
        for pname in self.peers:
            hint = moving_alias_hint(pname, context="peer key in config.yaml")
            if hint:
                errors.append(f"peer '{pname}': {hint}")
        for alias_from, alias_to in self.host.aliases.items():
            # The VALUE is the canonical name every state.db row is stamped
            # with, so a moving alias there makes THIS machine's identity move.
            # The KEY is a raw `hostname -s` reading and cannot move.
            hint = moving_alias_hint(alias_to, context="canonical host name")
            if hint:
                errors.append(f"host.aliases['{alias_from}']: {hint}")
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
        # Every sac dispatch target lands here, so this is the one place that
        # can tell "peer you never defined" from "peer whose name moves".
        # A bare KeyError('nas') reads as a config typo; it is the opposite —
        # the name is fine and the MACHINE it names is what changed.
        hint = moving_alias_hint(key, context="dispatch target")
        if hint:
            raise MovingAliasError(hint)
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


# Parsed-config cache, keyed on the file's IDENTITY rather than its path, so a
# rewritten config.yaml is never served stale.
#
# WHY IT EXISTS — measured 2026-08-09 on the live host. `GET /agents` calls
# `resolve_a2a_host()` once per row (_listen/_registry_endpoints.py:111-113),
# and that called this function, which re-read and re-parsed config.yaml from
# disk EVERY time. Counting `yaml.safe_load` by caller on ONE warm request:
#
#     23  host_config.load        <- 23 identical parses of ONE global file
#     17  _load_spec_dict         <- per-agent specs, no duplication
#      1  load_self_peer
#
# 56% of the parse calls in the request were this file, re-read for rows that
# cannot differ in it. Nothing about config.yaml varies per agent.
#
# Keyed on (resolved path, st_mtime_ns, st_size): mtime alone can miss a
# same-nanosecond rewrite on coarse clocks, and size catches the common
# edit-in-place case that mtime granularity could hide. A `stat` per call is
# ~microseconds against a parse of ~25ms.
_PARSED_CACHE: dict[tuple[str, int, int], "Config"] = {}


def load(path: Path | None = None) -> Config:
    """Read config.yaml; missing file or empty file yields defaults.

    Parsed results are cached per (path, mtime, size) — see `_PARSED_CACHE`.
    """
    p = Path(path) if path else _default_config_path()
    if not p.is_file():
        return Config(source_path=p)
    try:
        _st = p.stat()
        _key: tuple[str, int, int] | None = (str(p), _st.st_mtime_ns, _st.st_size)
    except OSError:  # stx-allow: fallback (reason: stat failure must not break config loading — degrade to an uncached parse)
        _key = None
    if _key is not None:
        _hit = _PARSED_CACHE.get(_key)
        if _hit is not None:
            return _hit
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
        # One parser for the per-peer schema: ``PeerSpec.from_dict`` is
        # the same code path unit tests drive, so file-load and direct
        # construction can never drift apart (and the schema — including
        # ``reverse_ssh`` — is defined in exactly one place).
        peers[str(name)] = PeerSpec.from_dict(spec, name=str(name))

    lead = _parse_lead(raw.get("lead"), source_path=p)
    scratch = _parse_scratch(
        raw.get("scratch_root"), raw.get("scratch_root_reason"), source_path=p
    )

    cfg = Config(host=host, peers=peers, lead=lead, scratch=scratch, source_path=p)
    if _key is not None:
        # Bounded: one entry per (path, mtime, size) actually parsed. In
        # practice one live entry plus a short tail of superseded ones after an
        # edit; clearing wholesale on a miss would defeat multi-path callers
        # (tests pass explicit paths), so trim oldest-first instead.
        if len(_PARSED_CACHE) >= 32:
            for _stale in list(_PARSED_CACHE)[:16]:
                _PARSED_CACHE.pop(_stale, None)
        _PARSED_CACHE[_key] = cfg
    return cfg


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
from ._host_interfaces import host_interfaces  # noqa: E402,F401

# Transport rendering (``build_ssh_argv``) and interface inventory
# (``host_interfaces``) live in focused sibling modules so the config
# schema file stays under the per-file line cap. Re-exports preserve
# the existing import path
# ``from scitex_agent_container._state.host_config import build_ssh_argv``
# / ``host_interfaces``. Tests for those functions sit unchanged
# against the old import path.
from ._host_ssh import (
    build_ssh_argv,  # noqa: E402,F401
    resolve_peer_scitex_root,  # noqa: E402,F401
)
from .ssh_control_options import (  # noqa: E402,F401
    ssh_control_options,
    ssh_control_options_str,
)
