"""The four conditions, plus the symbol probe. Pure verdicts, no I/O.

Each of the four fails in a DIFFERENT way, which is why each is a
separate check and none of them subsumes another:

1. ``ghost-tag``           — a tag exists, PyPI has no such release. The
   release *looks* done (the tag is right there) and is not. This is the
   one that ran for a full day unnoticed.
2. ``host-behind-pypi``    — what shipped is newer than what is installed.
3. ``running-vs-installed``— what is installed is newer than what the live
   daemon is executing. Installed is NOT running: Python does not reload
   a module in a live process, so an upgrade changes nothing at all until
   a restart.
4. ``release-run``         — the release workflow ended in failure and sat
   red in a tab nobody opens.

A check for any one of these is blind to the other three. On
2026-07-13 all four were true at once and the fleet saw none of them.
"""

from __future__ import annotations

import time

from . import _version
from ._model import Finding, Freshness, FreshnessReport
from ._symbols import EXPECTATIONS, probe

__all__ = [
    "build_report",
    "check_ghost_tags",
    "check_host_behind_pypi",
    "check_release_runs",
    "check_running_vs_installed",
    "check_symbols",
]

# A run that has not finished yet says nothing about whether it will ship.
_TERMINAL = "completed"


def _unknown(check: str, why: str) -> Finding:
    """UNKNOWN carries its reason. 'I don't know' is only useful with a
    'because'."""
    return Finding(check=check, state=Freshness.UNKNOWN, summary=why)


def check_host_behind_pypi(installed, latest, *, python=None) -> Finding:
    """Is the installed package older than the newest PyPI release?"""
    check = "host-behind-pypi"
    if not installed:
        return _unknown(check, "installed version unknown (package not installed?)")
    if not latest:
        return _unknown(check, "PyPI unreachable — cannot tell what shipped")

    behind = _version.is_behind(installed, latest)
    if behind is None:
        return _unknown(
            check, f"cannot order versions {installed!r} vs {latest!r}"
        )
    if not behind:
        return Finding(
            check=check,
            state=Freshness.FRESH,
            summary=f"installed {installed} is current with PyPI ({latest})",
            data={"installed": installed, "pypi_latest": latest},
        )

    exe = python or "python"
    return Finding(
        check=check,
        state=Freshness.STALE,
        summary=f"installed {installed} is BEHIND PyPI {latest}",
        remedy=f"{exe} -m pip install -U '{_DIST}=={latest}'",
        detail=(
            "The fixes released between these two versions are NOT running "
            "on this machine. Upgrading is not enough on its own — any "
            "long-lived process (sac listen, MCP servers) also has to be "
            "restarted, because Python does not reload modules in a live "
            "process."
        ),
        data={"installed": installed, "pypi_latest": latest},
    )


