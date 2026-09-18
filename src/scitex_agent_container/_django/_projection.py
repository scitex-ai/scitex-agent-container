"""Project raw /agents + /status rows into the small, safe dashboard shape.

The dashboard exposes a deliberately small whitelist: name, state, liveness,
runtime, harness, role, engine/model, host, a2a reachability, and PID. It does
NOT expose spec paths, state dirs, workdirs, session ids, or any credential-
adjacent material — the listener's rows carry those, and we drop them here on
purpose. Every field is read with ``.get`` so a row that is missing a key
degrades to an explicit value rather than a crash.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

# Map a coarse liveness/status word to an operator-facing (label, tone).
# Tones are rendered as CSS classes in the template.
_PRESENTATION: dict[str, tuple[str, str]] = {
    "alive": ("Alive", "good"),
    "running": ("Running", "good"),
    "active": ("Active", "good"),
    "dead": ("Dead", "bad"),
    "stopped": ("Stopped", "muted"),
    "not_running": ("Stopped", "muted"),
    "wedged": ("Wedged", "warn"),
    "startup_failed": ("Startup failed", "warn"),
    "startup_failed_superseded": ("Recovered", "warn"),
    "stale_latched": ("Provider stale-latched", "warn"),
    "recovering": ("Provider recovering", "warn"),
    "unknown": ("Unknown", "warn"),
    "not_started": ("Not started", "muted"),
    "ambiguous_registry": ("Ambiguous registry", "warn"),
    "spec_unreadable": ("Spec unreadable", "warn"),
}


def _text(value: Any, default: str = "") -> str:
    return str(value) if value not in (None, "") else default


# Typed listener error kinds -> operator-facing (label, tone). A non-2xx from
# /agents/<name>/status carries a `kind`; collapsing it to a bare "Unknown"
# would hide the real, fixable cause (e.g. an ambiguous spec). These are the
# kinds declared by the committed status route.
_KIND_PRESENTATION: dict[str, tuple[str, str]] = {
    "spec_resolution_failed": ("Spec invalid", "warn"),
    "ambiguous_registry": ("Ambiguous registry", "warn"),
    "unknown_agent": ("No such agent", "muted"),
    "spec_unreadable": ("Spec unreadable", "warn"),
}


def _error_state(status: Any) -> tuple[str, str, str] | None:
    """(label, tone, detail) when the status read failed with a typed kind."""
    from ._remote import RemoteOperationError

    if isinstance(status, RemoteOperationError) and status.kind:
        label, tone = _KIND_PRESENTATION.get(status.kind, (status.kind.replace("_", " ").title(), "warn"))
        return label, tone, str(status)
    return None


def _liveness_verdict(status: dict[str, Any]) -> str:
    """The status endpoint reports ``liveness`` as ``{"verdict": "DEAD"}``."""
    lv = status.get("liveness")
    if isinstance(lv, dict):
        return str(lv.get("verdict") or "").strip()
    return str(lv or status.get("liveness_verdict") or "").strip()


def _affirmatively_live(row: dict[str, Any], status: dict[str, Any]) -> bool:
    """Whether current observations prove the owning runtime is live."""
    for source in (status, row):
        verdict = _liveness_verdict(source).lower()
        if verdict:
            return verdict == "alive"
    for source in (status, row):
        coarse = _text(source.get("status") or source.get("state")).lower()
        if coarse:
            return coarse in {"alive", "running", "active", "ready"}
    return False


def _active_runtime_control(
    row: dict[str, Any], status: dict[str, Any]
) -> tuple[str, str] | None:
    """Return a degraded admission state only for a proven-live runtime."""
    if not _affirmatively_live(row, status):
        return None
    for source in (status, row):
        control = source.get("runtime_control")
        if isinstance(control, dict):
            admission = _text(control.get("turn_admission")).lower()
            if admission in {"stale_latched", "recovering"}:
                return admission, _text(control.get("detail"))
    return None


def _state(row: dict[str, Any], status: dict[str, Any]) -> tuple[str, str]:
    """Derive (label, tone) from the status endpoint first, then the row.

    Degraded turn admission refines only an affirmatively live observation.
    Otherwise precedence is ``liveness.verdict`` (the authoritative
    ALIVE/DEAD/WEDGED ternary), then ``status`` (a coarse word like ``stopped``
    / ``startup_failed``), then a row-level ``status``. Empty/unknown falls
    through to the explicit ``Unknown`` state.
    """
    # Turn admission is orthogonal to process liveness, but its persisted
    # observation must never mask authoritative dead/not-started liveness.
    runtime_control = _active_runtime_control(row, status)
    if runtime_control is not None:
        return _PRESENTATION[runtime_control[0]]
    for source in (status, row):
        verdict = _liveness_verdict(source).lower()
        if verdict:
            if verdict in _PRESENTATION:
                return _PRESENTATION[verdict]
            return verdict.replace("_", " ").replace("-", " ").title(), "muted"
    for source in (status, row):
        coarse = _text(source.get("status") or source.get("state")).lower()
        if coarse:
            if coarse in _PRESENTATION:
                return _PRESENTATION[coarse]
            return coarse.replace("_", " ").replace("-", " ").title(), "muted"
    return "Unknown", "warn"


def _node(row: dict[str, Any], turn_url: Any) -> str:
    """The node an agent lives on — from ``turn_url``'s hostname, else ``host``."""
    if isinstance(turn_url, str) and turn_url:
        try:
            host = urlsplit(turn_url).hostname
        except ValueError:
            host = None
        if host:
            return host
    host = row.get("host")
    return str(host) if host not in (None, "") else ""


