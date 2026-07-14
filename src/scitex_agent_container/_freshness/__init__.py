"""Deploy freshness: is the code we are running the code that shipped?

Born from the 2026-07-13/14 comms outage. Seven PRs fixed a `sac listen`
wedge; every one merged; the host ran 0.21.14 the entire time, which
predates all of them. Agents hand-drained the wedged pool and re-diagnosed
the same already-fixed bug for a day, because three consecutive tags never
reached PyPI and NOTHING anywhere said a word.

    "We are not failing to FIX these bugs. We are failing to SHIP them."

The drift is structural, not bad luck. A release is
``tag -> test -> build -> publish``. The tag is a local action that always
succeeds; everything downstream is asynchronous and can fail silently. No
consumer ever compares what it is running against what actually shipped,
so drift is monotonic — it can only accumulate, never self-correct. (Six
of the eighteen v0.21.x tags never published. The operator knew about two.)

This package closes the loop: the consumer checks itself, and speaks up.

* :mod:`._checks`  — the four failure modes, which are genuinely different
  and do not catch each other: ghost tag / host behind PyPI / running !=
  installed / release run failed.
* :mod:`._symbols` — probes a fix by the SYMBOL it introduced, never by a
  version string (which lies in both directions here).
* :mod:`._warn`    — the every-invocation stderr warning. The operator's
  primary ask: typing ``sac`` is how you find out.
* :mod:`._cache`   — cron writes, CLI reads. The CLI never does I/O beyond
  one small JSON file.

Three states, and the middle one is the reason this works:
**FRESH / STALE / UNKNOWN.** Only STALE ever speaks. UNKNOWN is silent —
never "fine", and never a remedy either, because a false RED gets acted on
and the action destroys a healthy thing.

Imports here are LAZY (PEP 562). ``_sources`` pulls in urllib and
subprocess, and this package sits in the CLI's import graph — an eager
import would put ~15 ms of urllib on every ``sac --help``, against a
~150 ms budget for the entire CLI.
"""

from __future__ import annotations

from ._model import Finding, Freshness, FreshnessReport

__all__ = [
    "EXPECTATIONS",
    "Finding",
    "Freshness",
    "FreshnessReport",
    "LiveSources",
    "StaticSources",
    "SymbolExpectation",
    "build_report",
    "cache_path",
    "check_ghost_tags",
    "check_host_behind_pypi",
    "check_release_runs",
    "check_running_vs_installed",
    "check_symbols",
    "probe",
    "read_cache",
    "warn_if_stale",
    "write_cache",
]

# name -> submodule holding it. Resolved on first attribute access so the
# CLI never pays for urllib/subprocess just by importing the package.
_LAZY = {
    "EXPECTATIONS": "._symbols",
    "SymbolExpectation": "._symbols",
    "probe": "._symbols",
    "LiveSources": "._sources",
    "StaticSources": "._sources",
    "build_report": "._checks",
    "check_ghost_tags": "._checks",
    "check_host_behind_pypi": "._checks",
    "check_release_runs": "._checks",
    "check_running_vs_installed": "._checks",
    "check_symbols": "._checks",
    "cache_path": "._cache",
    "read_cache": "._cache",
    "write_cache": "._cache",
    "warn_if_stale": "._warn",
}


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module, __name__), name)


def __dir__():
    return sorted(__all__)


# EOF
