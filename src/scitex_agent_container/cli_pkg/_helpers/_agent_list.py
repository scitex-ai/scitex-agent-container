"""Agent-list DATA ASSEMBLY (probes, labels, movement, discovery).

The READER-facing presentation (JSON dump + rich-table) lives in the
sibling :mod:`_agent_list_render` (split for the 512-line per-file cap);
its public names — ``print_agent_list`` / ``print_agent_list_json`` /
``_is_ghost_row`` / ``_extract_damaged_fields`` — are re-exported at the
bottom of this module so existing ``from ._agent_list import ...``
importers are unchanged.
"""

from __future__ import annotations

from ..._state.registry import Registry
from ...config import load_config

# Account-column resolvers live in the sibling ``_agent_list_account``
# (512-line cap split). Re-imported here so the bare-name call sites in
# ``get_agent_list_data`` — and the test seams that rebind
# ``_al._safe_account_for`` / ``_al._runtime_account_for`` — keep working.
from ._agent_list_account import (  # noqa: F401
    _runtime_account_for,
    _safe_account_for,
)


def _safe_port_for(name: str) -> int | None:
    """Return the agent's claimed a2a port, or None on any failure.

    Used by Layer-6 of auto-port-allocation to surface the allocated
    port in ``sac agents list`` output. Tolerant: a missing state.db,
    schema-not-yet-initialized error, or unknown name all map to
    ``None`` so the list command never fails because of port lookup.
    """
    # stx-allow: fallback (reason: list output must never crash on a
    # port-allocator hiccup; ``None`` cell rendered as ``—`` is the
    # right UX.)
    try:
        from ..._state import port_allocator

        return port_allocator.get_port(name)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None


def _movement_fields(name: str) -> dict:
    """Return the three movement keys for ``name`` (always all-present).

    Operator mandate (lead a2a 1781e82a, 2026-06-14): the fleet view's
    per-agent rows must carry the same ``session_jsonl_bytes`` /
    ``session_jsonl_last_write`` / ``heartbeat_at`` trio that the
    per-agent ``agent_status`` payload exposes, so a single fleet
    ``--json`` read answers "is each agent producing?".

    Tolerant: any state-dir resolution / IO failure degrades to the
    all-defaults shape (zero bytes + empty ISO strings) so the list
    command never crashes on a movement-probe hiccup.
    """
    # stx-allow: fallback (reason: list output must never crash on a
    # state-dir probe hiccup; explicit empty-values shape is the right UX.)
    try:
        from ..._lifecycle._session_movement import (
            resolve_state_dir,
            status_movement_fields,
        )

        return status_movement_fields(resolve_state_dir(name))
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return {
            "session_jsonl_bytes": 0,
            "session_jsonl_last_write": "",
            "heartbeat_at": "",
        }


def _probe_local(cfg) -> bool | None:
    """Probe an agent's liveness via its DECLARED runtime adapter.

    Must select the SAME runtime ``agent_status`` uses
    (:func:`_lifecycle._runtime_select._get_runtime`, which branches on
    ``spec.runtime``) — NOT a hardcoded ``ClaudeSessionRuntime``. That
    hardcode was the root cause of "live agent reads stopped" (fix
    ``liveness-live-agents-read-stopped``): the DEFAULT runtime is
    ``tui``, whose liveness is the ``tui-<name>`` tmux session's
    pane-activity (``TuiSessionRuntime.is_running``). Probing a live TUI
    agent through ``ClaudeSessionRuntime`` instead reached
    ``ApptainerContainerRuntime.is_running`` → ``os.kill(read_pid, 0)``,
    but a TUI agent NEVER writes an ``apptainer_pid`` file (it launches
    via tmux, not ``subprocess.Popen`` with a pidfile), so ``_read_pid``
    returned ``None`` → ``is_running`` returned ``False`` → status
    "stopped" for a provably-running agent. Routing through
    ``_get_runtime`` makes ``sac agents list`` agree with
    ``sac agents status``.

    SECOND fix (card ``sac-fix-live-agents-read-stopped``, 2026-07-08):
    even after routing here, ``TuiSessionRuntime.is_running`` still gated
    on ``session_activity`` freshness (pane I/O within 300s). tmux
    advances that stamp only on pane read/write, so every quiet-but-alive
    agent sitting at its input prompt read "stopped" minutes after its
    last output. ``is_running`` is now IDENTITY-based liveness (session
    exists AND its pane process is alive via ``os.kill(pane_pid, 0)``),
    so this probe reports a live idle agent as running.

    Returns None on exception (e.g. malformed config) so the caller
    surfaces ``status='unknown'`` rather than crashing the list.
    """
    # stx-allow: fallback (reason: runtime may be missing or state-dir may
    # not exist for an agent that never ran; either case maps to
    # liveness_unknown rather than a hard error.)
    try:
        from ..._lifecycle._runtime_select import _get_runtime

        return _get_runtime(cfg).is_running(cfg)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return None


