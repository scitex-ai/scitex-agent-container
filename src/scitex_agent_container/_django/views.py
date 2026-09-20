"""Browser + JSON views for the scoped Agents dashboard.

Read-only by default. A view never invents state: it asks the listener, scopes
the result to the caller, and projects it. Control is a separate, gated path.

DUAL MODE. The same views render two ways, chosen by the mount prefix (derived
with scitex-ui's ``mount_prefix`` — the single source of truth):

* **Standalone** (root mount, ``sac gui serve``): extend the full
  ``scitex_ui/standalone_shell.html`` (``fleet.html`` / ``detail.html``).
* **Mounted in SciTeX Hub** (``/apps/agents/``): extend the Hub's
  ``global_base.html`` (``fleet_hub.html`` / ``detail_hub.html``), which already
  renders the header + nav. This is what makes "no duplicate header, no project
  switcher" true: the package supplies content only, the Hub supplies the shell.

Per-row links and the lifecycle redirect use ``url_base`` so they are correct in
both modes.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlencode

from django.http import (
    HttpRequest,
    HttpResponseForbidden,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from ._authorization import can_control, fleet_visibility, resolve_identity, scope_rows
from ._constants import API_URL_ENV
from ._projection import project_detail, project_row
from ._remote import RemoteFleet, safe_error_message
from ._timeline import (
    KINDS,
    REFRESH_SECONDS,
    STALE_AFTER_SECONDS,
    build_timeline,
    timeline_rows,
)


def _mount_base(request: HttpRequest, view_path: str) -> str:
    """The app's mount prefix for the route currently rendering, via scitex-ui's
    SSOT. ``view_path`` must be the route this view is registered under (relative
    to the app root) — ``mount_prefix`` subtracts it from ``request.path`` to
    recover the prefix, and raises on a mismatch (a wiring bug), so this is
    correct per-route and never guesses. "" at a root mount.

    Content links and the lifecycle redirect are built from this value, so they
    are correct whether the app is standalone (base "") or mounted (base
    "/apps/agents")."""
    from scitex_ui.mount import mount_prefix

    try:
        return mount_prefix(request, view_path=view_path)
    except Exception:  # stx-allow: fallback (reason: a mount-detection failure renders standalone)
        return ""


def _shell_context(request: HttpRequest, title: str, view_path: str) -> dict:
    """Context for the standalone shell only (mounted mode uses global_base)."""
    from scitex_ui.branding import shell_context
    from scitex_ui.mount import mount_context

    # All three side panes are unused: this is a server-rendered fleet table,
    # not a file workspace. Declaring them unused is the scitex-ui API.
    panes = {"ai": "unused", "files": "unused", "viewer": "unused"}
    ctx = dict(shell_context(title, accent="agents", panes=panes))
    try:
        ctx.update(mount_context(request, view_path=view_path))
    except Exception:  # stx-allow: fallback (reason: a root mount emits no marker)
        pass
    return ctx


def _app_context(request: HttpRequest, title: str, view_path: str, **data) -> tuple[dict, bool]:
    """Build the render context and decide mounted (hub) vs standalone.

    Returns ``(context, is_standalone)``. ``url_base`` is the app's mount prefix
    (no trailing slash — the templates add it), derived for the route currently
    rendering. Because the four routes all share the same mount prefix, this is
    the same value on every view: "" at a root (standalone) mount, e.g.
    "/apps/agents" when mounted in the Hub. A non-empty base means we are
    mounted in the Hub, whose ``global_base`` owns the header.
    """
    base = _mount_base(request, view_path)
    ctx = dict(data)
    ctx["url_base"] = base
    if base == "":
        ctx.update(_shell_context(request, title, view_path))
        return ctx, True
    # Mounted in the Hub: global_base owns the header/nav; the package renders
    # content only, so there is no duplicate header and no project switcher.
    return ctx, False


def _fleet_rows(fleet: RemoteFleet, identity: str) -> tuple[list[dict], str]:
    # GET /agents is the batched fleet observation: listener-side enrichment
    # attaches liveness + launch-bound runtime identity in one active-instance
    # snapshot and one birth query.  Calling /status once per visible row was an
    # HTTP N+1 and repeated those same store scans N times.
    rows = scope_rows(fleet.list_all(), identity)
    agents = [project_row(row, row) for row in rows]
    return agents, ""


def _is_configured() -> bool:
    """Whether this deployment has *chosen* a listener."""
    return bool(os.environ.get(API_URL_ENV, "").strip())


@require_GET
def index(request: HttpRequest):
    """Render the fleet SHELL immediately; never block on the control plane.

    P0 (operator-reproduced): this view used to run the whole control-plane read
    inline, so the browser waited for SAC before it could paint anything - ~10s
    for the operator, 60.06s in my measurement - and then dumped an internal
    endpoint and setup prose when the read failed.

    Now: the shell renders from the identity's last-known snapshot if one exists,
    else in a `loading` state, and a READ-ONLY background refresh is kicked. The
    page never waits, and it never invents rows. A COMPLETED read failure is
    stored on the snapshot, so the next render / `/api/fleet` poll shows
    `unavailable` + Retry instead of a perpetual "loading". The browser-side
    poll (see _fleet_content.html) drives loading -> inventory / unavailable.
    """
    from ._inventory_cache import CACHE

    identity = resolve_identity(request)
    snapshot = CACHE.get(identity)
    if snapshot is None:
        # Cold cache: not asked yet. A loading page must NOT also claim "no
        # agents" (that is the false-empty defect) - it says it is reading.
        agents, comm_error, fleet_state, observed_age = [], "", "loading", None
    elif snapshot.error:
        # A completed failure is the last thing observed for this identity:
        # show unavailable + Retry, not a pending spinner, and not an empty fleet.
        agents, comm_error, fleet_state, observed_age = [], snapshot.error, "unavailable", snapshot.age()
    else:
        agents = [dict(row) for row in snapshot.agents]
        comm_error = ""
        observed_age = snapshot.age()
        if agents:
            fleet_state = "ok" if snapshot.is_fresh(ttl=CACHE.ttl) else "cached"
        else:
            # An answered read with nothing in scope: empty, not loading.
            fleet_state = "empty"

    # Refresh in the background, read-only, at most one at a time per identity.
    # A completed failure is recorded (redacted) so the next render / poll can
    # surface unavailable + Retry; a success warms the snapshot. The
    # authorization scope is read HERE, before the read starts (B3): a grant
    # revoked while the refresh is on the wire must not file its rows under the
    # scope the identity resolves afterwards.
    fleet = RemoteFleet.from_environment()
    scope = fleet_visibility(identity)

    def _record_failure(exc: Exception) -> None:
        """Store the COMPLETED failure (redacted) under the dispatch-time scope."""
        CACHE.record_error(identity, error=safe_error_message(exc), scope=scope)

    CACHE.refresh_async(
        identity,
        lambda: _fleet_rows(fleet, identity)[0],
        on_error=_record_failure,
    )

    context, is_standalone = _app_context(
        request,
        "Agents",
        view_path="",
        agents=agents,
        summary=_summary(agents),
        identity=identity,
        crosshost_authorized=identity in _crosshost_allowlist(),
        comm_error=comm_error,
        observed_age=observed_age,
        cache_ttl=CACHE.ttl,
        page="fleet",
        fleet_state=fleet_state,
        diagnostic_reason="the control plane did not answer" if fleet_state == "unavailable" else "",
    )
    template = "scitex_agent_container/fleet.html" if is_standalone else "scitex_agent_container/fleet_hub.html"
    return render(request, template, context)


@require_GET
def timeline(request: HttpRequest):
    """The rendered activity timeline (its JSON twin is ``timeline_api``).

    Server-rendered so the surface works with JS disabled and is complete on
    first paint; ``timeline.js`` then refreshes it in place, bounded and
    deduped. Filters come from the query string and are applied by the SAME
    server-side code the poll uses, so the two paths cannot diverge.
    """
    fleet = RemoteFleet.from_environment()
    identity = resolve_identity(request)
    try:
        rows = scope_rows(fleet.list_all(), identity)
        comm_error = ""
    except Exception as exc:  # stx-allow: fallback (reason: unreachable listener is a STATE to show)
        rows, comm_error = [], str(exc)
    names = [str(r["name"]) for r in rows if isinstance(r.get("name"), str)]
    statuses = fleet.read_statuses(names)
    agent_filter = request.GET.get("agent") or ""
    kind_filter = request.GET.get("kind") or ""
    entries = build_timeline(
        statuses,
        agent=agent_filter if isinstance(agent_filter, str) and agent_filter else None,
        kind=kind_filter if isinstance(kind_filter, str) and kind_filter else None,
    )
    context, is_standalone = _app_context(
        request,
        "Agents · Activity",
        view_path="timeline/",
        entries=timeline_rows(entries),
        summary=_timeline_summary(entries),
        agent_names=sorted(names),
        kinds=KINDS,
        selected_agent=agent_filter,
        selected_kind=kind_filter,
        refresh_seconds=REFRESH_SECONDS,
        stale_after_seconds=STALE_AFTER_SECONDS,
        identity=identity,
        listener=fleet.base_url,
        comm_error=comm_error,
        page="timeline",
    )
    template = (
        "scitex_agent_container/timeline.html"
        if is_standalone
        else "scitex_agent_container/timeline_hub.html"
    )
    return render(request, template, context)


def _timeline_summary(entries: list) -> dict:
    summary = {"total": len(entries), "observed": 0, "stale": 0, "unreachable": 0, "unknown": 0}
    for entry in entries:
        if entry.state in summary:
            summary[entry.state] += 1
    return summary


@require_GET
def fleet_api(request: HttpRequest) -> JsonResponse:
    """Browser poll endpoint: a synchronous, scoped read that WARMS the cache.

    On success it stores the projected rows so the next shell render shows the
    inventory (the loading -> inventory transition). On failure it stores the
    redacted error so the shell shows unavailable + Retry. The browser NEVER
    receives internal listener/transport detail (B2) - only a fixed phrase.
    """
    from ._inventory_cache import CACHE

    fleet = RemoteFleet.from_environment()
    identity = resolve_identity(request)
    # Dispatch-time authorization (B3): the scope this read is made under, not
    # the scope the identity resolves once the read has returned.
    scope = fleet_visibility(identity)
    try:
        agents, _ = _fleet_rows(fleet, identity)
        CACHE.put(identity, agents, scope=scope)
        return JsonResponse({"ok": True, "identity": identity, "agents": agents, "summary": _summary(agents)})
    except Exception as exc:  # stx-allow: fallback (reason: surface the failure as JSON, not a 500 page)
        CACHE.record_error(identity, error=safe_error_message(exc), scope=scope)
        return JsonResponse({"ok": False, "error": safe_error_message(exc)}, status=502)


@require_GET
def timeline_api(request: HttpRequest) -> JsonResponse:
    """The activity timeline as JSON, for the near-real-time poll.

    Reads the SAME per-agent status the fleet view reads, so this endpoint adds
    no second source of truth and no state of its own. Filters are applied
    server-side (``agent``, ``kind``): the browser must not pull the whole fleet
    to render one agent's history.
    """
    fleet = RemoteFleet.from_environment()
    identity = resolve_identity(request)
    try:
        rows = scope_rows(fleet.list_all(), identity)
    except Exception as exc:  # stx-allow: fallback (reason: an unreachable listener is a STATE the caller sees as the 502 JSON body)
        return JsonResponse({"ok": False, "error": str(exc)}, status=502)
    names = [str(r["name"]) for r in rows if isinstance(r.get("name"), str)]
    statuses = fleet.read_statuses(names)
    agent_filter = request.GET.get("agent") or None
    kind_filter = request.GET.get("kind") or None
    entries = build_timeline(
        statuses,
        agent=agent_filter if isinstance(agent_filter, str) else None,
        kind=kind_filter if isinstance(kind_filter, str) else None,
    )
    return JsonResponse(
        {
            "ok": True,
            "identity": identity,
            "count": len(entries),
            "entries": [e.as_dict() for e in entries],
            "stale_after_seconds": STALE_AFTER_SECONDS,
        }
    )


def _detail_extras(fleet: RemoteFleet, name: str, status: Any) -> dict:
    """Session-log tail + process resources for one agent.

    Both degrade to an explicit state (never a crash, never a fabricated value):
    the tail reads the listener's public ``/agents/<name>/tail`` (SSE, bounded,
    redacted); resources read ``/proc`` for the listener's pid and report
    ``namespace``/``unavailable`` when it is not visible from here.
    """
    from ._remote import RemoteOperationError
    from ._resources import read_resources
    from ._session import parse_tail_frames

    pid = None
    if isinstance(status, dict):
        pid = status.get("pid")
    resources = read_resources(pid)

    try:
        session_lines = parse_tail_frames(fleet.read_tail(name))
        session_error = ""
    except RemoteOperationError as exc:
        if exc.status_code == 404:
            # No session.jsonl yet — a legitimate state, not a fault.
            session_lines, session_error = [], ""
        else:
            session_lines, session_error = [], safe_error_message(exc)
    except Exception as exc:  # stx-allow: fallback (reason: a tail failure is a state, not fatal)
        session_lines, session_error = [], safe_error_message(exc)
    return {"session_lines": session_lines, "session_error": session_error, "resources": resources}


@require_GET
def detail(request: HttpRequest, name: str):
    fleet = RemoteFleet.from_environment()
    identity = resolve_identity(request)
    try:
        rows = scope_rows(fleet.list_all(), identity)
        list_error = ""
    except Exception as exc:  # stx-allow: fallback (reason: listener unreachable is a state)
        rows, list_error = [], safe_error_message(exc)
    row = next((r for r in rows if r.get("name") == name), None)
    if row is None:
        # Not own-scope (hidden) or genuinely absent. We do not reveal which —
        # an ordinary caller simply does not see cross-host agents.
        context, is_standalone = _app_context(
            request, f"Agent · {name}", view_path=f"{name}/",
            agent=None, identity=identity, list_error=list_error, not_found=True, page="detail",
        )
        template = "scitex_agent_container/detail.html" if is_standalone else "scitex_agent_container/detail_hub.html"
        return render(request, template, context)
    try:
        status = fleet.read_status(name)
    except Exception as exc:  # stx-allow: fallback (reason: one agent's status failing is per-agent)
        status = exc  # type: ignore[assignment]
    cross_host = row.get("scope") == "cross-host"
    agent = project_detail(row, status)
    agent.update(_detail_extras(fleet, name, status))
    from ._control import CONTROL_KEYS, new_dispatch_id

    context, is_standalone = _app_context(
        request, f"Agent · {name}", view_path=f"{name}/",
        agent=agent,
        identity=identity,
        cross_host=cross_host,
        can_operate=can_control(identity, cross_host=cross_host, request=request, agent=name),
        control_keys=CONTROL_KEYS,
        dispatch_id=new_dispatch_id(),
        list_error=list_error,
        page="detail",
    )
    template = "scitex_agent_container/detail.html" if is_standalone else "scitex_agent_container/detail_hub.html"
    return render(request, template, context)


@require_POST
def lifecycle_action(request: HttpRequest, name: str):
    fleet = RemoteFleet.from_environment()
    identity = resolve_identity(request)
    action = request.POST.get("action", "")
    if action not in {"start", "stop", "restart"}:
        return JsonResponse({"error": "unsupported lifecycle action"}, status=400)
    base = _mount_base(request, f"{name}/action")
    try:
        rows = scope_rows(fleet.list_all(), identity)
    except Exception as exc:  # stx-allow: fallback (reason: cannot authorize what cannot be listed)
        return JsonResponse({"error": safe_error_message(exc)}, status=502)
    row = next((r for r in rows if r.get("name") == name), None)
    cross_host = bool(row) and row.get("scope") == "cross-host"
    if row is None:
        # Not visible to this identity — refuse without revealing why.
        return HttpResponseForbidden("This agent is not within your scope.")
    if not can_control(identity, cross_host=cross_host, request=request, agent=name):
        from ._authorization import record_audit

        record_audit(
            {
                "event": "crosshost_control_denied",
                "identity": identity,
                "agent": name,
                "action": action,
                "path": request.path,
            }
        )
        return HttpResponseForbidden("You are not authorized for this action.")
    try:
        result = fleet.lifecycle(name, action)
        message = result.get("message") or result.get("status") or action
        state = "completed"
    except Exception as exc:  # stx-allow: fallback (reason: the failed delegate is reported to the operator in the gui-audit.log audit trail, not raised)
        raw_message = str(exc)  # kept for the server-side audit only
        message = safe_error_message(exc)  # browser-facing: no internal detail (B2)
        state = "failed"
    from ._authorization import record_audit

    record_audit(
        {
            "event": "lifecycle_action",
            "identity": identity,
            "agent": name,
            "action": action,
            "cross_host": cross_host,
            "state": state,
            "message": str(message)[:240],
            "raw_message": locals().get("raw_message", "")[:240],
            "path": request.path,
        }
    )
    query = urlencode({"operation": action, "state": state, "message": str(message)[:240]})
    return HttpResponseRedirect(f"{base}/{name}/?{query}")


@require_POST
def message_action(request: HttpRequest, name: str):
    """Send a message/steer or a UI-control key to an agent's own bridge.

    Delegates to the agent's published ``turn_url`` — the SAME endpoint the
    fleet's A2A send uses. The GUI never constructs a host/port, never touches
    tmux, and holds no queue: a delivery is either reported delivered or
    reported failed.

    Authorization is identical to :func:`lifecycle_action`: the caller must see
    the agent AND be allowed to control it. A refusal is audited.
    """
    from ._authorization import record_audit
    from ._control import (
        exactly_one_of,
        new_dispatch_id,
        send_control_key,
        send_message,
    )

    fleet = RemoteFleet.from_environment()
    identity = resolve_identity(request)
    base = _mount_base(request, f"{name}/message")

    def field(key: str) -> str:
        """A single POST value as text. ``get`` can return a list, and a list
        has no ``.strip()`` — reading it as one value is the honest shape."""
        value = request.POST.get(key, "")
        return value if isinstance(value, str) else (value[0] if value else "")

    message = field("message")
    control_key = field("control_key")
    dispatch_id = field("dispatch_id") or new_dispatch_id()

    if not exactly_one_of(message.strip(), control_key.strip()):
        query = urlencode(
            {
                "operation": "message",
                "state": "refused",
                "message": "Provide either a message or a single control key.",
            }
        )
        return HttpResponseRedirect(f"{base}/{name}/?{query}")

    try:
        rows = scope_rows(fleet.list_all(), identity)
    except Exception as exc:  # stx-allow: fallback (reason: cannot authorize what cannot be listed)
        return JsonResponse({"error": f"cannot reach listener: {exc}"}, status=502)
    row = next((r for r in rows if r.get("name") == name), None)
    if row is None:
        return HttpResponseForbidden("This agent is not within your scope.")
    cross_host = row.get("scope") == "cross-host"
    if not can_control(identity, cross_host=cross_host, request=request, agent=name):
        record_audit(
            {
                "event": "message_denied",
                "identity": identity,
                "agent": name,
                "path": request.path,
            }
        )
        return HttpResponseForbidden("You are not authorized for this action.")

    turn_url = row.get("turn_url")
    if control_key.strip():
        result = send_control_key(turn_url, key=control_key.strip(), dispatch_id=dispatch_id)
    else:
        result = send_message(turn_url, text=message, dispatch_id=dispatch_id)

    record_audit(
        {
            "event": "message_action",
            "identity": identity,
            "agent": name,
            "mode": result.mode,
            "cross_host": cross_host,
            "state": result.state,
            "dispatch_id": result.dispatch_id,
            "message": result.message[:240],
            "path": request.path,
        }
    )
    query = urlencode(
        {
            "operation": "control" if result.mode == "control" else "message",
            "state": result.state,
            "message": result.message[:240],
            "dispatch_id": result.dispatch_id,
        }
    )
    return HttpResponseRedirect(f"{base}/{name}/?{query}")


def _summary(agents: list[dict]) -> dict:
    total = len(agents)
    alive = sum(1 for a in agents if a["state_tone"] == "good")
    attention = sum(1 for a in agents if a["state_tone"] in {"warn", "bad"})
    cross = sum(1 for a in agents if a["cross_host"])
    return {"total": total, "alive": alive, "attention": attention, "cross_host": cross}


def _crosshost_allowlist() -> frozenset[str]:
    import os

    from ._constants import CROSSHOST_OPERATORS_ENV

    return frozenset(p.strip() for p in os.environ.get(CROSSHOST_OPERATORS_ENV, "").split(",") if p.strip())


__all__ = ["detail", "fleet_api", "index", "lifecycle_action"]
