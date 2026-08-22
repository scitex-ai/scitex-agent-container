"""The absolute ``sac`` a scheduled command must name, resolved per host.

WHY A JOB COMMAND MAY NOT SAY BARE ``sac``
==========================================
Every JobSpec here is self-bounding — its head is ``/usr/bin/timeout N`` — and
that head is ABSOLUTE. scitex-dev's ``resolve_execstart`` absolutises only the
FIRST token and passes a command that already starts with ``/`` through
verbatim, so wrapping the command took the payload binary OUT of resolution.
``timeout`` then looks ``sac`` up on whatever PATH its parent had.

``_jobs_plugin``'s own docstring recorded that consequence and scoped it to a
future in which these specs are "rendered as systemd units instead", calling it
"not live today". MEASURED 2026-08-20, it was live: the ecosystem supervisor
spawns periodic jobs DIRECTLY, through the same ``resolve_execstart``, so the
payload was unresolved on that path too.

THE FAILURE NEEDS TWO CONDITIONS, AND THIS FIXES ONE OF THEM. On
scitex-compute-01, ALL TEN sac jobs sat at exit 127 with zero successes —
self-pull included, so the host could not even fetch a fix. That took BOTH a
relative payload AND a supervisor whose PATH lacked the venv. scitex-dev owns
the second condition and has already closed it: ``build_supervisor_unit_text``
emits ``Environment=PATH=<venv_bin>:...`` derived from the resolved ExecStart,
since PR #713. compute-01 was failing because its UNIT was stale — the package
had been upgraded and the unit was never regenerated, and installing a newer
version does not rewrite an existing unit.

So this module is not the repair for that host; regenerating its unit is (and
that is sequenced behind a scitex-dev release, because ``ecosystem up`` on the
older version would reinstall the cron block retired the same night). What this
module removes is the DEPENDENCY: with an absolute payload, a stale unit, an
unlucky manager env, or a hand-started supervisor can no longer produce this
class of failure at all. The condition scitex-dev fixed must be re-established
on every host after every upgrade; the one fixed here cannot regress.

WHY NOT A LITERAL PATH
======================
The rejected option was pinning one absolute ``sac``, and the objection was
correct: the install location VARIES BY HOST. This module answers that
objection instead of overriding it. ``sys.executable`` is the interpreter that
imported this plugin — the supervisor's own — and a console script installed
with this package is by construction its sibling. The answer is therefore
per-host by construction, with nothing hardcoded and no host list to maintain.

It is also the rule scitex-dev already trusts: ``resolve_execstart``'s rule 1
is ``Path(sys.executable).parent / head``, and its docstring explains at length
why the parent must NOT be ``.resolve()``d (a venv's ``bin/python`` symlinks to
an interpreter outside the venv, where no console scripts live). We inherit
that reasoning by using the same unresolved parent.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

__all__ = ["sac_bin"]


def sac_bin(*, executable: str | None = None) -> str:
    """Absolute path to the ``sac`` installed beside the running interpreter.

    Returns the bare name ``"sac"`` when that sibling is absent, and WARNS when
    it does — the same shape scitex-dev's own last-resort rule uses. That case
    means sac is importable while its console script is not installed, which is
    a broken install rather than a host we should paper over: the command will
    still fail loudly at 127, but with a breadcrumb naming the reason.

    ``executable`` is a test seam so a test can point at a venv-shaped tree it
    built on disk instead of rewriting this module's globals.
    """
    candidate = Path(executable or sys.executable).parent / "sac"
    if candidate.is_file():
        return str(candidate)
    warnings.warn(
        f"no `sac` console script beside the running interpreter "
        f"({candidate}); scheduled commands will fall back to a bare `sac` "
        f"resolved against the ambient PATH, which is exactly how every sac "
        f"job on scitex-compute-01 reached exit 127 on 2026-08-20. Install "
        f"sac into this environment.",
        RuntimeWarning,
        stacklevel=2,
    )
    return "sac"
