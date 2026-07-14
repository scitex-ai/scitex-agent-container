"""The evidence seams: where the facts come from.

Every fact the checks reason about arrives through :class:`Sources`. Two
implementations:

* :class:`LiveSources` — the real thing. PyPI over HTTPS, ``git tag``,
  ``gh run list``, ``importlib.metadata``, ``systemctl show``.
* :class:`StaticSources` — the same interface, handed data captured from
  those real systems. This is what the tests drive: no mocks, no
  monkeypatching, no network — a real object with a real implementation,
  fed real recorded evidence (including the actual PyPI release list and
  the actual GitHub run conclusions from the 2026-07-13 incident).

**Every method returns ``None`` when it cannot get a real answer.** That
``None`` is the only way UNKNOWN reaches the verdict layer, and it is why
this alarm cannot become the next false-green: a source that cannot see
says so, instead of returning an empty list that reads like "nothing
wrong".

TIMEOUTS
--------
The fleet host runs at load ~60. Every timeout here is deliberately
generous (20-30 s, not 2-5 s), because a tight timeout on a loaded box
does not "fail fast" — it manufactures an UNKNOWN out of a machine that
was merely busy, and a check that goes blind under load is useless
exactly when the fleet is under load. None of this is on an interactive
path; the refresher runs from cron and nobody is waiting on it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Protocol

__all__ = ["DIST_NAME", "LiveSources", "Sources", "StaticSources"]

DIST_NAME = "scitex-agent-container"
PYPI_JSON_URL = f"https://pypi.org/pypi/{DIST_NAME}/json"
RELEASE_WORKFLOW = "pypi-publish-and-github-release-on-tag.yml"
LISTEN_UNIT = "sac-listen.service"

# See module docstring: generous on purpose, this host sits at load ~60.
_HTTP_TIMEOUT_S = 30
_CMD_TIMEOUT_S = 30


class Sources(Protocol):
    """The facts a freshness verdict needs. ``None`` always means UNKNOWN."""

    def pypi_versions(self) -> set[str] | None:
        """Every version PyPI has ever published. The only truth about
        what shipped — not the tag, not the GitHub release, not the
        changelog, all three of which lied during the 2026-07-13
        incident."""

    def pypi_latest(self) -> str | None:
        """PyPI's own idea of the newest release."""

    def installed_version(self) -> str | None:
        """What THIS interpreter has installed."""

    def installed_at(self) -> float | None:
        """When the installed package was last written (epoch seconds)."""

    def git_tags(self) -> list[str] | None:
        """Every ``v*`` release tag."""

    def release_runs(self) -> list[dict] | None:
        """Recent release-workflow runs, newest first."""

    def daemon_started_at(self) -> float | None:
        """When the long-lived listen daemon began executing."""


