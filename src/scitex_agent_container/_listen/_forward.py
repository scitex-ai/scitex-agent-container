"""Live-runner forwarding for ``sac listen``.

Extracted from ``server.py`` to keep that module under the 512-line
cap. Single responsibility: given an :class:`AgentConfig` and a
prompt, POST to the live runner's per-agent A2A sidecar and return
the response (or ``None`` to signal the caller should fall back to
the heavier ``claude --resume`` re-launch path).

Port resolution order (introduced with the auto-allocator):

1. ``port_allocator.get_port(name)`` — the actual port the runner
   bound at start time, from state.db. This is the source of truth.
2. ``cfg.a2a.port`` if an explicit int — for legacy agents started
   before the allocator landed and never recorded.
3. None → no live runner; caller re-launches.

The ``"auto"`` sentinel on ``cfg.a2a.port`` is intentionally NOT
treated as a port — it only ever means "the start-time allocator
should pick one." A non-numeric value here means the agent was
never started, so there's nothing live to forward to.

WHY THE TRANSPORT OUTCOME IS A DECLARED SHAPE, not a bare sentinel
------------------------------------------------------------------
This module used to answer every transport failure with the single
value ``-1``, which the caller turned into ``None`` — the SAME answer
it gives for "this agent has no port at all" (case 3 above). Two
states with opposite meanings collapsed into one:

  * NO_PORT  — nothing was ever bound; re-launching is correct.
  * REFUSED  — a port IS recorded and the kernel refused instantly;
    the agent is very likely ALIVE with its sidecar down.

Because both arrived as ``None``, a REFUSED send fell through to the
``claude --resume`` re-launch, which is wrong twice over. It is slow
(the caller's own 30s client timeout fires long before a fresh claude
finishes, so the reply is never seen and the work is burned invisibly),
and it is UNSAFE: re-launching ``claude --resume <sid>`` against a
session id that a live agent still holds puts two processes on one
session file. The information needed to avoid both was available
immediately — nothing was listening — and collapsing it into a timeout
is what made a routine unbound sidecar read as a daemon-wide outage
(card sac-listen-send-endpoint-wedged-fleet-wide-20260803, where the
misreading cost two wrong remedies before the right test was run).

So the transport now answers with :class:`ForwardOutcome` — one shape,
every signal a named field, ``kind`` validated at construction — and
REFUSED / TIMEOUT / UNREACHABLE each get their own loud HTTP status
naming the port. ``None`` is reserved for NO_PORT alone, which is what
the docstring above always said it meant.
"""

from __future__ import annotations

import asyncio
import json as _json
import urllib.error as _urlerror
import urllib.request as _urlrequest
from dataclasses import dataclass

from starlette.responses import JSONResponse

from .._state import port_allocator

__all__ = [
    "ForwardOutcome",
    "forward_to_live_runner",
    "post_to_live_runner",
]

#: The complete set of transport outcomes. Every send lands on exactly
#: one of these — there is no "other" and no implicit fallthrough.
KINDS = frozenset(
    {
        "reached",  # the sidecar answered (``http_status`` is set)
        "refused",  # kernel refused the connection: nothing is bound
        "timeout",  # bound but did not answer within the deadline
        "unreachable",  # any other transport error (DNS, network, ...)
    }
)


@dataclass(frozen=True)
class ForwardOutcome:
    """One send's transport result, in a fixed shape with a validator.

    ``kind`` is the multi-valued signal the old ``-1`` destroyed.
    ``http_status`` is set if and only if ``kind == "reached"`` — a
    status on any other kind would be an invented answer, so the
    validator refuses to build one.
    """

    kind: str
    port: int
    url: str
    http_status: int | None = None
    payload: bytes = b""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(
                f"ForwardOutcome.kind={self.kind!r} is not one of {sorted(KINDS)}"
            )
        if self.kind == "reached":
            if self.http_status is None:
                raise ValueError("kind='reached' requires an http_status")
        elif self.http_status is not None:
            raise ValueError(
                f"kind={self.kind!r} must not carry http_status="
                f"{self.http_status!r} — only a reached sidecar has one"
            )

    @property
    def failed(self) -> bool:
        return self.kind != "reached"


async def post_to_live_runner(
    url: str, port: int, prompt: str, *, timeout: float
) -> ForwardOutcome:
    """POST ``prompt`` to a runner sidecar and classify what happened.

    Split from :func:`forward_to_live_runner` so the transport decides
    only WHAT HAPPENED and the caller decides WHAT TO DO — the two were
    entangled in the collapsed-sentinel version.
    """
    body = _json.dumps({"text": prompt}).encode("utf-8")
    req = _urlrequest.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def _do_post() -> ForwardOutcome:
        try:
            with _urlrequest.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return ForwardOutcome(
                    kind="reached",
                    port=port,
                    url=url,
                    http_status=resp.status,
                    payload=resp.read(),
                )
        except _urlerror.HTTPError as exc:
            # An HTTP error IS an answer — the sidecar is up and spoke.
            return ForwardOutcome(
                kind="reached",
                port=port,
                url=url,
                http_status=exc.code,
                payload=exc.read(),
            )
        except _urlerror.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ConnectionRefusedError):
                return ForwardOutcome(
                    kind="refused", port=port, url=url, detail=str(reason)
                )
            # socket.timeout is an alias of TimeoutError on py3.10+.
            if isinstance(reason, TimeoutError):
                return ForwardOutcome(
                    kind="timeout", port=port, url=url, detail=str(reason)
                )
            return ForwardOutcome(
                kind="unreachable", port=port, url=url, detail=str(reason)
            )
        except TimeoutError as exc:
            # urllib raises the bare socket timeout on the read side.
            return ForwardOutcome(kind="timeout", port=port, url=url, detail=str(exc))

    return await asyncio.to_thread(_do_post)


