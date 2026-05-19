"""Per-host bearer registry for cross-host forwarding (WI-4 / Q4(b)).

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-4) and the lead's
2026-05-21 Q4 directive ("Option (b) — per-host bearer registry.
Aligns with peer.py's host-registry pattern and gives per-host
blast radius."):

  * Each host stores ``peer-tokens/<peer-host>.token`` populated by
    ``sac host add-peer <host> <token>`` (CLI to add).
  * The cross-host forwarder calls peer host with **the destination's
    bearer** pulled from that registry, not the forwarding host's
    own bearer.
  * Per-host blast radius: leaking host A's listen bearer compromises
    only host A (not the whole fleet, as the shared-fleet model
    would).
  * Loud failure when the registry is missing the destination's
    token — never silently drop a forward.

File layout::

    ~/.scitex/agent-container/peer-tokens/
        host-a.token       # 0600 — host A's listen bearer, used to
                           # authenticate at host A when forwarding
                           # there from anywhere else in the fleet.
        host-b.token
        ...

The plain filename match keeps the registry trivially auditable
(``ls peer-tokens/`` shows the trust topology) and matches the
existing ``listen-<host>.token`` shape one level up.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "PeerTokenError",
    "default_peer_tokens_dir",
    "read_peer_token",
    "write_peer_token",
    "list_peer_hosts",
]


class PeerTokenError(RuntimeError):
    """Raised when the peer-token registry can't satisfy a lookup.

    Loud-by-design (handoff §0): a missing peer-token is a *config*
    failure that breaks cross-host messaging; the operator needs to
    see it, not have it silently absorbed.
    """


def default_peer_tokens_dir(home: Path | None = None) -> Path:
    """Return the canonical ``peer-tokens/`` directory.

    Honours ``$SCITEX_AGENT_CONTAINER_RUNTIME_DIR`` if set (matches
    the rest of sac's path resolution); otherwise lands under
    ``~/.scitex/agent-container/peer-tokens``.

    NOTE: this is **not** a runtime dir — it's a config / secret
    dir, parallel to ``~/.scitex/agent-container/tokens/`` (the
    local listen token). Both sit under
    ``~/.scitex/agent-container/`` so a single backup pass covers
    them.
    """
    base = home if home is not None else Path.home()
    return base / ".scitex" / "agent-container" / "peer-tokens"


def write_peer_token(
    *,
    peer_host: str,
    token: str,
    tokens_dir: Path | None = None,
) -> Path:
    """Write ``token`` to ``<tokens_dir>/<peer_host>.token`` (mode 0600).

    Atomic via tmpfile + rename. Returns the destination path so
    callers can echo it. Raises ``PeerTokenError`` on validation
    failure (empty inputs).
    """
    if not peer_host:
        raise PeerTokenError("write_peer_token: peer_host must be non-empty")
    if not token:
        raise PeerTokenError("write_peer_token: token must be non-empty")
    tdir = tokens_dir if tokens_dir is not None else default_peer_tokens_dir()
    tdir.mkdir(parents=True, exist_ok=True)
    dst = tdir / f"{peer_host}.token"
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.write_text(token, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(dst)
    return dst


def read_peer_token(
    *,
    peer_host: str,
    tokens_dir: Path | None = None,
) -> str:
    """Return the bearer token for ``peer_host``.

    Raises :class:`PeerTokenError` with an actionable message when
    the file is missing — the cross-host forwarder turns this into a
    502 with the same message so the operator sees exactly which
    peer-token is missing.
    """
    if not peer_host:
        raise PeerTokenError("read_peer_token: peer_host must be non-empty")
    tdir = tokens_dir if tokens_dir is not None else default_peer_tokens_dir()
    src = tdir / f"{peer_host}.token"
    if not src.is_file():
        raise PeerTokenError(
            f"no peer token for host {peer_host!r} at {src}. "
            f"Add one with: sac host add-peer {peer_host} <token>"
        )
    try:
        token = src.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PeerTokenError(
            f"could not read peer token at {src}: {exc}"
        ) from exc
    if not token:
        raise PeerTokenError(
            f"peer token file {src} is empty. "
            f"Re-add with: sac host add-peer {peer_host} <token>"
        )
    return token


def list_peer_hosts(tokens_dir: Path | None = None) -> list[str]:
    """Return the sorted list of peer hosts that have a registered
    token (filenames stripped of the ``.token`` suffix).

    Observability surface for the operator. Token VALUES are never
    returned — that would defeat the purpose of storing them as
    secrets.
    """
    tdir = tokens_dir if tokens_dir is not None else default_peer_tokens_dir()
    if not tdir.is_dir():
        return []
    hosts: list[str] = []
    for child in tdir.iterdir():
        if child.suffix == ".token" and child.is_file():
            hosts.append(child.stem)
    return sorted(hosts)
