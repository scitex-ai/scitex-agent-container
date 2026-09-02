"""The constitution-distribution beat: a merged rule is not a live rule
until this runs.

Its own group because it fits neither sibling. It is not an accounts job,
it is not a liveness enforcer, and it REPAIRS rather than reports — the
one property :mod:`._specs_maintenance` defines itself by not having. A
reader checking "what writes into agent overlays on a timer" should find
exactly one file, and this is it.

The payload is :mod:`._constitution_refresh`, the source-side port of the
hand-written ``~/.local/bin/sac-constitution-refresh.sh`` (operator ruling
2026-09-02: hand-written scripts must live on the source side). The unit
pair that scheduled the script by hand — ``sac-constitution-refresh
.service`` / ``.timer``, OnBootSec=3min, OnUnitActiveSec=10min — is retired
by the migration table when this declared job is armed; the cadence below
is that unit's, kept rather than re-decided.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.jobs import JobSpec

__all__ = ["constitution_jobs"]


def constitution_jobs(*, executable: str | None = None) -> "list[JobSpec]":
    """sac's constitution-refresh JobSpec.

    ``executable`` is the same test seam :func:`._sac_bin.sac_bin` exposes,
    threaded through so a test can resolve the payload against a venv-shaped
    tree it built on disk rather than against whatever the running
    environment happens to have installed.
    """
    from scitex_dev.jobs import JobSpec

    from ._sac_bin import sac_bin

    # ABSOLUTE, resolved per host -- see :mod:`._sac_bin` for the measurement.
    sac = sac_bin(executable=executable)

    return [
        JobSpec(
            name="scitex-agent-container-constitution-refresh",
            schedule="*/10 * * * *",  # every 10min (cron form; timer cadence below)
            # SELF-BOUNDING (120s), the hand-written unit's TimeoutStartSec.
            # A pass is ~1s of md5 comparisons over ~20 overlays and writes
            # only on a real difference; 120s covers a slow disk without
            # ever hanging forever.
            command=f"/usr/bin/timeout 120 {sac} agents refresh-constitution",
            description=(
                "Pushes the current constitution (~/.claude/commands/"
                "constitution.md) into every agent overlay's upper/home/agent/"
                ".claude/commands/ — the path a RUNNING agent reads — and reads "
                "each copy back through the overlay, so a rule merged to develop "
                "is live in every agent without a restart. INCIDENT 2026-08-15: "
                "the operator set the THREE DAYS RULE and it reached 2 of 21 "
                "agents; three versions were in circulation, the oldest nine "
                "days old and missing two policy reversals. Declared here rather "
                "than as ~/.local/bin/sac-constitution-refresh.sh per the "
                "operator ruling of 2026-09-02 that hand-written scripts must "
                "live on the source side. Exit 2 refuses a source under 10000B "
                "(a probable truncation) rather than fan it out; exit 1 when any "
                "overlay could not be refreshed; --dry-run writes nothing."
            ),
            kind="timer",
            # 10 minutes: the cost of a pass is ~1s of md5 comparisons and it
            # writes only on a real difference, so the cadence is bounded by
            # how long a merged RULE may stay invisible to the fleet — not by
            # the cost of checking. On 2026-08-15 that window was open long
            # enough for 19 agents to be non-compliant with a rule the
            # operator called 「really important」 within minutes of setting it.
            # 3min after boot: the overlays exist before any agent starts, so
            # there is nothing to wait for beyond the login settling window.
            on_boot_sec="3min",
            on_unit_active_sec="10min",
        ),
    ]
