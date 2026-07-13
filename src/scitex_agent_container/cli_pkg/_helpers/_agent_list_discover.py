"""Defined-on-disk agents for ``sac agents list`` — discovery AND their rows.

Split out of ``_agent_list.py`` (512-line cap) so the filesystem-walk concern
lives beside its siblings ``_agent_list_account`` / ``_agent_list_render`` /
``_agent_list_host``. ``_agent_list`` re-imports both names, so the bare-name
call site ``_discover_defined_agents()`` in ``get_agent_list_data`` and the
test seams ``_al._discover_defined_agents`` / ``_al._is_self_peer_marker``
keep resolving unchanged.

:func:`defined_agent_rows` (moved here when the auth-status column pushed
``_agent_list`` over the cap) turns those discovered ``(name, spec)`` pairs into
list rows — the same concern, one module: DISCOVER the on-disk agents, then
BUILD their rows. ``get_agent_list_data`` stays the orchestrator that merges
them with the registry's rows.
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


def defined_agent_rows(
    *,
    registered: set[str],
    port_claims: dict[str, int],
    display_host: str,
    capability: str | None = None,
    machine: str | None = None,
    tags: str | None = None,
    running_only: bool = False,
) -> list[dict]:
    """Rows for agents DEFINED on disk but absent from the registry.

    The filesystem is the canonical "defined" surface; the registry is only a
    runtime cache of started/stopped state. An agent that was deleted from the
    registry (or has never been started) must still appear, or the operator
    cannot see it exists.

    While walking, each spec is yaml-validated: a broken one surfaces as
    ``status="invalid"`` rather than hiding, so the operator learns it will not
    start BEFORE they hit a confusing ``sac agents start`` traceback.

    These agents are never LIVE, so they carry the never-checked auth shape —
    a cached "auth failed" verdict describes a *running process*, and replaying
    it for an agent nobody is running would fill the fleet view with alarms
    about agents that do not exist. Keys stay present-but-empty so the
    ``--json`` row shape is uniform across every row.

    Helpers are resolved through the ``_agent_list`` module namespace at call
    time (rather than imported by value) so the real-attribute test seams —
    ``_al._safe_account_for`` / ``_al._movement_fields`` / etc., which the suite
    rebinds instead of mocking — keep working after this move. The
    function-level import also avoids a cycle: ``_agent_list`` imports THIS
    module at module scope.
    """
    from ..._state.auth_state import verdict_for
    from ...config import load_config
    from ...config._validation import validate_config
    from . import _agent_list as _al

    discover = getattr(_al, "_discover_defined_agents", _discover_defined_agents)
    rows: list[dict] = []
    for name, spec_path in discover():
        if name in registered:
            continue
        labels: dict[str, str] = {}
        cfg = None
        # stx-allow: fallback (defined-row labels are best-effort; a
        # broken yaml still surfaces with status=invalid + empty labels)
        try:
            cfg = load_config(str(spec_path))
            labels = cfg.labels
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            pass
        if machine and labels.get("machine") != machine:
            continue
        if capability:
            caps = [
                c.strip()
                for c in labels.get("capabilities", "").split(",")
                if c.strip()
            ]
            if capability not in caps:
                continue
        if tags and not _al._label_list_contains(labels, "tags", tags):
            continue
        # FIX (no double-parse): a spec that ``load_config`` accepted is
        # valid ⇒ "defined". Only RE-VALIDATE the ones that FAILED to load,
        # to split "invalid" from "defined" and recover their error list.
        errors: list[str] = []
        if cfg is None:
            # stx-allow: fallback (validator may raise on unparseable yaml;
            # treat as "invalid" with the exception text as the only error)
            try:
                errors = validate_config(str(spec_path))
            except Exception as exc:  # stx-allow: fallback (reason: see comment)
                errors = [str(exc)]
        status = "invalid" if errors else "defined"
        # PERF: defined agents are never live — in the running-only default
        # view they are all hidden, so skip their account + movement
        # enrichment (status is enough for the footer count).
        deferred = running_only
        row: dict = {
            "name": name,
            "status": status,
            "screen": "-",
            "multiplexer": getattr(cfg, "runtime", None) if cfg else None,
            "started_at": "-",
            "host": "local",
            "host_display": _al._host_display_for("local", display_host),
            "path": str(spec_path),
            "a2a_port": port_claims.get(name),
            "account": "" if deferred else _al._safe_account_for(cfg),
        }
        movement = (
            dict(_al._MOVEMENT_DEFAULTS) if deferred else _al._movement_fields(name)
        )
        row.update(movement)
        row.update(verdict_for(None))
        if errors:
            row["validation_errors"] = errors
        if labels:
            row["labels"] = labels
        rows.append(row)
    return rows
