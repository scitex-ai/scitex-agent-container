"""Pull-side self-peer discovery for ``sac mcp channel`` (TG 12706, #356 follow-up).

The listen-side analogue lives in
:mod:`scitex_agent_container._listen._self_peers` and registers
external listen sessions as peers via the generic ``agents/self/``
shape (op-2026-06-12-15). This module is the symmetric pull-side
piece: when ``sac mcp channel`` starts WITHOUT ``--name``, it
discovers "who am I" from the running session's
``.scitex/agent-container/agents/self/spec.yaml`` by walking the
current working directory UPWARD.

ONE GENERIC SHAPE
-----------------

The discovery convention is:

    <cwd-or-any-ancestor>/.scitex/agent-container/agents/self/spec.yaml

That is the ONLY path searched. There is no per-node home-scope
fallback — the operator rejected node-specific exceptions per a2a
message ``a8580f78125f44b1ad89442794ad3dce``. Every node (lead,
operator, peer, capsule, …) drops the same shape under its own
project root. Discovery walks upward from cwd and takes the first
hit.

PREDICATE PARITY
----------------

The YAML is validated through
:func:`scitex_agent_container._listen._self_peers.is_self_peer_spec`
— the same gate the listen-side discovery uses. A spec that fails
the predicate (carries ``apiVersion`` / ``spec:``, or omits
``listen_url``) is silently skipped, matching the listen-side
behaviour exactly.

NAME RESOLUTION
---------------

* Explicit ``self_identity`` arg → used verbatim.
* Else: call
  :func:`scitex_agent_container._listen.server._resolve_runtime_self_identity`
  (the same resolver the listen-side ``_self_peers`` consults at
  scan time, so the channel and the listen agree on "who am I").
* Else: fall back to the literal string ``"self"`` and emit a
  ``logging.WARNING`` so the operator sees the gap.

FAILURE MODES
-------------

Everything returns ``None`` (never raises):

* No spec found anywhere upward from ``start``.
* Predicate rejects the YAML (container-agent shape, missing
  ``listen_url``, malformed mapping).
* YAML parse error (logged at WARNING).
* File unreadable / permissions error (logged at WARNING).

The caller (``sac mcp channel``) is responsible for raising an
actionable error when ``--name`` was omitted AND discovery returned
``None`` — naming the spec-path convention in the error message so
the operator knows where to drop the file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# Type alias for the runtime-identity resolver — a zero-arg callable that
# returns the running session's runtime identity (``host_config.lead.name``
# in production) or ``None`` if no identity is configured. Injectable into
# :func:`discover_self_identity` to avoid monkeypatching the listen-side
# module in tests (PA-306 §3 no-mocks) and to keep a clean single
# collaborator boundary.
RuntimeIdentityResolver = Callable[[], "str | None"]


# Relative path under each cwd ancestor that the discovery searches.
# SSoT for the convention — every node uses this literal path.
_SPEC_REL = Path(".scitex/agent-container/agents/self/spec.yaml")


@dataclass(frozen=True)
class DiscoveredSelfIdentity:
    """Result of a successful cwd-walk self-peer discovery.

    Fields:

    * ``name`` — the resolved peer name (explicit > runtime identity >
      literal ``"self"``).
    * ``listen_url`` — the ``listen_url`` field from the discovered
      spec; the caller may override it with an explicit ``--listen-url``
      / ``$SAC_LISTEN_BASE_URL``.
    * ``spec_path`` — the absolute path to the discovered spec file,
      surfaced for diagnostics (``sac doctor``, error messages).
    * ``description`` — the optional ``description:`` field from the
      spec, or ``None`` if absent / non-string.
    """

    name: str
    listen_url: str
    spec_path: Path
    description: str | None


def _walk_upward_for_spec(start: Path) -> Path | None:
    """Walk ``start`` upward, return the first ``_SPEC_REL`` hit or None."""
    current = start.resolve()
    while True:
        candidate = current / _SPEC_REL
        if candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _default_runtime_resolver() -> str | None:
    """Default runtime-identity resolver — defers to the listen-side gate.

    Kept separate from :func:`_resolve_name` so callers (and tests) can
    inject an alternative resolver via the ``runtime_resolver`` parameter
    on :func:`discover_self_identity` without monkeypatching the listen
    module. The import is lazy on purpose: callers who pass an explicit
    ``self_identity`` never pay the listen-server import cost, and
    callers who inject their own resolver never touch the listen module
    at all.
    """
    try:
        from .._listen.server import _resolve_runtime_self_identity
    except Exception:  # stx-allow: fallback (reason: listen module import must not block channel startup; surface as None and let _resolve_name degrade)
        log.warning(
            "sac channel self-discovery: cannot import "
            "_listen.server._resolve_runtime_self_identity; "
            "falling back to literal 'self'"
        )
        return None
    return _resolve_runtime_self_identity()


def _resolve_name(
    self_identity: str | None,
    runtime_resolver: RuntimeIdentityResolver | None = None,
) -> str:
    """Resolve the peer name via the explicit → resolver → literal chain.

    Mirrors the listen-side gate so the channel and the listen agree on
    "who am I". A missing ``self_identity`` AND a ``None`` from the
    runtime resolver degrade to the literal ``"self"`` with a WARNING
    log — the same degraded shape
    :func:`_listen._self_peers.load_self_peer` produces, by design.

    ``runtime_resolver`` is injected by callers (and tests — PA-306 §3
    no-mocks) when they want to override the default lazy-import path to
    :func:`_listen.server._resolve_runtime_self_identity`. ``None``
    keeps the production default.
    """
    if self_identity and self_identity.strip():
        return self_identity
    resolver: RuntimeIdentityResolver = runtime_resolver or _default_runtime_resolver
    try:
        resolved = resolver()
    except Exception:  # stx-allow: fallback (reason: identity resolver errors must never block channel startup; degrade with a warning)
        log.warning(
            "sac channel self-discovery: runtime identity resolver "
            "raised; falling back to literal 'self'"
        )
        return "self"
    if isinstance(resolved, str) and resolved.strip():
        return resolved
    log.warning(
        "sac channel self-discovery: no runtime identity (host_config "
        "lead.name unset); falling back to literal 'self' — the peer "
        "listing will show 'self' rather than the running session's "
        "runtime identity"
    )
    return "self"


def discover_self_identity(
    start: Path | None = None,
    *,
    self_identity: str | None = None,
    runtime_resolver: RuntimeIdentityResolver | None = None,
) -> DiscoveredSelfIdentity | None:
    """Discover the running session's identity via cwd-walk for the self spec.

    Walks ``start`` (default :func:`pathlib.Path.cwd`) UPWARD for the
    first ``.scitex/agent-container/agents/self/spec.yaml`` hit. The
    YAML is gated through
    :func:`_listen._self_peers.is_self_peer_spec` (predicate parity
    with the listen-side discovery). The name is resolved by
    :func:`_resolve_name`: explicit arg > runtime resolver > literal
    ``"self"`` (with a WARNING log).

    Parameters
    ----------
    start:
        Directory to start the upward walk from. Defaults to ``cwd``.
    self_identity:
        Explicit runtime identity supplied by the caller (typically
        ``sac mcp channel --name``). Wins over the resolver.
    runtime_resolver:
        Optional zero-arg callable returning the running session's
        runtime identity (``host_config.lead.name`` in production) or
        ``None``. ``None`` (the default) defers to
        :func:`_default_runtime_resolver`, which lazy-imports
        :func:`_listen.server._resolve_runtime_self_identity`. Tests
        and embedders inject their own resolver here instead of
        monkeypatching the listen module — see PA-306 §3 no-mocks.

    Returns ``None`` on every failure mode (never raises):

    * No spec anywhere upward.
    * Predicate rejects (container-agent shape, missing
      ``listen_url``, malformed mapping).
    * YAML parse error (logged at WARNING).
    * File unreadable (logged at WARNING).
    """
    start_path = Path(start) if start is not None else Path.cwd()
    spec_path = _walk_upward_for_spec(start_path)
    if spec_path is None:
        return None
    try:
        import yaml
    except ImportError:  # pragma: no cover — PyYAML is a hard sac dep
        log.warning(
            "sac channel self-discovery: PyYAML unavailable; skipping %s",
            spec_path,
        )
        return None
    try:
        text = spec_path.read_text()
    except OSError as exc:
        log.warning("sac channel self-discovery: cannot read %s: %s", spec_path, exc)
        return None
    try:
        blob = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        log.warning(
            "sac channel self-discovery: malformed YAML at %s: %s",
            spec_path,
            exc,
        )
        return None

    # Predicate parity with the listen-side discovery — same gate, same
    # rejections, same generic shape.
    from .._listen._self_peers import is_self_peer_spec

    if not is_self_peer_spec(blob):
        return None

    # ``is_self_peer_spec`` already validated the mapping + listen_url
    # shape; safe to index.
    assert isinstance(blob, dict)
    listen_url = blob["listen_url"]
    raw_description = blob.get("description")
    description = (
        raw_description
        if isinstance(raw_description, str) and raw_description
        else None
    )

    name = _resolve_name(self_identity, runtime_resolver=runtime_resolver)

    return DiscoveredSelfIdentity(
        name=name,
        listen_url=listen_url,
        spec_path=spec_path,
        description=description,
    )


__all__ = [
    "DiscoveredSelfIdentity",
    "discover_self_identity",
]
