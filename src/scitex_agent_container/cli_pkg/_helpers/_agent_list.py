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

# Auth status (``auth-failed`` vs plain green ``running``) — sibling module.
# tmux-up is NOT operational: an agent whose API calls are all being rejected
# stays green forever. ``resolve_auth`` reads the WATCHDOG'S CACHED verdict;
# nothing here ever probes auth inline (that would cost minutes — see
# ``_agent_list_auth``).
from ._agent_list_auth import (  # noqa: F401
    LIVE_STATUSES,
    STATUS_AUTH_FAILED,
    all_auth_states,
    is_live_status,
    resolve_auth,
)

# The LIVE credential bind — ground truth for a running local agent, and the
# STRONGEST of the three Account signals. Same re-import rationale as above.
from ._agent_list_bound_account import bound_accounts_by_agent  # noqa: F401

# Host-DISPLAY resolution (Host column) — sibling module, 512-line cap split.
from ._agent_list_host import _host_display_for, _resolve_display_host

# The local liveness probe AND its record of how it answered. Kept in a
# sibling so the adapter name can only ever come from the probe call itself
# — see that module's header for why a separately-computed label would lie
# in exactly the case it exists to catch.
from ._agent_list_probe import LocalProbe, probe_local_detail  # noqa: F401

# Row shaping + the movement trio. Re-exported so the bare-name call sites
# here, and the test seams that rebind ``_al._movement_fields``, keep working.
from ._agent_list_row import (  # noqa: F401
    _MOVEMENT_DEFAULTS,
    _movement_fields,
    build_agent_row,
)


