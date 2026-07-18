"""Shared no-mocks helpers for the login-expired auto-restart suites.

Real captured panes (byte-identical to the ones the ``sac agents
auth-status`` matcher is tested against) and a real restart callable that
RECORDS instead of restarting — a plain object with the production
signature, never a mock of the thing under test. The corroboration logic
(:func:`_authheal._detect.detect_login_expired` → the real
``evaluate_agents`` matcher) runs for real against these panes.
"""

from __future__ import annotations

#: A fixed clock. Every suite injects it, so no test can be flaky on time.
NOW = 1_800_000_000.0

# Wedged: a system auth banner sits directly above the prompt, IDENTICAL on
# both reads → frozen → corroborated AUTH-FAILED. Same fixture the
# auth-status matcher suite uses.
STUCK = "● Login expired · Please run /login\n────────\n❯\n────────\n  ctx:1%\n"

# Healthy: no banner above the prompt.
OK = "  continuing the task now\n────────\n❯\n────────\n  ctx:1%\n"


def stuck(*names: str) -> dict:
    """``{name: (STUCK, STUCK)}`` — every named agent frozen-login-expired."""
    return {n: (STUCK, STUCK) for n in names}


def transient(*names: str) -> dict:
    """``{name: (STUCK, OK)}`` — a banner on run 1 that is GONE on run 2.

    The single-run-transient case: it looked login-expired once, but the
    second (decisive) read is clean, so the frozen check yields OK and the
    agent must NOT be restarted.
    """
    return {n: (STUCK, OK) for n in names}


class Recorder:
    """A real restart callable that records names instead of restarting.

    Not a mock: a plain object with the production ``(name) -> bool``
    signature. ``names`` is the evidence a test reads to prove a restart did
    — or, more importantly, did NOT — happen.
    """

    def __init__(self, *, ok: bool = True, boom: Exception | None = None) -> None:
        self.names: list[str] = []
        self._ok = ok
        self._boom = boom

    def __call__(self, name: str) -> bool:
        self.names.append(name)
        if self._boom is not None:
            raise self._boom
        return self._ok