def check_ghost_tags(tags, released) -> Finding:
    """A tag with no PyPI release: ``git tag`` succeeded, shipping did not.

    The verdict keys on the **head** tag — the newest one — and this is
    the whole design:

    * head tag not on PyPI => STALE. The last thing we tried to ship did
      not ship. This is the live, actionable failure, and it is exactly
      the state the repo was in from 23:24 on 2026-07-13 (head v0.21.16,
      PyPI 0.21.14) until v0.21.17 published a day later.
    * head tag published, older ghosts behind it => FRESH, but every
      ghost is still NAMED in the summary and in ``data``. They are real
      (v0.21.15 and v0.21.16 never shipped), they stay visible in ``sac
      freshness check`` -- and they raise no alarm, because a superseded
      ghost has no remedy and an alarm with no remedy is noise that gets
      the whole check switched off.

    That split is also self-cleaning: a deliberately-abandoned tag stops
    being the head as soon as a later version ships, and goes quiet on
    its own. No allowlist to maintain, and no way to abandon the HEAD tag
    without either shipping something newer or deleting the tag.
    """
    check = "ghost-tag"
    if tags is None:
        return _unknown(check, "no git checkout — cannot read release tags")
    if released is None:
        return _unknown(check, "PyPI unreachable — cannot tell what shipped")
    if not tags:
        return _unknown(check, "no release tags found")

    published = {k for k in (_version.parse(v) for v in released) if k is not None}
    ordered = sorted(
        ((_version.parse(t), t) for t in tags if _version.parse(t) is not None),
        key=lambda pair: pair[0],
    )
    if not ordered:
        return _unknown(check, "no parseable release tags")

    ghosts = [tag for key, tag in ordered if key not in published]
    head_key, head_tag = ordered[-1]
    head_is_ghost = head_key not in published

    data = {
        "ghosts": ghosts,
        "head_tag": head_tag,
        "head_is_ghost": head_is_ghost,
        "pypi_latest_published": _version.latest(released),
    }

    if head_is_ghost:
        return Finding(
            check=check,
            state=Freshness.STALE,
            summary=(
                f"GHOST TAG: {head_tag} is tagged but NEVER reached PyPI "
                f"(newest published: {_version.latest(released) or 'none'})"
            ),
            remedy=(
                "gh run list --workflow pypi-publish-and-github-release-on-tag.yml"
                "   # then re-run the failed release, or re-tag"
            ),
            detail=(
                "The tag exists, so the release LOOKS done. It is not: "
                "nothing was published. Anyone who assumes this fix is live "
                "is wrong, and will re-diagnose an already-fixed bug."
            ),
            data=data,
        )

    if ghosts:
        return Finding(
            check=check,
            state=Freshness.FRESH,
            summary=(
                f"head tag {head_tag} published OK; "
                f"{len(ghosts)} older tag(s) never shipped and were "
                f"superseded: {' '.join(ghosts)}"
            ),
            detail=(
                "Superseded ghosts: each of these tags exists in git but has "
                "no PyPI release. A later version did ship, so there is "
                "nothing to fix and no alarm is raised — but they are why "
                "the tag list cannot be trusted as a record of what shipped. "
                "Delete them if you want the history to stop lying."
            ),
            data=data,
        )

    return Finding(
        check=check,
        state=Freshness.FRESH,
        summary=f"every release tag reached PyPI (head: {head_tag})",
        data=data,
    )


def check_running_vs_installed(daemon_started_at, installed_at, *, unit) -> Finding:
    """Is a live daemon still executing code from before the last upgrade?

    Installed is not running. ``pip install -U`` rewrites files on disk;
    it does not — cannot — reach into a running process and swap the
    modules it imported at boot. Until the daemon restarts, the upgrade
    has changed precisely nothing about what is executing.
    """
    check = "running-vs-installed"
    if daemon_started_at is None:
        return _unknown(check, f"{unit} is not running (or not under systemd)")
    if installed_at is None:
        return _unknown(check, "cannot determine when the package was installed")

    if installed_at <= daemon_started_at:
        return Finding(
            check=check,
            state=Freshness.FRESH,
            summary=f"{unit} started after the last install — running current code",
            data={"daemon_started_at": daemon_started_at, "installed_at": installed_at},
        )

    lag_h = (installed_at - daemon_started_at) / 3600.0
    return Finding(
        check=check,
        state=Freshness.STALE,
        summary=(
            f"{unit} is RUNNING PRE-UPGRADE CODE — it started "
            f"{lag_h:.1f}h before the package was last installed"
        ),
        remedy=f"systemctl --user restart {unit}",
        detail=(
            "The package on disk was upgraded while this daemon was already "
            "running. Python does not reload modules in a live process, so "
            "the daemon is still executing the OLD code and will keep doing "
            "so until it is restarted. `sac --version` will report the NEW "
            "version the whole time, which is why the version string cannot "
            "be trusted to answer this."
        ),
        data={
            "daemon_started_at": daemon_started_at,
            "installed_at": installed_at,
            "lag_hours": lag_h,
        },
    )


