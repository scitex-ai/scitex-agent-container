"""Sac-local host & peer configuration (F-CS12).

Lives at ``~/.scitex/agent-container/sac.yaml`` (or under
``$SCITEX_AGENT_CONTAINER_HOME/sac.yaml``). Separate from any orochi
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

Resolution chain for the local canonical hostname (used by every
state.db write so cross-host queries scope correctly):

  1. ``$SAC_HOST`` env var (explicit override)
  2. ``host.canonical`` if set in sac.yaml (and not the literal
     ``$SAC_HOST`` placeholder)
  3. ``host.aliases[$(hostname -s)]`` if matching
  4. ``$(hostname -s)`` (or fqdn when fallback=hostname-fqdn)

The config is missing-tolerant: every key is optional. With no
sac.yaml at all the chain still produces a sensible canonical name
via ``hostname -s``.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(
    os.environ.get(
        "SCITEX_AGENT_CONTAINER_SAC_YAML",
        os.path.expanduser("~/.scitex/agent-container/sac.yaml"),
    )
)


@dataclass(frozen=True)
class PeerSpec:
    """One peer entry from ``peers:`` in sac.yaml."""

    name: str
    ssh: str  # 'user@host[:port]' or just 'host' (assumes ~/.ssh/config)
    via: tuple[str, ...] = ()  # ssh ProxyJump chain by peer name

    def jump_chain(self, peers: dict[str, "PeerSpec"]) -> list[str]:
        """Resolve ``via`` peer names into their ssh targets in order.

        Used to build the ``-J peer1,peer2`` ProxyJump argument when
        executing on a multi-hop peer. Unknown intermediate names are
        silently dropped — sac.yaml is operator-edited and should fail
        loudly elsewhere (see ``Config.validate``).
        """
        return [peers[name].ssh for name in self.via if name in peers]


@dataclass(frozen=True)
class HostBlock:
    canonical: str | None = None
    aliases: dict[str, str] = field(default_factory=dict)
    fallback: str = "hostname-short"  # 'hostname-short' | 'hostname-fqdn'


@dataclass(frozen=True)
class Config:
    host: HostBlock = field(default_factory=HostBlock)
    peers: dict[str, PeerSpec] = field(default_factory=dict)
    source_path: Path | None = None

    def canonical_host(self) -> str:
        """Resolve the local canonical hostname (see module docstring)."""
        env_override = os.environ.get("SAC_HOST")
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
        """Return a list of human-readable errors. Empty = valid."""
        errors: list[str] = []
        for pname, p in self.peers.items():
            for hop in p.via:
                if hop not in self.peers:
                    errors.append(
                        f"peer '{pname}': via=[..., '{hop}', ...] references "
                        f"an unknown peer (define '{hop}' under peers:)"
                    )
            if not p.ssh:
                errors.append(f"peer '{pname}': ssh: is required")
        valid_fallbacks = {"hostname-short", "hostname-fqdn"}
        if self.host.fallback not in valid_fallbacks:
            errors.append(
                f"host.fallback must be one of {sorted(valid_fallbacks)}, "
                f"got {self.host.fallback!r}"
            )
        return errors


def load(path: Path | None = None) -> Config:
    """Read sac.yaml; missing file or empty file yields defaults."""
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if not p.is_file():
        return Config(source_path=p)
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"sac.yaml must be a mapping at top level: {p}")

    host_raw = raw.get("host") or {}
    if not isinstance(host_raw, dict):
        raise ValueError(f"sac.yaml: 'host' must be a mapping: {p}")
    aliases_raw = host_raw.get("aliases") or {}
    if not isinstance(aliases_raw, dict):
        raise ValueError(f"sac.yaml: 'host.aliases' must be a mapping: {p}")
    host = HostBlock(
        canonical=host_raw.get("canonical"),
        aliases={str(k): str(v) for k, v in aliases_raw.items()},
        fallback=host_raw.get("fallback") or "hostname-short",
    )

    peers_raw = raw.get("peers") or {}
    if not isinstance(peers_raw, dict):
        raise ValueError(f"sac.yaml: 'peers' must be a mapping: {p}")
    peers: dict[str, PeerSpec] = {}
    for name, spec in peers_raw.items():
        if not isinstance(spec, dict):
            raise ValueError(
                f"sac.yaml: peer '{name}' must be a mapping with ssh:/via:"
            )
        via_raw = spec.get("via") or []
        if not isinstance(via_raw, list):
            raise ValueError(f"sac.yaml: peer '{name}' via: must be a list")
        peers[str(name)] = PeerSpec(
            name=str(name),
            ssh=str(spec.get("ssh") or ""),
            via=tuple(str(x) for x in via_raw),
        )

    return Config(host=host, peers=peers, source_path=p)


def host_interfaces() -> list[dict]:
    """Best-effort inventory of local network interfaces.

    Surfaced by ``sac host show`` and (eventually) recorded in
    ``state.db.host_interfaces``. Tailscale / wireguard / ssh-tunnel
    detection is heuristic — parses ``ip -j addr`` when available,
    falls back to a single ``hostname -I`` summary on failure.
    """
    import json
    import subprocess

    rows: list[dict] = []
    # stx-allow: fallback (reason: ip(8) missing on macOS / minimal
    # containers; we degrade to a single summary row)
    try:
        out = subprocess.run(
            ["ip", "-j", "addr"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout
        for iface in json.loads(out or "[]"):
            name = iface.get("ifname")
            for ai in iface.get("addr_info", []) or []:
                addr = ai.get("local")
                family = ai.get("family")
                if addr and family in ("inet", "inet6"):
                    rows.append({"iface": name, "addr": addr, "family": family})
    except (
        FileNotFoundError,
        subprocess.SubprocessError,
        ValueError,
    ):  # stx-allow: fallback (reason: see inline comment)
        pass

    if not rows:
        # stx-allow: fallback (reason: hostname -I is universal but
        # collapses every interface; better than nothing)
        try:
            out = subprocess.run(
                ["hostname", "-I"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout
            for addr in (out or "").split():
                rows.append({"iface": "?", "addr": addr, "family": "inet"})
        except (
            FileNotFoundError,
            subprocess.SubprocessError,
        ):  # stx-allow: fallback (reason: see inline comment)
            pass

    return rows
