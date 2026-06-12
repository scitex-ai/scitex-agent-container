"""Self-register external listen sessions as peers via the ``agents/self`` shape.

Operator directive op-2026-06-12-15 (TG 12631 → 12638): the
self-registration is COMPLETELY GENERIC — no special-casing of any
particular session ("lead", "operator", etc.). Any session/node
that owns a listen endpoint registers itself through ONE shape,
ONE code path. The lead/operator listen is one instance of the
pattern; it is NOT a code-level discriminator anywhere.

Convention (literal):

    <search-dir>/self/spec.yaml

    listen_url:  <required>
    description: <optional, shown in peer listings>

The literal ``self`` directory name is the SSoT for "this is the
RUNNING session — register it under MY runtime identity". The peer
NAME is derived from the running listen's own identity (the
canonical host, the listen's configured name, …) — NOT from a
hard-coded ``name:`` field in spec.yaml. Hard-coding would re-
introduce the "lead-specific" coupling the operator just spent two
back-and-forth Telegram messages explicitly rejecting.

For NON-self dirs (``<search-dir>/<other-name>/spec.yaml`` whose
shape still satisfies the self-peer predicate — listen-only YAML)
the name is the directory name. That keeps the convention generic
while still letting the operator drop pre-named pointer specs into
a search dir for nodes that are NOT "the running session".

Public surface:

* :func:`is_self_peer_spec(blob)` — pure predicate. ``True`` iff
  the parsed YAML carries ``listen_url`` AND has neither ``spec``
  nor ``apiVersion`` (the container-agent gates).

* :func:`load_self_peer(path, *, self_identity=None)` — read one
  spec file, return a peer dict in the same shape
  :meth:`Registry.list_all` returns. Name derivation:

    - Dir literally ``self`` AND ``self_identity`` provided →
      ``name = self_identity``.
    - Dir literally ``self`` AND no ``self_identity`` →
      ``name = "self"`` (degraded — the caller did not pass
      runtime identity; logged at ``warning``).
    - Any other dir → ``name = <dirname>``.

* :func:`discover_self_peers(search_dirs, *, self_identity=None)`
  — walk a sequence of agent-base directories, return every
  self-peer in deterministic ``name`` order. The ``self_identity``
  argument is forwarded to :func:`load_self_peer` so the literal
  ``self`` dir resolves to the running listen's identity at scan
  time. Search dirs are INJECTED so tests drive discovery without
  touching ``$HOME``.

Out of scope (deferred):

* Pull-side MCP registration (TG 12633 follow-up — the listening
  node's sac MCP CLIENT should ALSO surface itself so a peer's
  ``a2a_peers`` listing reports it even without the listen push).
  Sibling module under ``_mcp/``; this module owns the push side.
* Cross-host comms_nodes UPSERT —
  :mod:`_mcp._channel_self_register` already covers the channel
  path. This module is the listen-side analogue.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping, Sequence

log = logging.getLogger(__name__)


# The two keys that mark a YAML as "container-agent" rather than
# "self-peer". Presence of EITHER excludes the file from self-peer
# discovery — the self-peer shape must be the minimal listen-pointer,
# not a partial container spec.
_CONTAINER_AGENT_KEYS: frozenset[str] = frozenset({"spec", "apiVersion"})

# The literal directory name that means "register the running
# session under its runtime identity". Any other dir name is taken
# as the peer's intended name verbatim.
_SELF_DIR_LITERAL = "self"


def is_self_peer_spec(blob: Mapping[str, object] | None) -> bool:
    """Return True iff *blob* is a self-peer spec (``listen_url`` + no container keys).

    A self-peer spec MUST:

    * be a mapping (PyYAML returns ``None`` for an empty document
      and a non-mapping for ``--- 42``-style scalars — both are
      rejected as malformed self-peer specs).
    * declare a non-empty ``listen_url`` string.
    * NOT declare ``spec`` or ``apiVersion`` — those are the
      container-agent gates.

    Pure → testable without filesystem. The listen-side merge calls
    this to short-circuit obviously-not-a-peer specs that an
    external orchestrator may have dropped into an agents dir.
    """
    if not isinstance(blob, Mapping):
        return False
    listen_url = blob.get("listen_url")
    if not isinstance(listen_url, str) or not listen_url.strip():
        return False
    for k in _CONTAINER_AGENT_KEYS:
        if k in blob:
            return False
    return True


def load_self_peer(path: Path, *, self_identity: str | None = None) -> dict | None:
    """Read one spec file and return a peer dict, or ``None``.

    Name derivation (TG 12637/12638 — generic):

    * Dir literally :data:`_SELF_DIR_LITERAL` AND *self_identity*
      provided → ``name = self_identity``.
    * Dir literally :data:`_SELF_DIR_LITERAL` AND *self_identity*
      missing → ``name = "self"`` plus a ``log.warning`` so the
      operator can tell the listen forgot to pass its runtime
      identity into the discovery pipeline.
    * Any other dir → ``name = path.parent.name``.

    Return shape mirrors :meth:`Registry.list_all`'s entries so the
    listen-side handler concatenates the two lists with no
    field-by-field re-projection:

        {
            "name":        <derived as above>,
            "config":      <absolute path to the spec.yaml>,
            "listen_url":  <yaml.listen_url>,
            "description": <yaml.description, or "">,
            "kind":        "self-peer",
        }

    ``kind`` is the discriminator a peer-aware client uses to skip
    the container-only fields (``pid``, ``screen``, ``started_at``)
    that would be meaningless for a self-peer.

    Failure modes (all return ``None`` after one ``log.warning``):

    * OS read error (permissions, vanished file).
    * Malformed YAML.
    * Document fails :func:`is_self_peer_spec` (not a self-peer).

    ``None`` rows are filtered before concat by
    :func:`discover_self_peers`.
    """
    try:
        import yaml
    except ImportError:  # pragma: no cover — PyYAML is a hard sac dep
        log.warning("self-peer: PyYAML unavailable; skipping %s", path)
        return None
    try:
        text = path.read_text()
    except OSError as exc:
        log.warning("self-peer: cannot read %s: %s", path, exc)
        return None
    try:
        blob = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        log.warning("self-peer: malformed YAML at %s: %s", path, exc)
        return None
    if not is_self_peer_spec(blob):
        return None
    # ``is_self_peer_spec`` already validated the type — assert for type-checkers.
    assert isinstance(blob, Mapping)

    dir_name = path.parent.name
    if dir_name == _SELF_DIR_LITERAL:
        if self_identity and self_identity.strip():
            name = self_identity
        else:
            log.warning(
                "self-peer: %s lives in literal 'self/' dir but no "
                "self_identity was supplied; falling back to dir name "
                "(peer listing will show 'self' rather than the "
                "running session's runtime identity)",
                path,
            )
            name = _SELF_DIR_LITERAL
    else:
        name = dir_name

    description = blob.get("description", "")
    if not isinstance(description, str):
        description = ""
    return {
        "name": name,
        "config": str(path),
        "listen_url": blob["listen_url"],
        "description": description,
        "kind": "self-peer",
    }


def discover_self_peers(
    search_dirs: Sequence[Path], *, self_identity: str | None = None
) -> list[dict]:
    """Walk *search_dirs*, return every self-peer dict in name order.

    Each entry in ``search_dirs`` is treated as a base directory
    holding ``<name>/spec.yaml`` (and ``.yml``) — the same shape
    :func:`config._resolve._try_dir` consumes. A directory that does
    not exist is silently skipped (the listen may start before the
    operator has populated every search root).

    ``self_identity`` is the running listen's runtime identity (its
    canonical name, the only authoritative answer to "who am I").
    It is forwarded to :func:`load_self_peer` so a literal
    ``agents/self/spec.yaml`` resolves to the running session's
    identity at scan time — that's what makes the convention
    generic. ``None`` is accepted and degrades gracefully (see
    :func:`load_self_peer`).

    Deduplication: the same derived ``name`` MAY appear in multiple
    search dirs (a project-local override + a home-install
    fallback). The HIGHEST-priority dir wins (earlier in
    ``search_dirs``), matching :func:`config._resolve._search_dirs`
    precedence so the same override semantics apply to both "where
    does sac find the spec" and "what does sac a2a peers report".

    Return order is alphabetical by derived ``name`` — operator-
    facing listings never reflect filesystem walk order.
    """
    seen: dict[str, dict] = {}
    for base in search_dirs:
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            for ext in (".yaml", ".yml"):
                spec_path = child / f"spec{ext}"
                if spec_path.is_file():
                    peer = load_self_peer(spec_path, self_identity=self_identity)
                    if peer is not None and peer["name"] not in seen:
                        seen[peer["name"]] = peer
                    break  # one spec file per agent dir
    return sorted(seen.values(), key=lambda p: p["name"])


__all__ = [
    "discover_self_peers",
    "is_self_peer_spec",
    "load_self_peer",
]
