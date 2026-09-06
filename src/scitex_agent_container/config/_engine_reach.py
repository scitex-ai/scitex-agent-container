"""Is the engine endpoint THERE? — a named state, never a boolean.

MEASURED BY scitex-hub, 2026-09-05, against the one Qwen gateway:

    scitex-compute-04:18772   ->  HTTP 401   listening and auth-gating
    compute-04:18772          ->  000        the NAME does not resolve
    compute-04-lan:18772      ->  000        the NAME does not resolve

Two facts live in that table and a boolean carries neither of them.

**401 is the CORRECT answer for a healthy gateway.** It proves something is
listening on that port and demanding a key. A check that treats a non-2xx
response as failure reports the working gateway as broken.

**000 is not a verdict.** curl prints it for an unresolvable hostname exactly
as it prints it for a dead host, so "the Qwen gateway is down" and "you spelled
the host wrong" arrive as the same three characters. Anyone following the
fleet's ``-lan`` naming convention gets ``000`` from a gateway that is up.
Collapsing those into one boolean is how a correct migration gets abandoned on
a false negative — so this module refuses to.

THE STATES, and the three the measurement above forced apart:

  ``reachable-but-unauthorized``  the endpoint answered 401/403. Something IS
                                  listening and IS demanding a key. REACHABLE.
  ``listening``                   the endpoint answered anything else. Also
                                  reachable; kept apart from the 401 case only
                                  so a report can say which one it saw.
  ``connection-refused``          the name resolved, the port answered, and it
                                  said closed. A DEFINITE negative.
  ``name-does-not-resolve``       DNS gave nothing. This says NOTHING about the
                                  gateway. Undetermined.
  ``timed-out``                   no answer inside the bounded timeout. Also
                                  undetermined — a slow path is not a dead one.
  ``no-host-in-url``              the URL carries no host to dial at all.

DNS IS CHECKED FIRST AND SEPARATELY, before any connect. Folded into the
connect, a resolution failure surfaces as one more socket error among many and
lands in the same bucket as a timeout — which is the bucket that reads as
"down". :func:`._engine_honour.probe_verdict` does exactly that fold (every
non-refusal ``OSError`` becomes ``could-not-tell``); it is correct for its job,
which is deciding whether to refuse a START, and it deliberately never refuses
on a name it could not resolve. This module answers the operator's different
question — WHICH of those things happened — and is the one a preflight prints.

No key is ever sent, so a 401 is the expected outcome and not a failure to
authenticate: this asks whether the door exists, not whether we may enter.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

__all__ = [
    "REACH_LISTENING",
    "REACH_NAME_UNRESOLVED",
    "REACH_NO_HOST",
    "REACH_REFUSED",
    "REACH_STATES",
    "REACH_TIMED_OUT",
    "REACH_TRANSPORT_ERROR",
    "REACH_UNAUTHORIZED",
    "ReachVerdict",
    "reach_verdict",
]

REACH_LISTENING = "listening"
REACH_UNAUTHORIZED = "reachable-but-unauthorized"
REACH_REFUSED = "connection-refused"
REACH_NAME_UNRESOLVED = "name-does-not-resolve"
REACH_TIMED_OUT = "timed-out"
REACH_NO_HOST = "no-host-in-url"
REACH_TRANSPORT_ERROR = "transport-error"

#: Every state this module can return. A caller switching on the state is
#: expected to handle all of them; there is no residual "other".
REACH_STATES = (
    REACH_LISTENING,
    REACH_UNAUTHORIZED,
    REACH_REFUSED,
    REACH_NAME_UNRESOLVED,
    REACH_TIMED_OUT,
    REACH_NO_HOST,
    REACH_TRANSPORT_ERROR,
)

#: Bounded, and short. This is a preflight a human reads, not a health check.
REACH_TIMEOUT_S = 3.0

_SENTENCES = {
    REACH_LISTENING: ("the endpoint answered — something is listening at this address"),
    REACH_UNAUTHORIZED: (
        "reachable but unauthorized (401) — this is the CORRECT answer for a "
        "live gateway: something is listening and demanding a key"
    ),
    REACH_REFUSED: (
        "connection refused — the name resolved and the host actively said the "
        "port is closed. This is a definite negative"
    ),
    REACH_NAME_UNRESOLVED: (
        "the NAME does not resolve — this is NOT evidence the gateway is down, "
        "it is evidence the hostname is wrong. curl reports 000 here, the same "
        "000 it reports for a dead host"
    ),
    REACH_TIMED_OUT: (
        "timed out — undetermined. A slow or filtered path is not a dead one"
    ),
    REACH_NO_HOST: (
        "the URL carries no host to dial, so reachability could not be asked"
    ),
    REACH_TRANSPORT_ERROR: (
        "the transport failed before any answer — undetermined, not a negative"
    ),
}


@dataclass(frozen=True)
class ReachVerdict:
    """What the endpoint did, named. Deliberately not castable to a bool."""

    url: str
    state: str
    detail: str
    host: str = ""
    port: int = 0
    http_status: "int | None" = None

    def __post_init__(self) -> None:
        if self.state not in REACH_STATES:
            raise ValueError(
                f"unknown reachability state {self.state!r}; "
                f"expected one of {REACH_STATES}"
            )

    @property
    def proves_listening(self) -> bool:
        """Something IS there. A 401 counts — that is the whole point."""
        return self.state in (REACH_LISTENING, REACH_UNAUTHORIZED)

    @property
    def proves_absent(self) -> bool:
        """Something is definitely NOT there. Only a refusal earns this."""
        return self.state == REACH_REFUSED

    @property
    def undetermined(self) -> bool:
        """Neither of the above. An unresolvable NAME lands here, not in absent."""
        return not self.proves_listening and not self.proves_absent

    def describe(self) -> str:
        where = f"{self.host}:{self.port}" if self.host else self.url
        return f"{where} -> {self.state}: {self.detail}"


def _verdict(url, state, *, host="", port=0, status=None, extra="") -> ReachVerdict:
    detail = _SENTENCES[state]
    if extra:
        detail = f"{detail} ({extra})"
    return ReachVerdict(
        url=url, state=state, detail=detail, host=host, port=port, http_status=status
    )


def reach_verdict(url: str, *, timeout_s: float = REACH_TIMEOUT_S) -> ReachVerdict:
    """Probe ``url`` and name what happened. One DNS lookup, one HTTP request.

    Never raises for a network condition — an unreachable endpoint is an
    ANSWER here, not an error. The only exception it can propagate is a
    programming fault such as a non-string ``url``.
    """
    parsed = urlparse(str(url))
    host = parsed.hostname
    if not host:
        return _verdict(url, REACH_NO_HOST)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    # DNS FIRST, on its own. Folding it into the connect is what makes an
    # unresolvable name indistinguishable from a dead host.
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return _verdict(
            url, REACH_NAME_UNRESOLVED, host=host, port=port, extra=str(exc)
        )

    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return _verdict(
                url,
                REACH_LISTENING,
                host=host,
                port=port,
                status=getattr(response, "status", None),
            )
    except urllib.error.HTTPError as exc:
        # An HTTP status IS an answer, so every one of these is REACHABLE.
        state = REACH_UNAUTHORIZED if exc.code in (401, 403) else REACH_LISTENING
        return _verdict(
            url, state, host=host, port=port, status=exc.code, extra=f"HTTP {exc.code}"
        )
    except urllib.error.URLError as exc:
        return _classify_url_error(url, exc.reason, host, port)
    except (TimeoutError, socket.timeout) as exc:
        return _verdict(url, REACH_TIMED_OUT, host=host, port=port, extra=str(exc))
    except OSError as exc:  # stx-allow: fallback (reason: an unreachable endpoint is an ANSWER; a socket fault must be reported as undetermined, never raised into a caller that asked a yes/no question)
        return _verdict(
            url, REACH_TRANSPORT_ERROR, host=host, port=port, extra=str(exc)
        )


def _classify_url_error(url, reason, host, port) -> ReachVerdict:
    """Name the cause urllib wrapped, rather than reporting the wrapper.

    ``URLError`` carries the real socket error in ``.reason``; reporting the
    wrapper is what produces "URLError: <urlopen error ...>" in a preflight
    that was asked which of three specific things happened.
    """
    if isinstance(reason, ConnectionRefusedError):
        return _verdict(url, REACH_REFUSED, host=host, port=port, extra=str(reason))
    if isinstance(reason, socket.gaierror):
        return _verdict(
            url, REACH_NAME_UNRESOLVED, host=host, port=port, extra=str(reason)
        )
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return _verdict(url, REACH_TIMED_OUT, host=host, port=port, extra=str(reason))
    return _verdict(url, REACH_TRANSPORT_ERROR, host=host, port=port, extra=str(reason))