_ACTIVITY_SIGNALS = (
    "phase",
    "operation",
    "turn_elapsed",
    "last_progress",
    "queue",
    "inference",
    "tool",
    "wait",
)


def _activity(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = status.get("activity")
    if not isinstance(raw, dict):
        raw = {}
    projected = {}
    for name in _ACTIVITY_SIGNALS:
        signal = raw.get(name)
        if not isinstance(signal, dict) or signal.get("state") != "observed":
            reason = signal.get("reason") if isinstance(signal, dict) else ""
            projected[name] = {
                "state": "unknown",
                "value": None,
                "source": "",
                "reason": _text(
                    reason, "No authoritative runtime evidence is published."
                )[:160],
            }
            continue
        value = signal.get("value")
        if not isinstance(value, (str, int, float, bool)):
            projected[name] = {
                "state": "unknown",
                "value": None,
                "source": "",
                "reason": "The runtime observation had an unsupported value.",
            }
            continue
        projected[name] = {
            "state": "observed",
            "value": value,
            "source": _text(signal.get("source"))[:80],
            "reason": "",
        }
    return projected


def project_row(row: dict[str, Any], status: Any) -> dict[str, Any]:
    """Combine a list row with its status observation into one dashboard row."""
    err = _error_state(status)
    if isinstance(status, Exception) or status in (None, ""):
        status = {}
    if not isinstance(status, dict):
        status = {}
    name = _text(row.get("name"), _text(status.get("name"), "unknown"))
    if err is not None:
        label, tone, detail = err
    else:
        label, tone = _state(row, status)
        runtime_control = _active_runtime_control(row, status)
        detail = runtime_control[1] if runtime_control is not None else ""
    a2a_port = row.get("a2a_port", status.get("a2a_port"))
    turn_url = row.get("turn_url", status.get("turn_url"))
    host = _node(row, turn_url)
    # /status publishes active birth-certificate identity; /agents may carry a
    # stale declaration. Status therefore wins whenever it has a value.
    runtime = _text(status.get("runtime") or row.get("runtime"), "unknown")
    harness = _text(status.get("harness") or row.get("harness"), "unknown")
    engine = _text(status.get("engine") or row.get("engine"), "default")
    model = _text(status.get("model") or row.get("model"), "unknown")
    billing_mode = _text(
        status.get("billing_mode") or row.get("billing_mode"), "unspecified"
    )
    auth_identity = _text(
        status.get("auth_identity") or row.get("auth_identity"), "unknown"
    )
    identity_source = _text(
        status.get("runtime_identity_source") or row.get("runtime_identity_source"),
        "unknown",
    )
    role = row.get("role")
    if isinstance(role, list):
        role = ", ".join(str(r) for r in role)
    pid = row.get("pid", status.get("pid"))
    scope = row.get("scope", "own")
    return {
        "name": name,
        "state_label": label,
        "state_tone": tone,
        "state_detail": detail,
        "runtime": runtime,
        "harness": harness,
        "role": _text(role, "—"),
        "engine": engine,
        "model": model,
        "billing_mode": billing_mode,
        "auth_identity": auth_identity,
        "runtime_identity_source": identity_source,
        "project": _text(row.get("project"), "—"),
        "host": host or "this node",
        "scope": scope,
        "cross_host": scope == "cross-host",
        "a2a_port": a2a_port,
        "turn_url": turn_url,
        "pid": pid,
        "activity": _activity(status),
    }


def project_detail(row: dict[str, Any], status: Any) -> dict[str, Any]:
    """Project a single agent's detail (list row + status + optional spec)."""
    base = project_row(row, status)
    if isinstance(status, Exception) or not isinstance(status, dict):
        base["detail_error"] = str(getattr(status, "message", status) or status) if isinstance(
            status, Exception
        ) else ""
        base["detail"] = {}
        base["session"] = "—"
    else:
        base["detail_error"] = ""
        session = _text(status.get("session_id"), "—")
        base["session"] = session[:8] if len(session) > 8 else session
        base["detail"] = {
            "started_at": _text(row.get("started_at"), "—"),
            "a2a_port": status.get("a2a_port", row.get("a2a_port")),
            "turn_url": status.get("turn_url", row.get("turn_url")),
            "inbox_reachable": _text(status.get("inbox_reachable"), "unknown"),
        }
    return base


__all__ = ["project_detail", "project_row"]
