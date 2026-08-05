"""Overlay-masking observation for ``sac agents health``.

Published NEXT TO ``healthy`` (never folded into the exit code), same as
:mod:`._health_liveness`. Detector: :mod:`.._maintenance._overlay_masking`.
"""

from __future__ import annotations

from typing import Any

__all__ = ["overlay_masking_payload", "print_overlay_masking"]

_VERDICT_COLOUR = {
    "masked": "red",
    "clean": "green",
    "unknown": "yellow",
}


def overlay_masking_payload(name: str, config: Any) -> dict:
    """JSON-ready verdict dict; any gather failure degrades to UNKNOWN."""
    from .._maintenance._overlay_masking import inspect_agent_overlay
    from .._maintenance._overlay_masking_model import (
        REASON_INSPECT_ERROR,
        VERDICT_UNKNOWN,
        OverlayMaskVerdict,
    )

    try:
        return inspect_agent_overlay(name, config).to_dict()
    except Exception as exc:  # stx-allow: fallback (reason: an un-inspectable overlay is UNKNOWN with its reason — never a fabricated CLEAN, and never a crashed health command)
        return OverlayMaskVerdict(
            agent=name,
            overlay_root="",
            verdict=VERDICT_UNKNOWN,
            reason=REASON_INSPECT_ERROR,
            detail=f"could not inspect overlay ({type(exc).__name__}: {exc})",
        ).to_dict()


def print_overlay_masking(console: Any, payload: dict) -> None:
    """One verdict line; on MASKED, each shadow plus the operational rule."""
    from .._maintenance._overlay_masking_model import OPERATIONAL_RULE

    verdict = str(payload.get("verdict", "unknown"))
    colour = _VERDICT_COLOUR.get(verdict, "yellow")
    detail = payload.get("detail") or payload.get("reason") or "?"
    console.print(f"[{colour}]overlay: {verdict} — {detail}[/{colour}]")
    if verdict != "masked":
        return
    for shadow in payload.get("shadows", []):
        if shadow.get("status") == "masked":
            console.print(
                f"[red]  {shadow.get('package')} {shadow.get('version')} "
                f"masks base {shadow.get('base_version')} — "
                f"{shadow.get('dist_info')}[/red]"
            )
    console.print(f"[dim]  {OPERATIONAL_RULE}[/dim]")
