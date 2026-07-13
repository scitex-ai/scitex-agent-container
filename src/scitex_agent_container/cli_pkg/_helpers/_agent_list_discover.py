"""Defined-on-disk agent discovery for ``sac agents list``.

Split out of ``_agent_list.py`` (512-line cap) so the filesystem-walk concern
lives beside its siblings ``_agent_list_account`` / ``_agent_list_render`` /
``_agent_list_host``. ``_agent_list`` re-imports both names, so the bare-name
call site ``_discover_defined_agents()`` in ``get_agent_list_data`` and the
test seams ``_al._discover_defined_agents`` / ``_al._is_self_peer_marker``
keep resolving unchanged.
"""

from __future__ import annotations

from pathlib import Path


def _is_self_peer_marker(spec_path: Path) -> bool:
    """Return True iff ``spec_path`` is a self-peer registration marker.

    ``agents/self/spec.yaml`` (see ``_listen/_self_peers.py``) is a
    DELIBERATELY schema-incompatible file — it registers the running
    listen's own runtime identity, not a launchable Agent, and its own
    header says ``DO NOT add apiVersion or spec:``. Running the generic
    Agent validator against it always reports it "invalid" (missing
    apiVersion/kind/spec, unknown top-level fields) even though it is
    working exactly as designed. Reuses the SAME predicate the listen
    merge already uses to recognize this file, so there is one place
    that knows what a self-peer marker looks like.

    Tolerant: any read/parse failure returns False (falls through to
    normal defined-agent handling) rather than raising — matches this
    module's existing crash-tolerance convention.
    """
    # stx-allow: fallback (reason: classification hiccup must not hide or
    # misclassify a spec; falling through to normal validation is safe)
    try:
        import yaml

        from ..._listen._self_peers import is_self_peer_spec

        blob = yaml.safe_load(spec_path.read_text())
        return is_self_peer_spec(blob)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return False


def _discover_defined_agents() -> list[tuple[str, Path]]:
    """Walk the user-scope (and project-scope, when in a git repo)
    ``agents/`` tree and return ``(name, spec.yaml path)`` pairs for
    every agent declared on disk. Tolerant of partial state — a
    directory without a ``spec.yaml`` is skipped silently. Self-peer
    registration markers (see :func:`_is_self_peer_marker`) are NOT
    agents and are excluded here at the source, rather than surfacing
    as a spuriously "invalid" agent downstream.
    """
    pairs: list[tuple[str, Path]] = []
    seen: set[str] = set()

    roots: list[Path] = []
    # stx-allow: fallback (project-scope is optional; absent → skip)
    try:
        from scitex_config._ecosystem import local_state as _ls

        project = _ls.find_project_scope("agent-container")
        if project is not None:
            roots.append(project / "agents")
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        pass
    roots.append(Path.home() / ".scitex" / "agent-container" / "agents")

    for root in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name in seen:
                continue
            spec = child / "spec.yaml"
            if not spec.is_file():
                continue
            if _is_self_peer_marker(spec):
                continue
            pairs.append((child.name, spec))
            seen.add(child.name)
    return pairs
