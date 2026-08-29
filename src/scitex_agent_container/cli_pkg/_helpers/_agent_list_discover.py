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
from typing import Any, Callable

from . import _agent_list_roots


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

        text = spec_path.read_text()
        # Cheap NECESSARY condition before the expensive parse.
        # is_self_peer_spec requires a `listen_url` key, and a YAML key is
        # written literally in the document defining it -- so text without
        # "listen_url" cannot parse into a mapping that has it. Absence is
        # conclusive, so skipping the parse cannot change the answer.
        # NOT a path check: _self_peers.py says an external orchestrator may
        # drop a self-peer spec anywhere in an agents dir, so keying on the
        # directory name WOULD change behaviour. This keys on content.
        # Measured 2026-08-06: this parsed all ~105 specs to find one marker.
        if "listen_url" not in text:
            return False
        return is_self_peer_spec(yaml.safe_load(text))
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
    roots.extend(_agent_list_roots.user_scope_roots())

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
    group: str | None = None,
    running_only: bool = False,
    discover: Callable[[], list[tuple[str, Path]]] | None = None,
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
    from ._agent_list_beat import beat_is_recent

    # Injected first (caller/test supplies the real collaborator), else the
    # module-attribute seam the existing suite rebinds, else the real walk.
    discover = discover or getattr(
        _al, "_discover_defined_agents", _discover_defined_agents
    )
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
        if capability and not _al._label_capability_matches(labels, capability):
            continue
        if group and not _al._label_group_matches(labels, group):
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
        # An absent registry row is not evidence the agent stopped. If its
        # heartbeat file is STILL BEING WRITTEN it is alive right now, and
        # "defined" would be a flat contradiction of an observable fact —
        # measured on five agents 2026-08-03, one of them this CLI's own
        # host agent, mid-execution. We cannot say "running" (the registry
        # row that would carry pid/session is the thing that is missing),
        # so the honest render is UNKNOWN. Positive-only: a stale beat is
        # left exactly as it was, because SIGKILL leaves a fossil record
        # behind and "old" is not "gone".
        beat_live = beat_is_recent(name) if not errors else None
        if beat_live:
            status = "unknown"
        # PERF: a defined agent with no live beat is never live — in the
        # running-only default view they are all hidden, so skip their
        # account + movement enrichment (status is enough for the footer
        # count). An agent we just promoted to "unknown" IS a candidate for
        # that view, so it must not be deferred.
        deferred = running_only and not beat_live
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
        if beat_live:
            # The shape the remote-probe path already uses for "could not
            # tell", so a JSON consumer needs no new vocabulary to see that
            # liveness is UNRESOLVED here rather than negative.
            row["liveness_unknown"] = True
        if errors:
            row["validation_errors"] = errors
        if labels:
            row["labels"] = labels
        rows.append(row)
    return rows


def _load_or_synthesize_config(spec_path: Path | None, name: str) -> Any:
    """Load ``name``'s spec, or synthesize a bare ``AgentConfig(name=name)``.

    The remote probe only needs a config to derive the agent's tmux session
    name (:func:`_verdict_tmux.session_name_for_config` → ``tui-<name>``). A
    remote agent's spec DOES live on the master's disk (that is how the master
    ssh-dispatched it), so ``load_config`` normally succeeds; the synthesize
    fallback keeps the probe working — never silently skipped — even if the
    spec is momentarily unreadable. Returns ``None`` only if even a bare
    config cannot be built (so the caller degrades to "unknown" — hidden from
    the default view but counted in the footer, never a false "running").
    """
    from ...config import load_config

    if spec_path:
        # stx-allow: fallback (a transiently-unreadable spec must not disable the
        # live probe; fall through to a synthesized name-only config)
        try:
            return load_config(str(spec_path))
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            pass
    # stx-allow: fallback (a bare AgentConfig should always build; if it cannot,
    # the caller maps None -> "unknown" so a probe that could not run is hidden
    # from the default view + counted in the footer, never a false "running")
    try:
        from ...config._types import AgentConfig

        return AgentConfig(name=name)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None


# Remote-instance probing moved to ``_agent_list_remote_rows`` (2026-08-23,
# 512-line cap). Re-exported here so ``_agent_list`` and the
# ``_al.remote_instance_rows`` test seam keep resolving unchanged — the same
# re-export convention this module's header already documents.
#
# The two underscore-prefixed probes are re-exported too, deliberately: the
# suite imports them from HERE (test__agent_list_remote.py:45-48), so they are
# part of this module's de-facto contract regardless of the leading underscore.
# Exporting only the public name broke collection outright — which is the
# useful kind of failure, since it named the contract instead of leaving it to
# be discovered later.
from ._agent_list_remote_rows import (  # noqa: E402,F401
    _default_remote_status_probe,
    _probe_remote_statuses,
    remote_instance_rows,
)
