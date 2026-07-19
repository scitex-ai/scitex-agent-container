#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_freshness/__init__.py

"""sac's consumption of ``scitex_dev.versioning`` — the seam, not the logic.

WHAT THIS PACKAGE IS. A thin adapter. sac supplies the facts that are its own
(``_config``) and the symbol registry only it can know (``_expectations``);
every verdict — what is stale, which check is safe against which kind of
install, what remedy may be printed — comes from the primitive. Per the
operator's ruling, dev holds the primitive and the leaves consume it. There
is deliberately no second implementation of that judgment here, because two
implementations of "is this current?" is how you get two answers.

WHY EVERYTHING IS OPTIONAL AND LAZY
-----------------------------------
``scitex-dev`` is a ``[dev]`` extra, NOT a runtime dependency of sac, and it
must stay that way — a heavyweight developer toolkit has no business being
mandatory for an agent container to boot. So:

* every import of it is inside a function, never at module scope;
* its absence is UNKNOWN, never FRESH and never an error;
* nothing on the CLI hot path pays for it.

That degradation is not a compromise made for packaging convenience — it is
the same tri-state rule the primitive is built on, applied one level up. A
missing checker is an absence of evidence about currency. Reporting FRESH
there would be the exact false-all-clear the primitive exists to prevent, and
reporting STALE would be a false RED whose remedy damages a healthy install.

TRI-STATE AT THIS BOUNDARY
--------------------------
:func:`check_currency` returns ``None`` when it could not obtain a verdict at
all (primitive absent, or it raised). ``None`` means UNKNOWN. Callers must
not collapse it into "fine" — :func:`is_stale` exists so they do not have to
write that condition themselves and get it subtly wrong. Note also that an
*empty* report is UNKNOWN upstream, so "no findings" never reads as healthy
on either side of this seam.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from ._config import (
    CACHE_SUBPATH,
    DIST_NAME,
    LISTEN_UNIT,
    MODULE_NAME,
    PYPI_JSON_URL,
    RELEASE_WORKFLOW,
    sac_versioning_config,
)
from ._expectations import EXPECTATIONS

#: The env namespace the primitive derives from ``scitex-agent-container``.
#: Restated here (and pinned by a test against the real config) so the
#: hot-path gate below can read the knobs without importing scitex-dev.
_ENV_PREFIX = "SCITEX_AGENT_CONTAINER_FRESHNESS"

#: Mirrors the primitive's own default: 24 h against an hourly refresher, so
#: 24 consecutive misses are tolerated before the banner falls silent.
_DEFAULT_TTL_S = 24 * 60 * 60

if TYPE_CHECKING:  # pragma: no cover - kept off the CLI import path
    from scitex_dev.versioning import Report

__all__ = [
    "CACHE_SUBPATH",
    "DIST_NAME",
    "EXPECTATIONS",
    "LISTEN_UNIT",
    "MODULE_NAME",
    "PYPI_JSON_URL",
    "RELEASE_WORKFLOW",
    "available",
    "check_currency",
    "is_stale",
    "read_cached",
    "refresh_cache",
    "running_version",
    "sac_versioning_config",
    "stale_findings",
    "warn_once",
]


def available() -> bool:
    """Is the primitive importable in THIS interpreter?

    Reported rather than assumed, so a caller can render an honest "cannot
    tell" instead of a confident wrong answer.
    """
    from importlib.util import find_spec

    try:
        return find_spec("scitex_dev.versioning") is not None
    except (
        ImportError,
        ValueError,
    ):  # stx-allow: fallback (reason: a broken/partial scitex-dev install is UNKNOWN, not a crash)
        return False


def check_currency(sources: Any = None) -> "Report | None":
    """Run the full currency check for sac. ``None`` means UNKNOWN.

    ``sources`` is passed straight through to the primitive, so a caller
    (or a test) can drive the real verdict logic from recorded evidence via
    ``scitex_dev.versioning.StaticSources`` instead of the network.

    Never raises. A primitive that is absent, half-installed, or itself
    broken yields ``None`` — the honest UNKNOWN — because a currency check
    that can take down the CLI is worse than the staleness it reports.
    """
    try:
        from scitex_dev.versioning import check_currency as _check

        return _check(sac_versioning_config(), sources)
    except Exception:  # noqa: BLE001 - stx-allow: fallback (reason: any failure to obtain a verdict IS UNKNOWN; see module docstring)
        return None


def stale_findings(report: "Report | None") -> tuple:
    """The actionable findings, or ``()`` for UNKNOWN.

    Only positively-STALE findings are ever returned. UNKNOWN contributes
    nothing to speak about, which is what keeps this from crying wolf.
    """
    if report is None:
        return ()
    return tuple(report.stale)


def is_stale(report: "Report | None") -> bool:
    """True ONLY on positive evidence of staleness.

    Deliberately not ``report.state is not FRESH``: that would fold UNKNOWN
    into the alarm and produce a warning whose remedy nobody should run.
    """
    return bool(stale_findings(report))


def running_version() -> tuple[str | None, str]:
    """``(version, source)`` for the code actually executing here.

    This is the answer ``sac --version`` needs and ``importlib.metadata``
    cannot give. For an editable install the metadata is a fossil frozen at
    ``pip install -e`` time — it does not move when you ``git pull`` — so the
    primitive reads the SOURCE's own declared version instead. For a wheel
    the metadata ships beside the code and is honest, so it is used as-is.

    ``source`` is one of:

    * ``"content"``  — content-verified by the primitive. Trustworthy.
    * ``"metadata"`` — the raw ``importlib.metadata`` claim, used because the
      primitive was unavailable. May be a fossil; callers SHOULD label it.
    * ``"unknown"``  — could not be established at all.

    The source is returned rather than hidden because a version whose
    provenance is unstated is how this problem persisted for a week.
    """
    try:
        from scitex_dev.versioning import LiveSources

        effective = LiveSources(sac_versioning_config()).effective_version()
        if effective:
            return effective, "content"
    except Exception:  # noqa: BLE001 - stx-allow: fallback (reason: fall through to the labelled metadata answer below)
        pass

    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _dist_version

    try:
        return _dist_version(DIST_NAME), "metadata"
    except (
        PackageNotFoundError
    ):  # stx-allow: fallback (reason: running off a source tree with nothing installed)
        return None, "unknown"


def read_cached() -> "Report | None":
    """The last report the refresher wrote, or ``None`` (UNKNOWN).

    ``None`` covers all of: missing, unreadable, malformed, and EXPIRED. An
    old cache means the refresher died, and a dead refresher's last answer
    is a fossil rather than evidence about now.
    """
    try:
        from scitex_dev.versioning import read_cache

        return read_cache(sac_versioning_config())
    except Exception:  # noqa: BLE001 - stx-allow: fallback (reason: an unreadable cache is UNKNOWN, never an error)
        return None


def refresh_cache(sources: Any = None) -> "Report | None":
    """Run the checks and publish the result. The cron deployer's payload.

    Split from the read path on purpose: this one pays the network cost
    (PyPI, ``gh``, ``git``) off the interactive path, so the CLI never does.
    Returns the report it wrote, or ``None`` if it could not produce one.
    """
    report = check_currency(sources)
    if report is None:
        return None
    try:
        from scitex_dev.versioning import write_cache

        write_cache(sac_versioning_config(), report)
    except Exception:  # noqa: BLE001 - stx-allow: fallback (reason: an unwritable cache must not fail the refresh run)
        return report
    return report


def _cache_file() -> "Path | None":
    """Where the refresher writes, resolved with stdlib only.

    Deliberately duplicates the primitive's ``cache_path`` arithmetic rather
    than calling it, because calling it costs a 201 ms import (measured) and
    this runs before every single ``sac`` command. No judgment is duplicated
    — this resolves a PATH, it does not decide anything. Everything is
    resolved at call time: ``$HOME`` is ``/home/agent`` in a container and
    ``/home/ywatanabe`` on the host.
    """
    from pathlib import Path

    override = os.environ.get(f"{_ENV_PREFIX}_CACHE")
    if override:
        return Path(override)
    root = os.environ.get("SCITEX_DIR")
    base = Path(root) if root else Path.home() / ".scitex"
    return base.joinpath(*CACHE_SUBPATH)


def _has_stale_cached() -> bool:
    """Cheap pre-gate: is there a positively-STALE finding worth speaking about?

    Pure stdlib, one small file read, ~1 ms. Returns False for every one of:
    no file, unreadable, malformed, no timestamp, expired, or nothing stale
    — i.e. UNKNOWN and FRESH both fall through silently, exactly as they must.
    """
    import json
    import time

    path = _cache_file()
    if path is None:
        return False
    try:
        # stx-allow: STX-IO006 (reason: stdlib json is REQUIRED here, not a
        # shortcut. This runs before every `sac` command; importing scitex-io
        # for provenance would reintroduce exactly the heavy-import cost this
        # gate exists to avoid, and there is no provenance to track — the
        # file is a machine-written cache, not a research artifact.)
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (
        OSError,
        ValueError,
    ):  # stx-allow: fallback (reason: an unreadable cache is UNKNOWN -> silence)
        return False
    if not isinstance(raw, dict):
        return False

    generated_at = raw.get("generated_at")
    if not isinstance(generated_at, (int, float)) or not generated_at:
        return False

    ttl_raw = os.environ.get(f"{_ENV_PREFIX}_TTL_S")
    try:
        ttl = int(ttl_raw) if ttl_raw else _DEFAULT_TTL_S
    except (
        ValueError
    ):  # stx-allow: fallback (reason: a typo in an env var must not break the CLI)
        ttl = _DEFAULT_TTL_S
    if ttl <= 0:
        ttl = _DEFAULT_TTL_S
    # A dead refresher's last answer is a fossil, not evidence about now.
    if time.time() - generated_at > ttl:
        return False

    findings = raw.get("findings")
    if not isinstance(findings, list):
        return False
    return any(isinstance(f, dict) and f.get("state") == "stale" for f in findings)


def warn_once(stream: Any = None) -> int:
    """Emit the staleness banner at most once per process tree. Never raises.

    Runs on EVERY ``sac`` invocation, which makes cost a correctness concern
    rather than a nicety: importing ``scitex_dev.versioning`` costs 201 ms
    (measured), against a documented ~150 ms budget for sac's entire click +
    LazyGroup startup. Paying that on every command — including tab
    completion — is how a staleness check earns itself an env var that turns
    it off, and then there is no check at all.

    So the expensive path is gated behind a ~1 ms stdlib read of the cache
    the refresher already wrote. Nothing heavy is imported unless there is
    positively-STALE news to deliver, which is the rare case. When there IS,
    the primitive does the rendering — sac never composes a remedy of its
    own, because composing one is how an editable checkout gets handed a
    ``pip install -U``.

    Returns an exit code — non-zero only under
    ``SCITEX_AGENT_CONTAINER_FRESHNESS_SEVERITY=error``.
    """
    try:
        if os.environ.get(f"_{_ENV_PREFIX}_EMITTED") == "1":
            return 0
        if (os.environ.get(f"{_ENV_PREFIX}_QUIET") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            return 0
        if not _has_stale_cached():
            return 0

        from scitex_dev.versioning import emit_once

        return emit_once(sac_versioning_config(), stream=stream)
    except Exception:  # noqa: BLE001 - stx-allow: fallback (reason: rule 2 — a staleness warning must NEVER break the CLI)
        return 0


# EOF
