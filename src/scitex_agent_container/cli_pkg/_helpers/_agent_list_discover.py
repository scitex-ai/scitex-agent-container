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
        if capability:
            caps = [
                c.strip()
                for c in labels.get("capabilities", "").split(",")
                if c.strip()
            ]
            if capability not in caps:
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


def _default_remote_status_probe(
    specs: dict[str, Path],
    run_ssh: Callable[[list[str]], int] | None,
) -> Callable[[str, str], str]:
    """The production status probe: ssh the peer's tmux, map the verdict.

    Returns a ``(name, host) -> status`` callable. It routes through the
    ALREADY-deployed :func:`_lifecycle._verdict_resolve.remote_process_signal`
    (ssh ``tmux has-session`` on the peer) and maps its TERNARY verdict:

        ALIVE -> "running", DEAD -> "stopped", UNKNOWN -> "unknown".

    UNKNOWN maps to "unknown" — hidden from the default view but COUNTED in the
    footer and shown in ``-v`` (the render layer's non-live handling). A wedged
    ssh / bare-PATH / auth / ProxyJump hiccup did not OBSERVE the peer, so it
    must not masquerade as "running" — the ~17 stale ``spartan-bmNNN`` rows that
    rendered as a comforting green were exactly this lie. "Never hide" always
    meant "do not DELETE a live agent", not "report an un-probed one as up":
    only a POSITIVE remote absence (the peer's own ``tmux has-session`` rc=1)
    reads "stopped". ``run_ssh`` is threaded straight into
    ``remote_process_signal`` so tests inject a real rc-returning callable and
    never shell out.
    """

    def _probe(name: str, host: str) -> str:
        from ..._lifecycle._verdict import ALIVE, DEAD
        from ..._lifecycle._verdict_resolve import remote_process_signal

        config = _load_or_synthesize_config(specs.get(name), name)
        if config is None:
            return "unknown"
        # stx-allow: fallback (a probe that blew up observed NOTHING — "unknown"
        # (hidden + footer-counted), never a false "running" that would slander
        # an un-probed peer as up)
        try:
            signal = remote_process_signal(config, host, run_ssh=run_ssh)
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            return "unknown"
        return {ALIVE: "running", DEAD: "stopped"}.get(signal.verdict, "unknown")

    return _probe