def _label_list_contains(labels: dict, label_key: str, wanted: str) -> bool:
    """True iff any comma-separated ``wanted`` token is in ``labels[label_key]``.

    Mirrors the existing ``capabilities`` matching (comma-separated in YAML,
    ``value in list`` membership), generalised so both ``--capability`` and
    ``--tags`` share one comparison instead of two near-identical copies.
    Also OR-matches when the CALLER passes multiple comma-separated wanted
    values (``--tags active-development,researcher``): true if the agent
    carries ANY of them.
    """
    have = {c.strip() for c in labels.get(label_key, "").split(",") if c.strip()}
    want = {w.strip() for w in wanted.split(",") if w.strip()}
    return bool(have & want)


def get_agent_list_data(
    registry: Registry,
    capability: str | None = None,
    machine: str | None = None,
    tags: str | None = None,
    remote_probe_timeout_s: float = 2.0,
    max_parallel_probes: int = 8,
) -> list[dict]:
    """Get agent list as plain dicts for JSON or table output.

    Args:
        registry: The agent registry to query.
        capability: If set, only include agents whose ``capabilities`` label
            contains this value (comma-separated matching).
        machine: If set, only include agents whose ``machine`` label matches.
        tags: If set, only include agents whose ``tags`` label (comma-
            separated in YAML, e.g. ``tags: "active-development"``) contains
            ANY of the given comma-separated values — a free-form, multi-
            value lifecycle/status marker, deliberately separate from
            ``groups`` (which is ACL-gated and singular-effective; see
            ``config._group_resolver``) and from ``capabilities`` (what an
            agent can do, not its current work status).
        remote_probe_timeout_s: Per-agent SSH probe timeout for the
            ``is_running`` check. Short by default (2s) so the list
            command doesn't block indefinitely when the remote host is
            unreachable or the local ulimit wall throttles SSH fan-out
            (todo#254 regression). Exceeding this returns
            ``is_running=None`` (liveness unknown) instead of blocking.
        max_parallel_probes: How many remote probes to run concurrently.
            Kept small to stay under the macOS ``kern.maxproc`` wall
            that today's SSH fan-out regression exposed.

    Rows with a remote probe that timed out have ``status="unknown"``
    and ``liveness_unknown=True`` so JSON consumers can surface a
    soft-warning rather than treating unreachable remotes as offline.
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as _FuturesTimeout

    entries = registry.list_all()

    # First pass: resolve configs + filter.
    # F-CS17 stage 3b: there are no longer "remote" agents from sac's
    # POV. Every agent is a container on this host. Cross-host work
    # routes through F-CS12's ``sac --on <peer>`` which spawns a fresh
    # sac on the remote host; the remote sac then reports its own
    # local list. So this function probes every agent locally.
    prepared: list[dict] = []
    for idx, entry in enumerate(entries):
        name = entry.get("name", "?")
        screen_name = entry.get("screen", "?")
        started = entry.get("started_at", "?")
        labels: dict[str, str] = {}
        config_path = entry.get("config")
        cfg = None
        if config_path:
            # stx-allow: fallback (reason: config YAML may be corrupt or
            # missing — agent still appears in list with empty labels)
            try:
                cfg = load_config(config_path)
                labels = cfg.labels
            except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
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
        if tags and not _label_list_contains(labels, "tags", tags):
            continue

        prep = {
            "idx": idx,
            "name": name,
            "screen_name": screen_name,
            "started": started,
            "labels": labels,
            "cfg": cfg,
            "config_path": config_path,
        }
        prepared.append(prep)

    # Second pass: parallel local liveness probes with per-probe
    # timeout. The thread pool keeps the wall-clock cost low when many
    # agents are registered (each probe is ``docker inspect`` and
    # takes ~50ms-ish on a healthy host).
    #
    # Explicit shutdown(wait=False) instead of ``with ... as pool:``
    # so the context manager's __exit__ doesn't join all workers
    # (todo#254 regression: that would defeat the per-probe timeout).
    probe_results: dict[int, bool | None] = {}
    probe_targets = [
        (prep["idx"], prep["cfg"]) for prep in prepared if prep["cfg"] is not None
    ]
    if probe_targets:
        # Resolve _probe_local via the parent package at call time so
        # test monkeypatching of ``_helpers._probe_local`` still takes
        # effect (tests historically patched the flat-module attribute;
        # the __init__ re-export keeps that contract working post-split).
        import sys as _sys

        _pkg = _sys.modules[__name__.rsplit(".", 1)[0]]
        _probe_fn = getattr(_pkg, "_probe_local", _probe_local)

        pool = ThreadPoolExecutor(max_workers=max_parallel_probes)
        try:
            future_to_idx = {
                pool.submit(_probe_fn, cfg): idx for idx, cfg in probe_targets
            }
            for future in list(future_to_idx):
                idx = future_to_idx[future]
                # stx-allow: fallback (reason: per-probe runtime exception
                # maps to None = "liveness unknown", not "stopped")
                try:
                    probe_results[idx] = future.result(timeout=remote_probe_timeout_s)
                except _FuturesTimeout:  # stx-allow: fallback (reason: expected failure — see inline comment)
                    probe_results[idx] = None
                    future.cancel()
                except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                    probe_results[idx] = None
        finally:
            pool.shutdown(wait=False)

    # Third pass: build result rows. Per-row config_path is pulled
    # from each ``prep`` dict so validation runs against the agent's
    # own spec.yaml (was previously leaking the last loop iteration's
    # path — fixed 2026-05-13).
    results: list[dict] = []
    for prep in prepared:
        name = prep["name"]
        screen_name = prep["screen_name"]
        started = prep["started"]
        labels = prep["labels"]
        cfg = prep["cfg"]
        config_path = prep["config_path"]

        # ``multiplexer`` is the F-CS17 successor of the screen / tmux
        # column: it now reports the container engine the agent runs
        # on (docker / podman / apptainer), or None when the yaml is
        # missing / unparseable. Backwards compat: existing JSON
        # consumers still see a "multiplexer" key in each row.
        multiplexer: str | None = (
            getattr(cfg, "runtime", None) if cfg is not None else None
        )

        liveness_unknown = False
        probe = probe_results.get(prep["idx"])
        if cfg is None:
            # Couldn't load the yaml — can't probe.
            is_running = False
            liveness_unknown = True
        elif probe is None:
            is_running = False
            liveness_unknown = True
        else:
            is_running = bool(probe)

        status_val: str
        if liveness_unknown:
            status_val = "unknown"
        else:
            status_val = "running" if is_running else "stopped"

        errors: list[str] = []
        if config_path:
            from ...config._validation import validate_config

            try:  # stx-allow: fallback (validator raise → treat exception as a single error)
                errors = validate_config(str(config_path))
            except Exception as exc:
                errors = [str(exc)]
        # Host / path split for the table — keep the legacy `remote`
        # key on the row for backward-compat JSON consumers.
        host_label = "local"
        spec_path = str(config_path) if config_path else ""
        # Layer-6: surface the auto-allocated a2a port so operators can
        # see which IPC port the sidecar is bound to without grepping
        # state.db by hand. ``None`` when no claim exists (agent never
        # started under the allocator, or sidecar-disabled spec).
        a2a_port = _safe_port_for(name)
        # Which Anthropic account this agent authenticates as. Agents
        # sharing one label share one server-side rate limit. For a RUNNING
        # agent prefer the ACTUAL runtime account (read from its per-agent
        # ``<runtime>/home/.claude.json``) over the spec-derived label —
        # pool-based agents all resolve to the same host-OAuth spec label
        # otherwise, hiding the load-balanced per-agent pick. Called as a
        # bare name so a test can rebind ``_al._runtime_account_for``.
        account_label = _safe_account_for(cfg)
        if status_val == "running":
            runtime_label = _runtime_account_for(name)
            if runtime_label:
                account_label = runtime_label
        row: dict = {
            "name": name,
            "status": status_val,
            "screen": screen_name,
            "multiplexer": multiplexer,
            "started_at": started,
            "host": host_label,
            "path": spec_path,
            "a2a_port": a2a_port,
            "account": account_label,
        }
        # Operator mandate (lead a2a 1781e82a, 2026-06-14): surface
        # session.jsonl movement + last heartbeat at the per-row level
        # of ``sac agents status --json`` so the kick-cycle reads
        # MOVEMENT without scraping the SDK heartbeat.json out of band.
        # All three keys are always present; missing-data renders as
        # ``0`` / ``""``.
        row.update(_movement_fields(name))
        if errors:
            row["validation_errors"] = errors
        if liveness_unknown:
            row["liveness_unknown"] = True
        if labels:
            row["labels"] = labels
        results.append(row)

    # Merge in agents that are *defined* on disk but absent from the
    # registry. Filesystem is the canonical "defined" surface; the
    # registry is a runtime cache of started/stopped state. An agent
    # that was deleted from the registry (or never started) should
    # still show up so the operator can spot it.
    #
    # While walking, also yaml-validate each spec — broken yamls
    # surface as status="invalid" rather than silently hiding, so the
    # operator notices the agent won't actually start before they
    # discover it via a confusing `sac agent start` traceback.
    from ...config._validation import validate_config

    registered = {r["name"] for r in results}
    for name, spec_path in _discover_defined_agents():
        if name in registered:
            continue
        labels: dict[str, str] = {}
        cfg = None
        # stx-allow: fallback (defined-row labels are best-effort; a
        # broken yaml still surfaces with status=invalid + empty labels)
        try:
            cfg = load_config(str(spec_path))
            labels = cfg.labels
        except Exception:
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
        if tags and not _label_list_contains(labels, "tags", tags):
            continue
        # stx-allow: fallback (validator may raise on unparseable yaml;
        # treat as "invalid" with the exception text as the only error)
        try:
            errors = validate_config(str(spec_path))
        except Exception as exc:
            errors = [str(exc)]
        status = "invalid" if errors else "defined"
        row: dict = {
            "name": name,
            "status": status,
            "screen": "-",
            "multiplexer": getattr(cfg, "runtime", None) if cfg else None,
            "started_at": "-",
            "host": "local",
            "path": str(spec_path),
            "a2a_port": _safe_port_for(name),
            "account": _safe_account_for(cfg),
        }
        row.update(_movement_fields(name))
        if errors:
            row["validation_errors"] = errors
        if labels:
            row["labels"] = labels
        results.append(row)
    return results


def _is_self_peer_marker(spec_path: "Path") -> bool:  # noqa: F821
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


def _discover_defined_agents() -> "list[tuple[str, Path]]":  # noqa: F821
    """Walk the user-scope (and project-scope, when in a git repo)
    ``agents/`` tree and return ``(name, spec.yaml path)`` pairs for
    every agent declared on disk. Tolerant of partial state — a
    directory without a ``spec.yaml`` is skipped silently. Self-peer
    registration markers (see :func:`_is_self_peer_marker`) are NOT
    agents and are excluded here at the source, rather than surfacing
    as a spuriously "invalid" agent downstream.
    """
    from pathlib import Path as _Path

    pairs: list[tuple[str, _Path]] = []
    seen: set[str] = set()

    roots: list[_Path] = []
    # stx-allow: fallback (project-scope is optional; absent → skip)
    try:
        from scitex_config._ecosystem import local_state as _ls

        project = _ls.find_project_scope("agent-container")
        if project is not None:
            roots.append(project / "agents")
    except Exception:
        pass
    roots.append(_Path.home() / ".scitex" / "agent-container" / "agents")

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


# Presentation layer lives in the sibling ``_agent_list_render`` module
# (512-line cap split). Re-exported here so ``from ._agent_list import
# print_agent_list`` and the ``_helpers/__init__`` lazy map keep resolving.
from ._agent_list_render import (  # noqa: E402,F401
    _extract_damaged_fields,
    _is_ghost_row,
    print_agent_list,
    print_agent_list_json,
)

