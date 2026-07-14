"""The holder's health LEDGER — a failed check is never erased by a lucky reply.

The defect this closes (operator incident 2026-07-14)
=====================================================
The standby loop counted ``consecutive_unhealthy`` and reset it to ``0``
on ANY single successful probe::

    if _probe_health(...):
        consecutive_unhealthy = 0          # <-- the whole record, gone
        _log("standing by behind healthy holder PID …")

A holder that FLAPS — answers one probe, misses the next — therefore
never accumulated the CONSECUTIVE failures the take-over threshold
demands. It oscillated ``1/2`` → "healthy" → ``1/2`` → "healthy" and
``sac listen`` stood by behind it FOREVER::

    # sac listen: holder PID 738982 on 127.0.0.1:7878 not answering
      health (1/2) — re-checking in 4.0s before taking over
    # sac listen: standing by behind healthy holder PID 738982 …
    # sac listen: standing by behind healthy holder PID 738982 …
    ... (forever)

That is not a hypothetical: the live 7878 daemon on this box was
measured answering ``HTTP 200`` one minute and ``Connection refused``
the next. Flapping is the ACTUAL field behaviour, and the reset made it
invisible.

The rule
========
**A failure is a fact. One later success does not un-happen it.**

* Any non-SERVING observation adds to ``failures``.
* A SERVING observation does NOT wipe ``failures``. It only builds a
  ``serving_streak``; the suspicion is cleared only once the holder has
  answered ``recovery_streak`` times IN A ROW — and that clearing is
  logged LOUD, because "the thing I said was broken now looks fine" is
  exactly the transition an operator must never have hidden from them.
* ``failures >= threshold`` ⇒ the verdict is corroborated ⇒ act.

A genuinely healthy holder that blips once is therefore NOT destroyed
(it answers twice more and is cleared), while a holder that keeps
failing is ALWAYS eventually acted on — which is precisely the
distinction the old counter could not draw.
"""

from __future__ import annotations

from ._holder_health import HolderHealth, HolderProbe

__all__ = [
    "HolderLedger",
    "ListenTakeoverFailed",
    "takeover_failure_message",
]


class ListenTakeoverFailed(RuntimeError):
    """The holder failed its health checks and the port could NOT be freed.

    Deliberately NOT a subclass of ``ListenAlreadyRunningError``: plain
    contention is NORMAL (stand by behind a serving primary), whereas an
    unfreeable non-serving holder is an OUTAGE that no amount of further
    waiting can fix. The CLI maps this to a non-zero exit carrying the
    operator-actionable message built by :func:`takeover_failure_message`.
    """


class HolderLedger:
    """Corroboration ledger for one holder's health observations.

    ``threshold`` consecutive-equivalent failures are required before the
    holder is declared wedged (so a daemon that merely has not finished
    binding yet is never taken over), but — unlike the counter it
    replaces — a single lucky reply does NOT erase the record.
    """

    __slots__ = ("_threshold", "_recovery_streak", "failures", "serving_streak")

    def __init__(self, *, threshold: int, recovery_streak: int | None = None) -> None:
        self._threshold = max(1, threshold)
        self._recovery_streak = max(1, recovery_streak or self._threshold)
        self.failures = 0
        self.serving_streak = 0

    @property
    def threshold(self) -> int:
        return self._threshold

    def record(self, probe: HolderProbe) -> bool:
        """Record one observation. Returns ``True`` iff it CLEARED a suspicion.

        A ``True`` return is the "holder recovered" transition and must be
        logged loudly — it is the moment we stop distrusting a holder we
        previously reported as failing.
        """
        if probe.health is HolderHealth.SERVING:
            if self.failures == 0:
                return False  # a clean holder; nothing to clear
            self.serving_streak += 1
            if self.serving_streak >= self._recovery_streak:
                self.failures = 0
                self.serving_streak = 0
                return True
            return False
        # NOT_SERVING or UNREACHABLE — both are failures. A holder that
        # answers nothing is NOT healthy; absence of evidence is not
        # evidence of health.
        self.serving_streak = 0
        self.failures += 1
        return False

    @property
    def suspect(self) -> bool:
        """True while at least one failed check stands un-cleared."""
        return self.failures > 0

    @property
    def corroborated(self) -> bool:
        """True once enough failures have accrued to act on the verdict."""
        return self.failures >= self._threshold

    def reset(self) -> None:
        """Forget the history (used after a take-over changes the world)."""
        self.failures = 0
        self.serving_streak = 0


def takeover_failure_message(
    *,
    host: str,
    port: int,
    holder_pid: int | None,
    probe: HolderProbe,
    failures: int,
    attempts: int,
    error: str,
) -> str:
    """Build the LOUD, actionable message for an unfreeable wedged holder.

    Names (a) what was OBSERVED, (b) which PID, (c) why we did not force
    it, and (d) the exact command a human can run. Never a bare "did not
    respond" — the operator sat watching an unbounded loop precisely
    because the daemon never told him anything he could act on.
    """
    pid_str = str(holder_pid) if holder_pid is not None else "<unknown>"
    return (
        f"ERROR: sac listen cannot take over {host}:{port}.\n"
        f"  The flock holder PID {pid_str} FAILED its health check "
        f"{failures}x ({probe.describe()}), so it is NOT serving — but it "
        f"also did not exit on SIGTERM after {attempts} take-over "
        f"attempt(s): {error}\n"
        f"  Refusing to SIGKILL it automatically: a probe-based 'wedged' "
        f"verdict can be wrong, and force-killing a healthy control plane "
        f"would cut the whole fleet off from this host.\n"
        f"  Refusing to stand by behind it either: it is not serving, so "
        f"waiting silently would hide the outage indefinitely.\n"
        f"  To force the take-over, run:  sac listen restart --force\n"
        f"  To inspect the holder first:  sac listen status  /  "
        f"lsof -nP -iTCP:{port} -sTCP:LISTEN"
    )