def _all_port_claims() -> dict[str, int]:
    """Return ``{agent_name: a2a_port}`` for every claim, in ONE db read.

    Replaces the former per-agent ``_safe_port_for`` (``get_port`` per row)
    in ``sac agents list``: each ``get_port`` opened + init-schema'd the
    state.db ~3x (~62ms/agent on a full host); ``list_claims`` does it once.
    Callers look the port up per row from the returned dict. Tolerant: any
    failure maps to an empty map so the list never crashes on a port-lookup
    hiccup (rows render ``—``).
    """
    # stx-allow: fallback (reason: list output must never crash on a
    # port-allocator hiccup; an empty map rendered as ``—`` is the right UX.)
    try:
        from ..._state import port_allocator

        return {c["name"]: c["port"] for c in port_allocator.list_claims()}
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return {}


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

    THE TRI-STATE ANSWER ONLY. The probe itself now lives in
    :func:`._agent_list_probe.probe_local_detail`, which additionally
    records WHICH adapter answered and, on an abstention, why. This
    remains the bool-or-None view because that is what the merged
    regression guard drives (``test_probe_local_reports_a_live_tui_session
    _as_running``) and what external callers have always had. It delegates
    rather than re-implementing, so the two can never disagree.
    """
    return probe_local_detail(cfg).running


def _label_group_matches(labels: dict, wanted: str) -> bool:
    """True iff the spec's ``groups`` name ANY comma-separated ``wanted`` value.

    Replaces the pre-2026-07-19 ``tags``-label matcher. ``tags:`` was
    ABOLISHED (operator decision) because every spec carrying it also
    carried the same classification inside ``groups:`` — pure duplication.
    ``groups`` is now the only classification field.

    NOT a rename of the old string matcher: ``tags`` was authored as a
    CSV STRING (``tags: "active-development"``) while ``groups`` is a YAML
    LIST (``groups: [developer, active]``), so the old ``.split(",")``
    read would not work here. Group reading is delegated to
    :func:`config._group_resolver.all_named_groups` — the SSOT MULTI-value
    reader that ``sac agents start --group`` already trusts — which
    honours the plural list, the singular ``group`` string, and a
    defensively-authored bare string alike.

    Matching is case-insensitive / whitespace-trimmed (mirroring
    ``_start_group_filter.resolve_group_targets``) and OR-shaped: a caller
    passing ``--group developer,researcher`` matches an agent in EITHER.
    """
    from ...config._group_resolver import all_named_groups

    have = {g.strip().lower() for g in all_named_groups(labels)}
    want = {w.strip().lower() for w in wanted.split(",") if w.strip()}
    return bool(have & want)


def get_agent_list_data(
    registry: Registry,
    capability: str | None = None,
    machine: str | None = None,
    group: str | None = None,
    remote_probe_timeout_s: float = 2.0,
    max_parallel_probes: int = 8,
    running_only: bool = False,
    remote_status_probe=None,
    remote_run_ssh=None,
) -> list[dict]:
    """Get agent list as plain dicts for JSON or table output.

    Args:
        registry: The agent registry to query.
        running_only: PERF hint from the DEFAULT human view, which discards
            every non-running row before rendering. When True, the heavy
            per-row enrichment (account resolution + session-movement IO) is
            SKIPPED for rows that are not ``running`` — they still get a
            correct ``status`` (so the hidden-count footer is right) but a
            blank ``account`` + default movement fields. The ``--json`` and
            ``-v`` / ``--all`` paths leave this False so every row stays fully
            enriched (they show non-running rows). Default False preserves the
            original all-rows-enriched behaviour.
        capability: If set, only include agents whose ``capabilities`` label
            contains this value (comma-separated matching).
        machine: If set, only include agents whose ``machine`` label matches.
        group: If set, only include agents whose spec names ANY of the
            given comma-separated groups in ``metadata.labels.groups``
            (or the singular ``.group``). Replaces the abolished ``tags``
            filter (operator decision 2026-07-19): ``groups`` is the only
            classification field, so the lifecycle/status marker that used
            to live in ``tags`` (e.g. ``active-development``) is now the
            ``active`` group. Read via the SSOT multi-value reader
            ``config._group_resolver.all_named_groups``, so an agent in
            several groups is reachable by ANY of them — the same cut
            ``sac agents start --group`` uses, NOT the singular-effective
            ACL resolution.
        remote_probe_timeout_s: Per-agent SSH probe timeout for the
            ``is_running`` check. Short by default (2s) so the list
            command doesn't block indefinitely when the remote host is
            unreachable or the local ulimit wall throttles SSH fan-out
            (todo#254 regression). Exceeding this returns
            ``is_running=None`` (liveness unknown) instead of blocking.
        max_parallel_probes: How many remote probes to run concurrently.
            Kept small to stay under the macOS ``kern.maxproc`` wall
            that today's SSH fan-out regression exposed.
        remote_status_probe: Injection seam for the cross-host status probe
            (``(name, host) -> status``). ``None`` uses the default ssh probe
            (peer tmux ``has-session`` via ``remote_process_signal``). Tests
            pass a real callable so they never shell out.
        remote_run_ssh: Injection seam ONE LEVEL DOWN — the ``(argv) -> rc``
            ssh runner threaded into the default status probe's
            ``remote_process_signal``. Lets a test drive the real
            verdict-mapping (rc 0/1/other -> running/stopped/running) without
            an ssh binary. Ignored when ``remote_status_probe`` is given.

    Rows with a remote probe that timed out have ``status="unknown"``
    and ``liveness_unknown=True`` so JSON consumers can surface a
    soft-warning rather than treating unreachable remotes as offline.
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as _FuturesTimeout

    entries = registry.list_all()

    # Live credential binds, measured ONCE for this listing (one /proc walk,
    # not one per row). Re-measured on every call, so ``--watch`` never
    # renders a bind read minutes ago.
    live_binds = bound_accounts_by_agent()

    # Host DISPLAY column hostname, resolved ONCE (test-swappable seam).
    display_host = _resolve_display_host()

    # A2A ports for ALL agents in ONE db read (was a per-agent get_port that
    # re-opened + re-init-schema'd state.db ~3x each). Looked up per row.
    port_claims = _all_port_claims()

    # Cached AUTH verdicts for ALL agents in ONE db read, same shape as the
    # port claims above. The watchdog wrote these; we only read them. Never
    # probe auth per row — the real check captures each pane twice, seconds
    # apart, and would turn this command into a multi-minute wait.
    auth_states = all_auth_states()

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
        if group and not _label_group_matches(labels, group):
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
    probe_results: dict[int, LocalProbe] = {}
    probe_targets = [
        (prep["idx"], prep["cfg"]) for prep in prepared if prep["cfg"] is not None
    ]
    if probe_targets:
        # Resolve the probe via the parent package at call time so a test
        # swapping ``_helpers.probe_local_detail`` still takes effect (tests
        # historically patched the flat-module attribute; the __init__
        # re-export keeps that contract working post-split).
        import sys as _sys

        _pkg = _sys.modules[__name__.rsplit(".", 1)[0]]
        _probe_fn = getattr(_pkg, "probe_local_detail", probe_local_detail)

        pool = ThreadPoolExecutor(max_workers=max_parallel_probes)
        try:
            future_to_idx = {
                pool.submit(_probe_fn, cfg): idx for idx, cfg in probe_targets
            }
            for future in list(future_to_idx):
                idx = future_to_idx[future]
                # stx-allow: fallback (reason: an abstention, NOT a "stopped" —
                # and it says which of the two ways it abstained, because a
                # verdict that cannot explain itself is what made the third
                # "live agent reads stopped" report undiagnosable.)
                try:
                    probe_results[idx] = future.result(timeout=remote_probe_timeout_s)
                except _FuturesTimeout:  # stx-allow: fallback (reason: expected failure — see inline comment)
                    probe_results[idx] = LocalProbe(
                        running=None,
                        runtime=None,
                        error=f"probe exceeded {remote_probe_timeout_s}s",
                    )
                    future.cancel()
                except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                    probe_results[idx] = LocalProbe(
                        running=None, runtime=None, error=f"probe raised: {exc}"
                    )
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
        # HOW the verdict was reached, carried on the row. A bare "stopped"
        # is exactly what made the 2026-08-04 report undiagnosable once the
        # host had rebooted: no adapter, no reason, nothing to re-derive from.
        probe_runtime: str | None = None
        probe_error: str | None = None
        if cfg is None:
            # Couldn't load the yaml — can't probe.
            is_running = False
            liveness_unknown = True
            probe_error = "spec did not load"
        elif probe is None or probe.running is None:
            is_running = False
            liveness_unknown = True
            if probe is not None:
                probe_runtime = probe.runtime
                probe_error = probe.error
        else:
            is_running = probe.running
            probe_runtime = probe.runtime

        status_val: str
        if liveness_unknown:
            status_val = "unknown"
        else:
            status_val = "running" if is_running else "stopped"

        # tmux-up != OPERATIONAL. A liveness probe says only that the session
        # and its pane process exist — an agent whose every API call is being
        # rejected satisfies that and is doing NOTHING. Fold the watchdog's
        # CACHED verdict in, so such a row reads ``auth-failed`` (with the age
        # of the evidence) instead of a reassuring green ``running``.
        auth, status_val = resolve_auth(name, auth_states, started, status_val)

        # FIX (no double-parse): a config that ``load_config`` ACCEPTED is
        # valid by construction — ``load_config`` runs ``validate_raw`` and
        # RAISES on any error — so cfg-not-None ⇒ zero errors. Only
        # RE-VALIDATE (a second open+parse of the same file) when the load
        # FAILED, to recover the error list for the YAML column.
        errors: list[str] = []
        if config_path and cfg is None:
            from ...config._validation import validate_config

            try:  # stx-allow: fallback (validator raise → single error)
                errors = validate_config(str(config_path))
            except Exception as exc:
                errors = [str(exc)]
        # Host / path split for the table — keep the legacy `remote`
        # key on the row for backward-compat JSON consumers.
        host_label = "local"
        spec_path = str(config_path) if config_path else ""
        # a2a port from the ONE-query claims map. ``None`` when no claim
        # exists (agent never started under the allocator).
        a2a_port = port_claims.get(name)
        # PERF: the running-only default view discards non-LIVE rows, so skip
        # their account resolution + movement IO. ``status`` is already
        # computed, so the hidden-count footer stays correct. Gate on
        # ``is_live_status`` (not ``!= "running"``): a ``login-required`` row is
        # SHOWN in the default view, so deferring its enrichment would blank out
        # its Account — and that account is precisely the one that is dead.
        deferred = running_only and not is_live_status(status_val)
        # Which Anthropic account this agent authenticates as. For a LIVE agent
        # prefer the ACTUAL runtime account (its per-agent
        # ``<runtime>/home/.claude.json``) over the spec-derived label — pool
        # agents share one host-OAuth spec label otherwise. Bare names so a
        # test can rebind ``_al._safe_account_for`` / ``_al._runtime_account_for``.
        if deferred:
            account_label = ""
        else:
            account_label = _safe_account_for(cfg)
            if is_live_status(status_val):
                # Precedence: live BIND > runtime login RECORD > spec label.
                # See ``_agent_list_bound_account`` for why leading with the
                # record was wrong — it is a PAST login, measured 11-37 days
                # stale, and the spec label collapses to one host identity.
                account_label = (
                    live_binds.get(name) or _runtime_account_for(name) or account_label
                )
        results.append(
            build_agent_row(
                name=name,
                status_val=status_val,
                screen_name=screen_name,
                multiplexer=multiplexer,
                started=started,
                host_label=host_label,
                host_display=_host_display_for(host_label, display_host),
                spec_path=spec_path,
                a2a_port=a2a_port,
                account_label=account_label,
                deferred=deferred,
                errors=errors,
                liveness_unknown=liveness_unknown,
                probe_runtime=probe_runtime,
                probe_error=probe_error,
                labels=labels,
            )
        )

    # Merge in agents recorded as running on a REMOTE peer (master-authoritative
    # cross-host visibility). The master already wrote an ``instances`` row on
    # dispatch (``host=<peer>``, ``remote=1``); this is the READ side that was
    # missing. Precedence: a LOCAL registry row wins (its name is in
    # ``reg_names`` so the remote merge skips it); the remote row in turn
    # suppresses the defined-on-disk row (``covered`` feeds ``defined_agent_rows``
    # so a remote agent is not ALSO emitted as a defined/local row).
    reg_names = {r["name"] for r in results}
    remote_rows = remote_instance_rows(
        registered=reg_names,
        display_host=display_host,
        port_claims=port_claims,
        running_only=running_only,
        capability=capability,
        machine=machine,
        group=group,
        status_probe=remote_status_probe,
        run_ssh=remote_run_ssh,
    )
    results.extend(remote_rows)
    covered = reg_names | {r["name"] for r in remote_rows}

    # Then the agents DEFINED on disk but absent from BOTH registries. Their
    # discovery + row-build live together in the sibling ``_agent_list_discover``
    # (512-line cap split); this stays the orchestrator that merges the sources.
    # They are never live, so they carry the never-checked auth shape.
    results.extend(
        defined_agent_rows(
            registered=covered,
            port_claims=port_claims,
            display_host=display_host,
            capability=capability,
            machine=machine,
            group=group,
            running_only=running_only,
        )
    )
    return results


# Defined-on-disk discovery + row-build live in the sibling
# ``_agent_list_discover`` module (512-line cap split). Re-imported so the
# bare-name call sites above and the test seams
# ``_al._discover_defined_agents`` / ``_al._is_self_peer_marker`` resolve.
from ._agent_list_discover import (  # noqa: E402,F401
    _discover_defined_agents,
    _is_self_peer_marker,
    defined_agent_rows,
    remote_instance_rows,
)

# Presentation layer lives in the sibling ``_agent_list_render`` module
# (512-line cap split). Re-exported here so ``from ._agent_list import
# print_agent_list`` and the ``_helpers/__init__`` lazy map keep resolving.
from ._agent_list_render import (  # noqa: E402,F401
    _extract_damaged_fields,
    _is_ghost_row,
    print_agent_list,
    print_agent_list_json,
)
