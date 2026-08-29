"""Remote-instance rows for ``sac agents list`` — network probing, not disk.

Extracted from ``_agent_list_discover`` (2026-08-23) when that module hit the
512-line cap. The split follows the concern boundary its own docstring already
named: ``_agent_list_discover`` walks the LOCAL filesystem and builds rows for
what it finds there, while everything here reaches ACROSS THE NETWORK — ssh to a
peer's tmux, map the verdict, synthesize rows for instances this host does not
own.

Keeping ssh probing inside a module called ``_discover`` was the drift; a reader
looking for "why is my peer reported down" had no reason to open a file about
disk discovery.

``_agent_list_discover`` re-exports :func:`remote_instance_rows`, so existing
importers and the ``_al.remote_instance_rows`` test seam keep resolving
unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

__all__ = ["remote_instance_rows"]


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

        # Imported inside the call, not at module scope: ``_agent_list_discover``
        # imports THIS module for the ``remote_instance_rows`` re-export, so a
        # top-level import here would close the cycle.
        from ._agent_list_discover import _load_or_synthesize_config

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
    import time as _time
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
        # ONE shared wall-clock budget for the batch. future.result(timeout=T)
        # in submission order restarts the deadline PER FUTURE, so n stalled
        # probes cost about ceil(n/workers)*T even though the pool is parallel
        # -- precisely the serialization the docstring above promises cannot
        # happen. The local pass in _agent_list.get_agent_list_data was fixed
        # for todo#254 and its comment already claims "same defect, same fix, as
        # _probe_remote_statuses"; the fix never landed here. A comment is not a
        # fix, and only the test caught the difference.
        _deadline = _time.monotonic() + probe_timeout_s
        for future in list(future_to_name):
            name = future_to_name[future]
            _remaining = max(0.0, _deadline - _time.monotonic())
            # stx-allow: fallback (a per-probe timeout/exception is "unknown" —
            # hidden from the default view + counted in the footer; a probe that
            # could not run must not masquerade as a running remote row)
            try:
                statuses[name] = future.result(timeout=_remaining)
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

    # Function-scope for the same cycle reason as in _default_remote_status_probe.
    from ._agent_list_discover import _discover_defined_agents

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
        if capability and not _al._label_capability_matches(labels, capability):
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
