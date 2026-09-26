"""The optional typed blocks of ``config.yaml`` — ``resolve:``, ``lead:`` and
``scratch_root:``.

Extracted from :mod:`.host_config` so that file stays under the project's
512-line ceiling (it had 8 lines of headroom left, which is not headroom).
Pure extraction — no behaviour change.

Each block here is the same shape: a frozen dataclass plus the one parser
that builds it from raw YAML and raises ``ValueError`` naming the offending
peer/path, so an operator typo surfaces at config-load time rather than as an
opaque ssh or HTTP failure much later. The public import path
``from scitex_agent_container._state.host_config import LeadConfig`` is
preserved by a re-export in :mod:`.host_config`.

:class:`HostBlock` and :class:`PeerSpec` deliberately stay in
:mod:`.host_config`: they are the core schema every caller touches, not
optional sub-blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_RESOLVE_ALLOWED_SOURCES = ("scitex-hpc",)


@dataclass(frozen=True)
class ResolveSpec:
    """Dispatch-time peer-target resolution descriptor (Phase 1 schema).

    Carried on a ``PeerSpec`` via the ``resolve`` field. When set, the peer's
    live ``ssh`` target is to be filled in at dispatch time by querying
    ``source`` (currently only ``"scitex-hpc"``) instead of being pinned
    statically in ``peers.yaml``. Phase 1 of the label-style-peer migration
    only *parses and validates* this field — no resolver code runs. Phase 2
    will wire the lookup into ``try_dispatch``. Full plan in the lead's
    planning doc ``sac-dispatch-time-node-resolution.md``.

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


#: The literal ``scratch_root:`` value that means "this host keeps ``/uvwork``
#: in the apptainer overlay upper" — a WRITTEN decision, never a default.
SCRATCH_ROOT_NONE = "none"


@dataclass(frozen=True)
class ScratchBlock:
    """``scratch_root:`` (+ ``scratch_root_reason:``) — where ``/uvwork`` lives.

    Every agent's ``/uvwork`` (uv itself, the agent venv, ``TMPDIR``, the uv
    cache) is bound from ``<scratch_root>/sac/agents/<agent>/uvwork`` on the
    host (see :mod:`..runtimes._apptainer_scratch`). Without that bind it
    lands in the apptainer overlay upper on the ROOT volume — the volume that
    filled to 0 four times on ``scitex-compute-04`` on 2026-09-02, with
    ``overlays/<agent>/upper/uvwork`` measured at 11.7 GB (sac), 3.3 GB
    (scitex-dev), 3.0 GB (scitex-hub), 2.5 GB (scitex-cards).

    Two shapes, both explicit:

      scratch_root: /scratch              # an ABSOLUTE path that exists
      scratch_root: none                  # keep /uvwork in the overlay ...
      scratch_root_reason: <why>          # ... which needs a stated reason

    A host with NO ``scratch_root:`` line resolves the default ``/scratch``
    when that is a mount point or directory, and REFUSES to start any agent
    otherwise — see :func:`.host_scratch.resolve_scratch_root`. The literal
    ``none`` exists so that keeping ``/uvwork`` on the root volume is a
    decision someone wrote down, not the shape a missing line falls into.
    """

    root: str
    """Absolute path, or the literal :data:`SCRATCH_ROOT_NONE`."""

    reason: str = ""
    """Free text; REQUIRED when ``root`` is ``none``, optional otherwise."""

    @property
    def is_none(self) -> bool:
        return self.root == SCRATCH_ROOT_NONE


def _parse_scratch(raw_root, raw_reason, *, source_path: Path) -> ScratchBlock | None:
    """Normalize ``scratch_root:`` / ``scratch_root_reason:`` into a block.

    Missing ``scratch_root:`` → ``None`` (the resolver then probes the
    default ``/scratch``). Present → strict: a non-empty string that is
    either an absolute path or the literal ``none``; ``none`` additionally
    requires a non-empty ``scratch_root_reason:``. A ``scratch_root_reason:``
    with no ``scratch_root:`` is an orphan and is refused too — it reads as a
    decision that was half-written. Every refusal names ``source_path`` and
    the offending value so the fix is a one-line edit of that file.
    """
    key = "scratch_root"
    if raw_root is None:
        if raw_reason is not None:
            raise ValueError(
                f"config.yaml at {source_path}: 'scratch_root_reason' is set "
                f"but 'scratch_root' is not — the reason belongs to a "
                f"'scratch_root: none' line; add that line or drop the reason"
            )
        return None
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise ValueError(
            f"config.yaml at {source_path}: '{key}' must be an absolute path "
            f"or the literal 'none' (got {raw_root!r})"
        )
    root = raw_root.strip()
    if raw_reason is not None and not isinstance(raw_reason, str):
        raise ValueError(
            f"config.yaml at {source_path}: 'scratch_root_reason' must be a "
            f"string (got {type(raw_reason).__name__})"
        )
    reason = (raw_reason or "").strip()
    if root == SCRATCH_ROOT_NONE:
        if not reason:
            raise ValueError(
                f"config.yaml at {source_path}: '{key}: none' keeps every "
                f"agent's /uvwork in the apptainer overlay upper on this "
                f"host's root volume; that is a written decision and needs a "
                f"'scratch_root_reason: <why>' line next to it"
            )
        return ScratchBlock(root=SCRATCH_ROOT_NONE, reason=reason)
    if not root.startswith("/"):
        raise ValueError(
            f"config.yaml at {source_path}: '{key}' must be an absolute path "
            f"or the literal 'none' (got {root!r})"
        )
    return ScratchBlock(root=root, reason=reason)


__all__ = [
    "LeadConfig",
    "ResolveSpec",
    "SCRATCH_ROOT_NONE",
    "ScratchBlock",
    "_RESOLVE_ALLOWED_SOURCES",
    "_parse_lead",
    "_parse_resolve",
    "_parse_scratch",
]
