"""Browser + JSON views for the scoped Agents dashboard.

Read-only by default. A view never invents state: it asks the listener, scopes
the result to the caller, and projects it. Control is a separate, gated path.
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.http import HttpRequest, HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from ._authorization import can_control, resolve_identity, scope_rows
from ._projection import project_detail, project_row
from ._remote import RemoteFleet


def _context_base(request: HttpRequest, title: str) -> dict:
    from scitex_ui.branding import shell_context
    from scitex_ui.mount import mount_context

    # All three side panes are unused: this is a server-rendered fleet table,
    # not a file workspace. Declaring them unused is the scitex-ui API (it
    # hides the panes) rather than a stylesheet hack at private class names.
    panes = {"ai": "unused", "files": "unused", "viewer": "unused"}
    ctx = dict(shell_context(title, accent="agents", panes=panes))
    # merge mount context ONLY when a host declared a prefix — standalone has
    # none, and mount_context() without a declaration would be a wrong claim.
    try:
        ctx.update(mount_context(request, view_path=""))
    except Exception:  # stx-allow: fallback (reason: standalone mount is not declared)
        pass
    return ctx


@require_GET
def index(request: HttpRequest):
    fleet = RemoteFleet.from_environment()
    identity = resolve_identity(request)
    try:
        rows = scope_rows(fleet.list_all(), identity)
        statuses = fleet.read_statuses([r["name"] for r in rows if isinstance(r.get("name"), str)])
        agents = [project_row(r, statuses.get(r.get("name"), {})) for r in rows]
        comm_error = ""
    except Exception as exc:  # stx-allow: fallback (reason: an unreachable listener is a STATE to show)
        agents, comm_error = [], str(exc)
    summary = _summary(agents)
    context = {
        **_context_base(request, "Agents"),
        "agents": agents,
        "summary": summary,
        "identity": identity,
        "crosshost_authorized": identity in _crosshost_allowlist(),
        "comm_error": comm_error,
        "listener": fleet.base_url,
        "page": "fleet",
    }
    return render(request, "scitex_agent_container/fleet.html", context)


@require_GET
def fleet_api(request: HttpRequest) -> JsonResponse:
    fleet = RemoteFleet.from_environment()
    identity = resolve_identity(request)
    try:
        rows = scope_rows(fleet.list_all(), identity)
        statuses = fleet.read_statuses([r["name"] for r in rows if isinstance(r.get("name"), str)])
        agents = [project_row(r, statuses.get(r.get("name"), {})) for r in rows]
        return JsonResponse({"ok": True, "identity": identity, "agents": agents, "summary": _summary(agents)})
    except Exception as exc:  # stx-allow: fallback (reason: surface the failure as JSON, not a 500 page)
        return JsonResponse({"ok": False, "error": str(exc)}, status=502)


@require_GET
def detail(request: HttpRequest, name: str):
    fleet = RemoteFleet.from_environment()
    identity = resolve_identity(request)
    try:
        rows = scope_rows(fleet.list_all(), identity)
    except Exception as exc:  # stx-allow: fallback (reason: listener unreachable is a state)
        rows = []
        list_error = str(exc)
    else:
        list_error = ""
    row = next((r for r in rows if r.get("name") == name), None)
    if row is None:
        # Either not own-scope (hidden) or genuinely absent. We do not reveal
        # which — an ordinary caller simply does not see cross-host agents.
        context = {
            **_context_base(request, f"Agent · {name}"),
            "agent": None,
            "identity": identity,
            "list_error": list_error,
            "not_found": True,
            "page": "detail",
        }
        return render(request, "scitex_agent_container/detail.html", context)
    try:
        status = fleet.read_status(name)
    except Exception as exc:  # stx-allow: fallback (reason: one agent's status failing is per-agent)
        status = exc  # type: ignore[assignment]
    cross_host = row.get("scope") == "cross-host"
    context = {
        **_context_base(request, f"Agent · {name}"),
        "agent": project_detail(row, status),
        "identity": identity,
        "cross_host": cross_host,
        "can_operate": can_control(identity, cross_host=cross_host, request=request, agent=name),
        "list_error": list_error,
        "page": "detail",
    }
    return render(request, "scitex_agent_container/detail.html", context)


@require_POST
def lifecycle_action(request: HttpRequest, name: str):
    fleet = RemoteFleet.from_environment()
    identity = resolve_identity(request)
    action = request.POST.get("action", "")
    if action not in {"start", "stop", "restart"}:
        return JsonResponse({"error": "unsupported lifecycle action"}, status=400)
    # Determine scope: a cross-host agent must be resolvable from the full list.
    try:
        rows = scope_rows(fleet.list_all(), identity)
    except Exception as exc:  # stx-allow: fallback (reason: cannot authorize what cannot be listed)
        return JsonResponse({"error": f"cannot reach listener: {exc}"}, status=502)
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
    except Exception as exc:  # stx-allow: fallback (reason: a failed delegate is reported, not raised)
        message = str(exc)
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
            "path": request.path,
        }
    )
    query = urlencode({"operation": action, "state": state, "message": str(message)[:240]})
    return HttpResponseRedirect(f"../{name}/?{query}")


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
