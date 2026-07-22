"""Overlay-masking verdict, rendered for ``sac agents check-health``.

Sibling of :mod:`._health_liveness`, same doctrine: the observation is
published NEXT TO the ``healthy`` bool, never folded into it. ``healthy``
gates the command's exit code and automation keys on that; a MASKED overlay
is a standing configuration hazard, not a dead process, and must not make a
live agent read as down. The verdict is three-valued (masked / clean /
unknown) because the detector can genuinely fail to see (overlay root
missing on this host, unreadable upper, unreadable base package set) — and
"could not tell" must render as UNKNOWN, never as clean.

Detector: :mod:`.._maintenance._overlay_masking` (2026-07-22 incident —
historical pip installs in overlay uppers silently shadowed the base venv's
``scitex_cards`` across base rebuilds).
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
    """Resolve ``name``'s overlay-masking verdict as a JSON-ready dict.

    Tolerant by construction: any failure to gather degrades to an UNKNOWN
    verdict carrying the reason — never to a fabricated CLEAN, and never to
    an exception that takes the health command down.
    """
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
    """Print the verdict AND why; on MASKED, print the operational rule."""
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
