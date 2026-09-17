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

from ._authorization import can_control, resolve_identity, scope_rows
from ._constants import API_URL_ENV
from ._diagnostics import diagnostic_id, diagnostic_summary
from ._projection import project_detail, project_row
from ._remote import RemoteFleet


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
    rows = scope_rows(fleet.list_all(), identity)
    named = [str(r["name"]) for r in rows if isinstance(r.get("name"), str)]
    statuses = fleet.read_statuses(named)
    agents = [project_row(r, statuses.get(str(r.get("name")), {})) for r in rows]
    return agents, ""


def _is_configured() -> bool:
    """Whether this deployment has *chosen* a listener.

    Distinguishes "nobody told me where the control plane is" (a setup problem
    the deployer fixes) from "the listener I was given is not answering" (an
    outage the operator retries). Treating both as one banner is what the old
    page did, and it sent operators to the wrong fix.
    """
    return bool(os.environ.get(API_URL_ENV, "").strip())


def _fleet_view_state(agents: list[dict], comm_error: str) -> dict:
    """Resolve the page into exactly one of these states:

    ``setup-required | unavailable | empty | ok``

    ``denied`` is deliberately NOT resolved here. It is an AUTHORIZATION
    outcome, not a fleet-read outcome, and the two must not be conflated: a
    caller can be fully authorized for the fleet it read (``ok``) and still be
    refused a specific action. Resolving ``denied`` from the read path would
    either mask a working fleet or leak its size, so the view decides it from
    the authorization result alone (see ``index``).
    """
    if comm_error:
        return {
            "fleet_state": "setup-required" if not _is_configured() else "unavailable",
            "diagnostic_id": diagnostic_id(comm_error),
            "diagnostic_reason": diagnostic_summary(comm_error),
        }
    if agents:
        return {"fleet_state": "ok", "diagnostic_id": "", "diagnostic_reason": ""}
    return {"fleet_state": "empty", "diagnostic_id": "", "diagnostic_reason": ""}



@require_GET
def index(request: HttpRequest):
    fleet = RemoteFleet.from_environment()
    identity = resolve_identity(request)
    try:
        agents, comm_error = _fleet_rows(fleet, identity)
    except Exception as exc:  # stx-allow: fallback (reason: an unreachable listener is a STATE to show)
        agents, comm_error = [], str(exc)
    context, is_standalone = _app_context(
        request,
        "Agents",
        view_path="",
        agents=agents,
        summary=_summary(agents),
        identity=identity,
        crosshost_authorized=identity in _crosshost_allowlist(),
        comm_error=comm_error,
        listener=fleet.base_url,
        page="fleet",
        **_fleet_view_state(agents, comm_error),
    )
    template = "scitex_agent_container/fleet.html" if is_standalone else "scitex_agent_container/fleet_hub.html"
    return render(request, template, context)


@require_GET
def fleet_api(request: HttpRequest) -> JsonResponse:
    fleet = RemoteFleet.from_environment()
    identity = resolve_identity(request)
    try:
        agents, _ = _fleet_rows(fleet, identity)
        return JsonResponse({"ok": True, "identity": identity, "agents": agents, "summary": _summary(agents)})
    except Exception as exc:  # stx-allow: fallback (reason: surface the failure as JSON, not a 500 page)
        return JsonResponse({"ok": False, "error": str(exc)}, status=502)


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
            session_lines, session_error = [], str(exc)
    except Exception as exc:  # stx-allow: fallback (reason: a tail failure is a state, not fatal)
        session_lines, session_error = [], str(exc)
    return {"session_lines": session_lines, "session_error": session_error, "resources": resources}


@require_GET
def detail(request: HttpRequest, name: str):
    fleet = RemoteFleet.from_environment()
    identity = resolve_identity(request)
    try:
        rows = scope_rows(fleet.list_all(), identity)
        list_error = ""
    except Exception as exc:  # stx-allow: fallback (reason: listener unreachable is a state)
        rows, list_error = [], str(exc)
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
    context, is_standalone = _app_context(
        request, f"Agent · {name}", view_path=f"{name}/",
        agent=agent,
        identity=identity,
        cross_host=cross_host,
        can_operate=can_control(identity, cross_host=cross_host, request=request, agent=name),
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
    except Exception as exc:  # stx-allow: fallback (reason: the failed delegate is reported to the operator in the gui-audit.log audit trail, not raised)
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