class LiveSources:
    """Real evidence, from the real systems."""

    def __init__(self, repo_root: Path | None = None, unit: str = LISTEN_UNIT):
        self._repo_root = repo_root
        self._unit = unit
        self._pypi: dict | None | object = _UNSET

    # -- PyPI ------------------------------------------------------------
    def _pypi_json(self) -> dict | None:
        if self._pypi is _UNSET:
            self._pypi = self._fetch_pypi()
        return self._pypi  # type: ignore[return-value]

    def _fetch_pypi(self) -> dict | None:
        # Imported here, not at module scope: this module is reachable
        # from the CLI's import graph and urllib costs ~15 ms we do not
        # want to pay on `sac --help`.
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(
                PYPI_JSON_URL, timeout=_HTTP_TIMEOUT_S
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):  # stx-allow: fallback (reason: offline/slow/garbled PyPI is UNKNOWN, never "fine")
            return None

    def pypi_versions(self) -> set[str] | None:
        data = self._pypi_json()
        if not data:
            return None
        releases = data.get("releases")
        if not isinstance(releases, dict):
            return None
        # A version with an empty file list was yanked/never uploaded; it
        # is NOT a shipped release, and counting it would let a ghost
        # masquerade as published.
        return {v for v, files in releases.items() if files}

    def pypi_latest(self) -> str | None:
        data = self._pypi_json()
        if not data:
            return None
        return (data.get("info") or {}).get("version")

    # -- this install ----------------------------------------------------
    def installed_version(self) -> str | None:
        """The version of the LOADED code, via ``_provenance`` (the SSOT).

        Not re-derived here: ``_provenance.identity()`` already resolves
        "which code is actually loaded, and what does it declare", and
        having two answers to that question is how this class of bug
        starts in the first place.
        """
        from .._provenance import identity

        version = identity().get("version") or ""
        # `_provenance` says "0.0.0+unknown" when nothing is installed;
        # that is an absence of evidence, not a version.
        return None if not version or version.startswith("0.0.0+") else version

    def installed_at(self) -> float | None:
        """mtime of the dist-info dir (when pip wrote it), else the package.

        dist-info is the better signal: pip stamps it at install time,
        whereas a package ``__init__.py`` can be touched by anything.
        """
        from importlib.metadata import PackageNotFoundError, distribution

        try:
            dist = distribution(DIST_NAME)
        except PackageNotFoundError:  # stx-allow: fallback (reason: running off a source tree with nothing installed -- no install time exists)
            dist = None
        if dist is not None:
            base = getattr(dist, "_path", None)
            if isinstance(base, Path) and base.exists():
                return base.stat().st_mtime
        from .._provenance import package_dir

        init = package_dir() / "__init__.py"
        try:
            return init.stat().st_mtime
        except OSError:  # stx-allow: fallback (reason: no readable package file -> UNKNOWN)
            return None

    # -- the repo --------------------------------------------------------
    def repo_root(self) -> Path | None:
        if self._repo_root is not None:
            return self._repo_root
        from .._provenance import package_dir
        from .._provenance._git import repo_root_for_package

        return repo_root_for_package(package_dir())

    def git_tags(self) -> list[str] | None:
        """Release tags from the checkout. ``None`` when there is no repo.

        A wheel-only host has no tags to read, and that is UNKNOWN, not
        "no ghosts". Run the refresher where a checkout exists (the dev
        host, CI) to get this check.
        """
        root = self.repo_root()
        if root is None:
            return None
        out = _run(["git", "-C", str(root), "tag", "-l", "v*"])
        if out is None:
            return None
        return [line.strip() for line in out.splitlines() if line.strip()]

    def release_runs(self) -> list[dict] | None:
        """Release-workflow runs via ``gh``. ``None`` when gh is absent."""
        out = _run(
            [
                "gh", "run", "list",
                "--workflow", RELEASE_WORKFLOW,
                "--limit", "10",
                "--json", "conclusion,status,headBranch,createdAt,url",
            ],
            cwd=self.repo_root(),
        )
        if out is None:
            return None
        try:
            runs = json.loads(out)
        except ValueError:  # stx-allow: fallback (reason: unparseable gh output is UNKNOWN)
            return None
        return runs if isinstance(runs, list) else None

    # -- the running daemon ----------------------------------------------
    def daemon_started_at(self) -> float | None:
        """``ExecMainStartTimestamp`` of the listen unit, as epoch seconds.

        systemd reports this in microseconds since the epoch, which is
        exact and needs no date parsing. ``0`` means the unit exists but
        has never run -> nothing is running -> UNKNOWN, not stale.
        """
        out = _run(
            [
                "systemctl", "--user", "show", self._unit,
                "-p", "ExecMainStartTimestampMonotonic",
                "-p", "ExecMainStartTimestamp",
                "--value",
            ]
        )
        if out is None:
            return None
        return _parse_systemd_timestamp(out)


class StaticSources:
    """The same interface, fed real recorded evidence. For tests.

    Not a mock: it is a genuine implementation of :class:`Sources` whose
    backing store happens to be a dict instead of a network. Tests hand
    it the actual bytes the real systems returned, so the verdict logic
    is exercised against reality without ever touching reality.
    """

    def __init__(
        self,
        *,
        pypi_versions=None,
        pypi_latest=None,
        installed_version=None,
        installed_at=None,
        git_tags=None,
        release_runs=None,
        daemon_started_at=None,
    ):
        self._pypi_versions = pypi_versions
        self._pypi_latest = pypi_latest
        self._installed_version = installed_version
        self._installed_at = installed_at
        self._git_tags = git_tags
        self._release_runs = release_runs
        self._daemon_started_at = daemon_started_at

    def pypi_versions(self):
        return set(self._pypi_versions) if self._pypi_versions is not None else None

    def pypi_latest(self):
        return self._pypi_latest

    def installed_version(self):
        return self._installed_version

    def installed_at(self):
        return self._installed_at

    def git_tags(self):
        return list(self._git_tags) if self._git_tags is not None else None

    def release_runs(self):
        return list(self._release_runs) if self._release_runs is not None else None

    def daemon_started_at(self):
        return self._daemon_started_at


class _Unset:
    pass


_UNSET = _Unset()


def _run(argv: list[str], cwd: Path | None = None) -> str | None:
    """Run a command; ``None`` on any failure. Never raises, never hangs."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_CMD_TIMEOUT_S,
            cwd=str(cwd) if cwd else None,
        )
    except (OSError, subprocess.SubprocessError):  # stx-allow: fallback (reason: missing binary / timeout is UNKNOWN, never "fine")
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _parse_systemd_timestamp(raw: str) -> float | None:
    """First line = monotonic usec, second = human stamp. Prefer neither
    if the unit never started."""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return None
    # `--value` with two -p flags prints them in the order asked:
    # monotonic first. 0 => never started.
    try:
        monotonic_usec = int(lines[0])
    except ValueError:
        return None
    if monotonic_usec <= 0:
        return None
    # Convert monotonic -> wall clock via /proc/uptime, which is the only
    # translation available without pulling in a date parser.
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            uptime_s = float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):  # stx-allow: fallback (reason: no /proc/uptime -> cannot translate -> UNKNOWN)
        return None
    import time as _time

    boot_epoch = _time.time() - uptime_s
    return boot_epoch + (monotonic_usec / 1_000_000.0)


# EOF
