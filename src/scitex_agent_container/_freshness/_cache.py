"""The cache: written by the refresher, read by every ``sac`` invocation.

WHY A CACHE AND NOT A LOOKUP
---------------------------
The warning has to appear when the operator types ``sac`` — that is the
whole point; a README nobody opens is not a control. But a PyPI lookup in
the CLI hot path would be a ~200-2000 ms network round trip on every
single command, and would HANG the CLI on a flaky link. A version check
that makes ``sac`` slow, or that wedges it when the network is bad, gets
switched off within a day, and then there is no check at all.

So the two halves are split by who can afford to wait:

* the **refresher** (cron / ``sac freshness refresh``) pays the network
  cost, off the interactive path, and writes this file;
* the **CLI** reads this file and nothing else. No socket, no subprocess.

STALE CACHE IS UNKNOWN, AND UNKNOWN IS SILENT
---------------------------------------------
Beyond ``ttl_s`` we do not trust our own file. An old cache means the
refresher died, and a dead refresher's last answer is not evidence about
the present — it is a fossil. Returning it would be the same class of
bug as the one this subsystem exists to kill: a confident statement
backed by nothing current. So an expired cache reads as ``None``
(UNKNOWN), and UNKNOWN says nothing at all.

Default TTL is 24 h against an hourly refresher — 24 consecutive misses
before we fall silent. Deliberately generous: this host runs at load ~60
and a cron job can simply not get scheduled for a long while. A tight TTL
here would not make us safer, it would just make us blind and noisy by
turns.

PATH RESOLUTION
---------------
``scitex_config._ecosystem.local_state`` is the ecosystem's canonical
resolver and would be the natural import — but it costs **1.59 s** to
import (measured: ``python -X importtime`` => 1,593,638 us cumulative),
and this module is on a path with a ~150 ms budget for the WHOLE CLI.
Importing it here would make every ``sac`` command ten times slower,
which is a guaranteed revert.

So the one-line contract (``$SCITEX_DIR``, default ``~/.scitex``) is
mirrored in stdlib here, and ``test__cache.py`` asserts our resolution
equals ``local_state``'s for both the default and the ``$SCITEX_DIR``
override. If the ecosystem ever changes that contract, the test fails
loudly instead of the two silently disagreeing about where the file is.
That is SSOT held by a test rather than by an import we cannot afford.

Everything is resolved AT CALL TIME. Nothing here is a module-level
``Path.home()`` constant: ``$HOME`` is ``/home/agent`` inside a container
and ``/home/ywatanabe`` on the host, and an import-time constant cannot
be redirected by a test fixture (or by a container) that sets the env
afterwards. That bug has already cost this repo a day.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ._model import FreshnessReport

__all__ = ["DEFAULT_TTL_S", "cache_path", "read_cache", "write_cache"]

# 24 h against an hourly refresher. See module docstring.
DEFAULT_TTL_S = 24 * 60 * 60

# Relative fragment only — never joined to a home dir at import time.
_CACHE_SUBPATH = ("agent-container", "runtime", "deploy-freshness.json")

_ENV_CACHE = "SAC_FRESHNESS_CACHE"
_ENV_SCITEX_DIR = "SCITEX_DIR"
_ENV_TTL = "SAC_FRESHNESS_TTL_S"


def scitex_dir() -> Path:
    """``$SCITEX_DIR``, else ``~/.scitex``. Resolved per call.

    Mirrors ``scitex_config._ecosystem.local_state.user_root()``; the
    equivalence is pinned by a test rather than by an import we cannot
    afford on the CLI hot path (see module docstring).
    """
    env = os.environ.get(_ENV_SCITEX_DIR)
    return Path(env) if env else Path.home() / ".scitex"


def cache_path() -> Path:
    """Where the freshness cache lives. Resolved per call.

    ``$SAC_FRESHNESS_CACHE`` overrides it outright — that is the seam
    tests use, and the way a container can be pointed at the host's
    cache instead of its own empty ``$HOME``.
    """
    override = os.environ.get(_ENV_CACHE)
    if override:
        return Path(override)
    return scitex_dir().joinpath(*_CACHE_SUBPATH)


def ttl_s() -> int:
    """Cache lifetime. ``$SAC_FRESHNESS_TTL_S`` overrides the default.

    A garbage value falls back to the default rather than raising — a
    typo in an env var must not break ``sac``.
    """
    raw = os.environ.get(_ENV_TTL)
    if not raw:
        return DEFAULT_TTL_S
    try:
        value = int(raw)
    except ValueError:  # stx-allow: fallback (reason: a malformed env var must not break the CLI; the default is the safe answer)
        return DEFAULT_TTL_S
    return value if value > 0 else DEFAULT_TTL_S


def write_cache(report: FreshnessReport, path: Path | None = None) -> Path:
    """Atomically publish a report. Returns the path written.

    tmp + ``os.replace`` so a reader never sees a half-written file: the
    CLI reads this on every invocation, and a torn read would surface as
    a spurious UNKNOWN at best.
    """
    target = path or cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target


def read_cache(
    path: Path | None = None,
    *,
    now: float | None = None,
    max_age_s: int | None = None,
) -> FreshnessReport | None:
    """Load the cached report, or ``None`` when it cannot be trusted.

    ``None`` (=> UNKNOWN => silence) for every one of: no file, an
    unreadable file, malformed JSON, a report with no timestamp, and a
    report older than the TTL. Each of those is an absence of current
    evidence, and this function refuses to dress any of them up as an
    answer.
    """
    target = path or cache_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # stx-allow: fallback (reason: missing/corrupt cache is UNKNOWN by design -- the caller stays silent)
        return None
    if not isinstance(raw, dict):
        return None

    report = FreshnessReport.from_dict(raw)
    if not report.generated_at:
        return None

    age = (time.time() if now is None else now) - report.generated_at
    limit = ttl_s() if max_age_s is None else max_age_s
    if age > limit:
        return None
    return report


# EOF
