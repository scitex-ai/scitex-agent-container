"""The every-invocation warning. This is the whole point of the feature.

Operator, 2026-07-14: "Comments and READMEs are meaningless if nobody
reads them. If you're not on the latest version, that itself should emit
a warning. When I type ``sac`` on the host and a newer version is already
published, a warning should appear. THAT is how I learn I need to
install."

So the control lives where the attention already is — in front of the
command the operator types every day — and not in a document, a dashboard
or a tab.

THREE RULES THIS MODULE CANNOT BREAK
------------------------------------
1. **Never slow ``sac`` down.** No network, no subprocess, no heavy
   import. One small JSON read of a file the cron refresher already
   wrote. ``$SAC_FRESHNESS_QUIET`` is honoured before we even touch the
   disk.
2. **Never break ``sac``.** Every failure path in here ends in silence.
   A staleness warning that can crash the CLI is infinitely worse than
   the staleness it reports.
3. **Never cry wolf.** Only a cached finding that is positively STALE
   speaks. Missing cache, expired cache, corrupt cache, unparseable
   version, offline refresher -> UNKNOWN -> **say nothing at all**. A
   check that nags when it does not know gets silenced within a day, and
   a silenced check is worth less than no check, because everyone still
   believes it is watching.

Rules 2 and 3 are why this is a warning and not a hard failure by
default. ``$SAC_FRESHNESS_SEVERITY=error`` is the single knob that
tightens it, once the signal has earned that trust.
"""

from __future__ import annotations

import os
import sys

__all__ = ["SEVERITY_DEFAULT", "warn_if_stale"]

SEVERITY_DEFAULT = "warn"
_SEVERITIES = ("silent", "warn", "error")

_ENV_QUIET = "SAC_FRESHNESS_QUIET"
_ENV_SEVERITY = "SAC_FRESHNESS_SEVERITY"
_ENV_DEBUG = "SAC_FRESHNESS_DEBUG"

# Exit code used only when severity=error. Distinct from click's 1/2 so a
# staleness abort is never mistaken for a usage error or a command failure.
EXIT_STALE = 3

_BAR = "!" * 72


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def severity() -> str:
    """``silent`` | ``warn`` (default) | ``error``.

    An unrecognised value falls back to the default instead of raising —
    a typo in an env var must never be able to break the CLI.
    """
    raw = (os.environ.get(_ENV_SEVERITY) or "").strip().lower()
    return raw if raw in _SEVERITIES else SEVERITY_DEFAULT


def warning_lines(findings) -> list[str]:
    """The banner. Mirrors ``_drift.drift_warning_lines``' house style.

    Every stale finding gets its own ``why`` + ``fix`` pair, because an
    alarm that does not say what to DO is an alarm people learn to skip.
    """
    if not findings:
        return []
    lines = [_BAR, "sac-freshness WARNING: this sac is not what shipped."]
    for finding in findings:
        lines.append(f"  * {finding.summary}")
        if finding.remedy:
            lines.append(f"      fix: {finding.remedy}")
    lines.append(f"  (silence: {_ENV_QUIET}=1   details: sac freshness check)")
    lines.append(_BAR)
    return lines


def warn_if_stale(stream=None) -> int:
    """Emit the staleness banner to stderr. Returns an exit code.

    Returns ``0`` in every case except ``severity=error`` with a genuinely
    STALE cached report, which returns :data:`EXIT_STALE`. The caller
    decides whether to act on it; this function's own contract is that it
    NEVER raises, whatever it finds on disk.

    ``stream`` is resolved at call time (default ``sys.stderr``) so tests
    can capture it — the same seam ``_drift`` uses.
    """
    if _truthy(os.environ.get(_ENV_QUIET)):
        return 0
    level = severity()
    if level == "silent":
        return 0

    debug = _truthy(os.environ.get(_ENV_DEBUG))
    out = stream if stream is not None else sys.stderr

    try:
        from ._cache import read_cache

        report = read_cache()
        if report is None:
            # No current evidence. This is UNKNOWN, and UNKNOWN is silent.
            if debug:
                print(
                    "sac-freshness: no usable cache (missing/expired/corrupt) "
                    "-> UNKNOWN -> silent. Refresh: sac freshness refresh",
                    file=out,
                )
            return 0

        stale = report.stale
        if not stale:
            if debug:
                print(
                    f"sac-freshness: cached state={report.state.value} "
                    f"({len(report.findings)} checks) -> nothing to warn about",
                    file=out,
                )
            return 0

        for line in warning_lines(stale):
            print(line, file=out)
        return EXIT_STALE if level == "error" else 0

    except Exception as exc:  # stx-allow: fallback (reason: rule 2 -- a freshness warning must NEVER be able to break the sac CLI; any failure here degrades to silence)
        if debug:
            print(f"sac-freshness: check failed ({exc!r}) -> silent", file=out)
        return 0


# EOF