def _probe_remote_statuses(
    candidates: list[dict],
    probe: Callable[[str, str], str],
    max_parallel_probes: int,
    probe_timeout_s: float,
) -> dict[str, str]:
    """``{name: status}`` from ``probe`` over a bounded pool with a hard timeout.

    Mirrors the local liveness pass in :func:`_agent_list.get_agent_list_data`:
    a dedicated ``ThreadPoolExecutor`` with a per-future timeout and
    ``shutdown(wait=False)`` so one wedged peer cannot serialize (or hang) the
    whole ``sac agents list``. A timed-out / raised probe maps to "unknown" —
    hidden from the default view but COUNTED in the footer: a probe that could
    not run is not evidence a remote agent died, but neither is it evidence it
    is running, so it must not render as a comforting green.
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as _FuturesTimeout

    statuses: dict[str, str] = {}
    if not candidates:
        return statuses
    workers = max(1, min(max_parallel_probes, len(candidates)))
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        future_to_name = {
            pool.submit(probe, c["name"], c["host"]): c["name"] for c in candidates
        }
        for future in list(future_to_name):
            name = future_to_name[future]
            # stx-allow: fallback (a per-probe timeout/exception is "unknown" —
            # hidden from the default view + counted in the footer; a probe that
            # could not run must not masquerade as a running remote row)
            try:
                statuses[name] = future.result(timeout=probe_timeout_s)
            except _FuturesTimeout:  # stx-allow: fallback (reason: see inline comment)
                statuses[name] = "unknown"
                future.cancel()
            except Exception:  # stx-allow: fallback (reason: see inline comment)
                statuses[name] = "unknown"
    finally:
        pool.shutdown(wait=False)
    return statuses


def remote_instance_rows(
    *,
    registered: set[str],
    display_host: str,
    port_claims: dict[str, int],
    running_only: bool = False,
    capability: str | None = None,
    machine: str | None = None,
    group: str | None = None,
    instances_oracle: Callable[[], list[dict]] | None = None,
    status_probe: Callable[[str, str], str] | None = None,
    run_ssh: Callable[[list[str]], int] | None = None,
    max_parallel_probes: int = 8,
    probe_timeout_s: float = 12.0,
) -> list[dict]:
    """Rows for agents recorded as running on a REMOTE peer (master-authoritative).

    THE READ-SIDE the fleet view was missing. On dispatch, the master already
    records an ``instances`` row (``host=<peer>``, ``remote=1``, ``pid=NULL``;
    see ``_dispatch.try_dispatch_remote``) and tombstones it on stop — but
    :func:`get_agent_list_data` only ever read the JSON ``Registry`` (local,
    hostless) and the on-disk spec walk, so a spartan-dispatched agent fell
    through to a misleading ``defined`` / ``local`` row. This reads the active
    ``remote=1`` rows and emits a proper ``host=<peer>`` row per remote agent.

    Precedence: a LOCAL ``Registry`` row wins (its name is in ``registered``,
    so it is skipped here) and this in turn suppresses the defined-on-disk row
    (the caller feeds the union of covered names into
    :func:`defined_agent_rows`). Keyed on the authoritative ``remote`` flag, NOT
    a hostname compare, so a same-host-named local start is never mistaken for a
    cross-host one.

    STATUS IS LIVE, never a trusted stale row: each remote row's status comes
    from an ssh probe of the peer's own tmux (see
    :func:`_default_remote_status_probe`), so an agent that died on a
    login-node reboot reads ``stopped`` rather than a comforting ``running`` —
    and a peer the probe could NOT reach (wedged ssh, a broken ProxyJump to a
    compute node, auth) reads ``unknown`` (hidden from the default view but
    counted in the footer), NOT a false ``running``. Probes run through a
    bounded pool (:func:`_probe_remote_statuses`) to keep list latency bounded.
    Both the ``status_probe`` and the underlying ``run_ssh`` are injection seams
    so tests never shell out.

    The Account column is derived from the agent's on-disk spec (which lives on
    the master's disk — that is how it was dispatched) via
    :func:`_safe_account_for`, exactly like :func:`defined_agent_rows`, so a
    remote row no longer shows a bare ``—``.

    These agents are never locally LIVE, so — exactly like
    :func:`defined_agent_rows` — they carry the never-checked auth shape
    (``verdict_for(None)``) and the empty movement trio, keeping the ``--json``
    row shape uniform. Helpers resolve through the ``_agent_list`` module
    namespace so the suite's real-attribute seams keep working.
    """
    from ..._state.auth_state import verdict_for
    from ..._state.state_db import list_active_instances
    from ...config import load_config
    from . import _agent_list as _al

    del running_only  # accepted for signature-parity; remote status is always
    # probed (a remote row's whole value is its live status), and the render
    # layer — not this builder — hides non-running rows in the default view.

    oracle = instances_oracle or (lambda: list_active_instances(host=None))
    discover = getattr(_al, "_discover_defined_agents", _discover_defined_agents)
    specs: dict[str, Path] = {name: path for name, path in discover()}

    # First pass: keep only the active REMOTE rows, apply the label filters
    # (loading the agent's on-disk spec for its labels), collect candidates.
    candidates: list[dict] = []
    for entry in oracle():
        name = entry.get("name")
        if not name or name in registered:  # a local Registry row wins
            continue
        if not entry.get("remote"):  # key on the authoritative flag
            continue
        spec_path = specs.get(name)
        labels: dict[str, str] = {}
        # stx-allow: fallback (label filtering is best-effort; an unreadable
        # spec yields empty labels, never a crash of the list)
        try:
            if spec_path is not None:
                labels = load_config(str(spec_path)).labels
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            labels = {}
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
        if group and not _al._label_group_matches(labels, group):
            continue
        candidates.append(
            {
                "name": name,
                "host": entry.get("host") or "",
                "started_at": entry.get("started_at") or "-",
                "a2a_port": entry.get("a2a_port")
                or entry.get("bound_port")
                or port_claims.get(name),
                "spec_path": spec_path,
                "labels": labels,
            }
        )

    # Second pass: LIVE status via the injected (or default ssh) probe.
    probe = status_probe or _default_remote_status_probe(specs, run_ssh)
    statuses = _probe_remote_statuses(
        candidates, probe, max_parallel_probes, probe_timeout_s
    )

    # Third pass: build the rows (mirrors ``defined_agent_rows``' shape).
    rows: list[dict] = []
    for cand in candidates:
        name = cand["name"]
        host = cand["host"]
        spec_path = cand["spec_path"]
        # Account from the on-disk spec — the SAME spec-derived label
        # ``defined_agent_rows`` uses. The remote agent's spec DOES live on the
        # master's disk (that is how it was ssh-dispatched), so this kills the
        # bare "—" the Account column showed for every remote row. (This is the
        # spec-derived label, NOT the exact runtime pool pick — that accurate
        # value needs a DB column and is a separate follow-up.) Best-effort: an
        # unreadable spec yields "" so the list never crashes on it.
        account = ""
        if spec_path is not None:
            # stx-allow: fallback (a broken/unreadable spec must not crash the
            # list; "" is the honest empty, exactly as before this change)
            try:
                account = _al._safe_account_for(load_config(str(spec_path)))
            except Exception:  # stx-allow: fallback (reason: see inline comment)
                account = ""
        row: dict = {
            "name": name,
            # A probe that could not OBSERVE the peer is "unknown" (hidden from
            # the default view, counted in the footer), never a false "running".
            "status": statuses.get(name, "unknown"),
            "screen": "-",
            "multiplexer": None,
            "started_at": cand["started_at"],
            "host": host,
            "host_display": _al._host_display_for(host, display_host),
            "path": str(spec_path or ""),
            "a2a_port": cand["a2a_port"],
            "account": account,
            "remote": True,
        }
        row.update(dict(_al._MOVEMENT_DEFAULTS))
        row.update(verdict_for(None))
        if cand["labels"]:
            row["labels"] = cand["labels"]
        rows.append(row)
    return rows
