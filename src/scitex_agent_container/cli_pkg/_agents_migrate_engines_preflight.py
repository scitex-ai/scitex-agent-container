"""``--preflight`` — is the gateway the migration points at actually there?

Split from :mod:`._agents_migrate_engines_report` on the module line budget,
and it is a different question anyway: that module reports the CENSUS (which
specs, which roots, what would be written), this one reports one HTTP round
trip to the address the sweep writes into every spec.

THE PROBE IS THREE-VALUED BECAUSE THE FAILURES LOOK ALIKE. Through curl,
``scitex-compute-04:18772`` answers 401 (reachable and auth-gating) while
``compute-04:18772`` answers ``000`` — which reads as "the gateway is down"
and means "the hostname does not resolve". And it dials ``/v1/models`` rather
than the base, which answers 404: a 401 there proves the inference API is
present AND gating, while the base's 404 proves only that some process holds
the port. See :mod:`...config._engine_reach`.
"""

from __future__ import annotations

from ._agents_migrate_engines_report import _lit
from ._helpers import console

__all__ = ["preflight_payload", "render_preflight"]


def preflight_payload() -> dict:
    """Probe the gateway the migration writes into every spec.

    ``/v1/models``, NOT the base. The base answers 404 — measured — and a
    preflight that dials it reports "something is listening" from a path the
    gateway does not serve. Both addresses are in the payload so a reader can
    see which one was dialled rather than inferring it.
    """
    from ..config._engine_reach import reach_verdict
    from ..config._qwen_gateway import (
        QWEN_GATEWAY_PROBE_PATH,
        QWEN_GATEWAY_PROVIDER,
        qwen_gateway_probe_url,
        qwen_gateway_url,
    )

    url = qwen_gateway_probe_url()
    verdict = reach_verdict(url)
    return {
        "provider": QWEN_GATEWAY_PROVIDER,
        "url": url,
        "base_url": qwen_gateway_url(),
        "probe_path": QWEN_GATEWAY_PROBE_PATH,
        "state": verdict.state,
        "detail": verdict.detail,
        "http_status": verdict.http_status,
        "proves_listening": verdict.proves_listening,
        # The load-bearing one: is the INFERENCE API served at that address?
        # ``proves_listening`` is true of a 404 too, and a 404 is what the
        # base returns, so a report keying on it goes green on no evidence.
        "serves_endpoint": verdict.serves_endpoint,
        "proves_absent": verdict.proves_absent,
        "undetermined": verdict.undetermined,
    }


def render_preflight(payload: dict) -> None:
    if payload["serves_endpoint"]:
        colour = "green"
    elif payload["proves_absent"]:
        colour = "red"
    else:
        # Undetermined AND listening-wrong-path land here. Neither is a
        # negative and neither is evidence the API is there.
        colour = "yellow"
    console.print(
        f"[bold]gateway preflight[/bold] {_lit(payload['url'])} "
        f"([{colour}]{_lit(payload['state'])}[/{colour}])\n"
        f"  {_lit(payload['detail'])}",
        soft_wrap=True,
    )
    if payload["undetermined"]:
        console.print(
            "  [dim]UNDETERMINED is not a negative. Nothing here says the "
            "gateway is down.[/dim]",
            soft_wrap=True,
        )