def check_release_runs(runs) -> Finding:
    """Did the most recent finished release run actually succeed?

    Anything that is not ``success`` — ``failure``, ``cancelled``,
    ``timed_out`` — shipped nothing. v0.21.16's run FAILED and v0.21.15's
    was CANCELLED; both left a tag behind and neither published, so
    treating "not success" as one class is not pedantry, it is the two
    real cases.
    """
    check = "release-run"
    if runs is None:
        return _unknown(check, "gh unavailable — cannot read release runs")

    finished = [r for r in runs if (r or {}).get("status") == _TERMINAL]
    if not finished:
        return _unknown(check, "no completed release runs found")

    last = finished[0]
    conclusion = last.get("conclusion")
    ref = last.get("headBranch") or "?"
    if conclusion == "success":
        return Finding(
            check=check,
            state=Freshness.FRESH,
            summary=f"last release run ({ref}) succeeded",
            data={"conclusion": conclusion, "ref": ref, "url": last.get("url")},
        )

    return Finding(
        check=check,
        state=Freshness.STALE,
        summary=f"last release run ({ref}) ended in {conclusion!r} — NOTHING SHIPPED",
        remedy=f"gh run view {last.get('url') or ''}".strip(),
        detail=(
            "The release pipeline is test -> build -> publish -> release. A "
            "non-success conclusion means build/publish never ran, so the tag "
            "exists with no PyPI release behind it. Red in a tab nobody opens "
            "is how this went unnoticed for a day."
        ),
        data={"conclusion": conclusion, "ref": ref, "url": last.get("url")},
    )


def check_symbols(expectations=EXPECTATIONS, prober=probe) -> Finding:
    """Are the fixes we KNOW shipped actually present in the loaded code?

    This is the check that cannot be fooled by a number. Each expectation
    names a symbol that exists only in fixed code; ``hasattr`` is asked of
    the module object in the running interpreter. See ``_symbols`` for why
    every version-string-based answer here is worthless.
    """
    check = "symbol-probe"
    if not expectations:
        return _unknown(check, "no symbol expectations registered")

    missing, present, unknown = [], [], []
    for exp in expectations:
        result = prober(exp)
        if result is True:
            present.append(exp)
        elif result is False:
            missing.append(exp)
        else:
            unknown.append(exp)

    if missing:
        first = missing[0]
        return Finding(
            check=check,
            state=Freshness.STALE,
            summary=(
                f"{len(missing)} known fix(es) MISSING from the loaded code: "
                + ", ".join(e.dotted for e in missing)
            ),
            remedy=f"pip install -U {_DIST}   # then restart long-lived processes",
            detail=(
                "Probed by symbol, not by version string. "
                + first.why
                + f"  (expected since {first.since})"
            ),
            data={
                "missing": [e.dotted for e in missing],
                "present": [e.dotted for e in present],
                "unknown": [e.dotted for e in unknown],
            },
        )

    if not present:
        return _unknown(check, "no symbol could be probed")

    return Finding(
        check=check,
        state=Freshness.FRESH,
        summary=f"all {len(present)} probed fix(es) present in the loaded code",
        data={
            "present": [e.dotted for e in present],
            "unknown": [e.dotted for e in unknown],
        },
    )


_DIST = "scitex-agent-container"


def build_report(sources, *, unit: str, python: str | None = None, now=None):
    """Run every check against ``sources`` and assemble the report."""
    latest = sources.pypi_latest()
    released = sources.pypi_versions()
    findings = (
        check_host_behind_pypi(sources.installed_version(), latest, python=python),
        check_ghost_tags(sources.git_tags(), released),
        check_running_vs_installed(
            sources.daemon_started_at(), sources.installed_at(), unit=unit
        ),
        check_release_runs(sources.release_runs()),
        check_symbols(),
    )
    return FreshnessReport(
        findings=findings,
        generated_at=time.time() if now is None else now,
    )


# EOF
