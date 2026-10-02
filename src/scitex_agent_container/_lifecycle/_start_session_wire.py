"""Explicit start session policy at the client and host protocol boundary."""

from __future__ import annotations

SESSION_PROTOCOL = "sac.start-session/v1"


def start_session_capability() -> dict:
    """Typed host support, advertised only after host-bearer authentication."""
    return {"protocol": SESSION_PROTOCOL, "modes": ["continue", "resume", "fresh"]}


def normalize_start_session(value: object) -> str | None:
    """Absent means preserve the host spec; reject unknown or non-string input."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("'session' must be fresh, continue, resume, or new-session")
    mode = value.strip().lower()
    if mode == "new-session":
        mode = "fresh"
    if mode not in {"fresh", "continue", "resume"}:
        raise ValueError("'session' must be fresh, continue, resume, or new-session")
    return mode


def start_session_cli_args(value: object) -> list[str]:
    """Use the public host CLI to enforce the selected harness's continuity."""
    mode = normalize_start_session(value)
    return [] if mode is None else ["--session", mode]


def require_host_session_support(mode, *, base_url, bearer, timeout_s, opener):
    """Refuse an old/unknown host before a mutating POST can drop continuity."""
    from ._in_sif_http_client import HostListenTransportError, host_listen_call
    from ._listen_client_resolve import SpawnRequestError

    try:
        status, body = host_listen_call(
            "GET",
            "/v1/health",
            base_url=base_url,
            bearer=bearer,
            timeout_s=timeout_s,
            opener=opener,
        )
    except HostListenTransportError as exc:
        raise SpawnRequestError(
            f"explicit session {mode!r} refused before POST: {exc}"
        ) from exc
    capability = (
        body.get("capabilities", {}).get("start_session")
        if isinstance(body, dict) and isinstance(body.get("capabilities"), dict)
        else None
    )
    if (
        status != 200
        or not isinstance(body, dict)
        or body.get("service") != "sac-listen"
        or not isinstance(capability, dict)
        or capability.get("protocol") != SESSION_PROTOCOL
        or not isinstance(capability.get("modes"), list)
        or mode not in capability["modes"]
    ):
        raise SpawnRequestError(
            f"explicit session {mode!r} refused before POST: host has no authenticated "
            f"{SESSION_PROTOCOL} capability; qualify a matched whole host artifact first",
            status=status,
            body=body,
        )