def _refused_response(name: str, outcome: ForwardOutcome) -> JSONResponse:
    """503 for a recorded-but-unbound sidecar, naming the port.

    Deliberately NOT a death verdict, and deliberately not a re-launch.
    Most agents in this fleet never bind ``/v1/turn`` and are reached on
    the a2a subscriber channel instead, so "nothing is listening" says
    the TRANSPORT is unavailable, never that the agent is gone. The same
    distinction is drawn, in the same words, by the CLI-side preflight in
    ``cli_pkg/_send.py`` — keep the two wordings in step.
    """
    return JSONResponse(
        {  # stx-allow: STX-SAC001 (reason: a transport ERROR payload — name+url+a2a_port of the sidecar we failed to reach — NOT an A2A AgentCard; the v0-field heuristic false-positives on the name+url pair, exactly as it does on the send-reply contract in cli_pkg/_send.py)
            "name": name,
            "route": "live-runner",
            "kind": "refused",
            "a2a_port": outcome.port,
            "url": outcome.url,
            "error": (
                f"agent {name!r}: nothing is listening on a2a port "
                f"{outcome.port}, so the /v1/turn transport cannot carry "
                f"this prompt. This is NOT a death verdict — most agents "
                f"in this fleet never bind /v1/turn and are reached over "
                f"the a2a subscriber channel instead."
            ),
            "hint": (
                f"Deliver with `sac a2a send {name} ...` (or the a2a_send "
                f"tool), which does not need this port. To check the port "
                f"itself: `ss -ltn | grep {outcome.port}`. Do NOT "
                f"force-restart the agent on this signal, and do NOT read "
                f"it as a `sac listen` outage — the daemon answered you "
                f"instantly to say this."
            ),
        },
        status_code=503,
    )


def _timeout_response(
    name: str, outcome: ForwardOutcome, timeout: float
) -> JSONResponse:
    """504 for a sidecar that accepted the connection and went quiet."""
    return JSONResponse(
        {  # stx-allow: STX-SAC001 (reason: a transport ERROR payload, not an A2A AgentCard — see the note on _refused_response)
            "name": name,
            "route": "live-runner",
            "kind": "timeout",
            "a2a_port": outcome.port,
            "url": outcome.url,
            "timeout_s": timeout,
            "error": (
                f"agent {name!r}: the sidecar on a2a port {outcome.port} "
                f"accepted the connection but sent no reply within "
                f"{timeout:g}s. Something IS bound — this is not the "
                f"'no listener' case — so the runner is wedged or the turn "
                f"is genuinely still running."
            ),
            "hint": (
                f"Check whether the runner is mid-turn before restarting "
                f"anything: `sac agents status {name}`. If it is wedged, "
                f"`sac agents restart {name}`."
            ),
        },
        status_code=504,
    )


def _unreachable_response(name: str, outcome: ForwardOutcome) -> JSONResponse:
    """502 for a transport error that is neither refused nor timeout."""
    return JSONResponse(
        {  # stx-allow: STX-SAC001 (reason: a transport ERROR payload, not an A2A AgentCard — see the note on _refused_response)
            "name": name,
            "route": "live-runner",
            "kind": "unreachable",
            "a2a_port": outcome.port,
            "url": outcome.url,
            "error": (
                f"agent {name!r}: transport error reaching {outcome.url} — "
                f"{outcome.detail}"
            ),
            "hint": (
                f"This is not a refused port and not a timeout, so the "
                f"usual sidecar remedies do not apply. Verify the host and "
                f"port in the agent's spec, then retry: `sac agents status "
                f"{name}`."
            ),
        },
        status_code=502,
    )


async def forward_to_live_runner(
    cfg, name: str, prompt: str, options: dict, timeout: float = 600.0
) -> JSONResponse | None:
    """Push a prompt onto the live runner's inbox via its sidecar.

    Returns ``None`` — and ONLY ``None`` — when no port is resolvable,
    i.e. case 3 of the module docstring: there is no live runner, so the
    caller's ``claude --resume`` re-launch is the right next step.

    Every other outcome is a response, never a silent fallthrough: a
    reached sidecar's reply, or a loud 503 / 504 / 502 that names the
    port and says what to do about it.
    """
    port = port_allocator.get_port(name)
    if not port:
        a2a = getattr(cfg, "a2a", None)
        raw = getattr(a2a, "port", None) if a2a else None
        if isinstance(raw, int) and raw > 0:
            port = raw
    if not port:
        return None
    a2a = getattr(cfg, "a2a", None)
    host = getattr(a2a, "host", None) or "127.0.0.1"

    url = f"http://{host}:{port}/v1/turn"
    outcome = await post_to_live_runner(url, port, prompt, timeout=timeout)

    if outcome.kind == "refused":
        return _refused_response(name, outcome)
    if outcome.kind == "timeout":
        return _timeout_response(name, outcome, timeout)
    if outcome.kind == "unreachable":
        return _unreachable_response(name, outcome)

    status = outcome.http_status
    if status is None:  # unreachable: the validator guarantees it is set
        raise AssertionError("reached outcome without http_status")
    if status >= 400:
        return JSONResponse(
            {
                "name": name,
                "route": "live-runner",
                "status": status,
                "error": outcome.payload.decode("utf-8", "replace"),
            },
            status_code=status,
        )
    return JSONResponse(
        {
            "name": name,
            "route": "live-runner",
            "text": _json.loads(outcome.payload.decode("utf-8"))["text"],
        }
    )
