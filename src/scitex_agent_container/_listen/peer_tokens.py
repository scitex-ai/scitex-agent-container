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
import tempfile
from ipaddress import IPv6Address
from pathlib import Path, PureWindowsPath

__all__ = [
    "PeerTokenError",
    "peer_token_path",
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

    Defaults to ``Path.home()``; an explicit ``home`` selects another home.
    This is configuration, not instance runtime state. The configured registry
    directory may be a symlink, but token entries must be regular files.
    """
    base = home if home is not None else Path.home()
    return base / ".scitex" / "agent-container" / "peer-tokens"


def peer_token_path(peer_host: str, tokens_dir: Path | None = None) -> Path:
    """Resolve one credential entry without admitting paths or token symlinks.

    Host aliases, FQDNs and IP literals remain single filenames. Reject POSIX
    and Windows separators/drive prefixes even on a different host platform.
    Validation does not create the directory or read any credential value.
    """
    if (
        not isinstance(peer_host, str)
        or not peer_host
        or peer_host.strip() != peer_host
        or peer_host in {".", ".."}
        or any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in peer_host)
    ):
        raise PeerTokenError(
            "peer_host must be a single non-empty host identifier, not a path"
        )
    if PureWindowsPath(peer_host).drive:
        try:
            IPv6Address(peer_host)
        except ValueError as exc:
            raise PeerTokenError("peer_host must not contain a drive prefix") from exc
    root = Path(tokens_dir) if tokens_dir is not None else default_peer_tokens_dir()
    filename = f"{peer_host}.token"
    path = root / filename
    if path.name != filename:
        raise PeerTokenError("peer_host must resolve to one filename")
    try:
        if path.is_symlink():
            raise PeerTokenError("peer token entries must not be symbolic links")
        if path.resolve().parent != root.resolve():
            raise PeerTokenError("peer token entry escapes the configured registry")
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, PeerTokenError):
            raise
        raise PeerTokenError("cannot resolve peer token registry path") from exc
    return path


def write_peer_token(
    *,
    peer_host: str,
    token: str,
    tokens_dir: Path | None = None,
) -> Path:
    """Write ``token`` to ``<tokens_dir>/<peer_host>.token`` (mode 0600).

    Atomic via a unique private staging file and rename. Returns the destination
    path so callers can report it. Raises ``PeerTokenError`` for empty inputs,
    unsafe host identifiers or token symlinks. Directory symlinks remain valid.
    """
    if not peer_host:
        raise PeerTokenError("write_peer_token: peer_host must be non-empty")
    if not token:
        raise PeerTokenError("write_peer_token: token must be non-empty")
    dst = peer_token_path(peer_host, tokens_dir)
    dst.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=".peer-token-", suffix=".tmp", dir=dst.parent
    )
    tmp = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), 0o600)
            else:
                os.chmod(tmp, 0o600)
            stream.write(token)
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)
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
    src = peer_token_path(peer_host, tokens_dir)
    if not src.is_file():
        raise PeerTokenError(
            f"no peer token for host {peer_host!r} at {src}. "
            f"Add one with: sac host add-peer {peer_host} <token>"
        )
    try:
        descriptor = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            token = stream.read().strip()
    except OSError as exc:
        raise PeerTokenError(f"could not read peer token at {src}: {exc}") from exc
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
    secrets. Unsafe or symlink token entries raise ``PeerTokenError`` rather
    than reporting an incomplete registry as healthy.
    """
    tdir = tokens_dir if tokens_dir is not None else default_peer_tokens_dir()
    if not tdir.is_dir():
        return []
    hosts: list[str] = []
    for child in tdir.iterdir():
        if child.suffix == ".token":
            path = peer_token_path(child.stem, tdir)
            if path.is_file():
                hosts.append(child.stem)
    return sorted(hosts)
