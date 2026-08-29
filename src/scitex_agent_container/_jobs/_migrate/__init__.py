"""The one-time cutover from ``sac.<job>`` to ``scitex-agent-container-<job>``.

The ecosystem decided (operator, 2026-08-11) that every federated job is
named ``scitex-<pkg>-<name>`` with hyphens only — no ``.`` and no ``_`` —
because ``JobSpec.name`` becomes a systemd unit FILENAME verbatim and a
dot in a unit name reads as systemd's template/instance separator to every
human who has ever typed ``systemctl``. scitex-dev's auditor enforces it
as PS-226 (charset, error) and PS-227 (package qualification, warning).

Renaming a job therefore RENAMES ITS UNIT, and that is a live
double-supervisor hazard rather than a cosmetic change. This package is
the migration that makes the rename safe:

* :mod:`._renames`   — the table: old name, new name, and the stated
                       reason for any job deliberately held back.
* :mod:`._plan`      — the ORDER (stop -> disable -> carry drop-ins ->
                       displace -> reload -> install -> logging ->
                       verify), enforced by construction.
* :mod:`._verify`    — exactly-one-supervisor, counted over BOTH names.
* :mod:`._logsink`   — predictable logging, as a drop-in, on scitex-dev's
                       own path convention.
* :mod:`._selection` — the knob for which declared jobs run on this host.

Everything here is pure except the ``systemctl`` probe; the executor lives
in ``cli_pkg/_dev_jobs_migrate.py``, so the planning half is testable
against real specs without touching a host.
"""

from __future__ import annotations

from ._logsink import (
    LOG_DIR,
    LOG_PACKAGE,
    LOGGING_DROPIN,
    log_path,
    log_slug,
    logging_dropin_text,
)
from ._plan import (
    ACTION_ORDER,
    Step,
    assert_never_touches_listen,
    default_install_argv,
    plan,
    plan_one,
)
from ._renames import (
    KIND_UNIT_SUFFIXES,
    NEVER_TOUCH,
    RENAMES,
    Rename,
    by_local,
    units_for,
)
from ._selection import (
    SELECT_ALL,
    SELECTION_ENV,
    SELECTION_FILE,
    explain,
    is_selected,
    parse_selection,
    selection,
    selection_path,
)
from ._verify import Supervisors, systemd_user_available, verify_exactly_one

__all__ = [
    "ACTION_ORDER",
    "KIND_UNIT_SUFFIXES",
    "LOGGING_DROPIN",
    "LOG_DIR",
    "LOG_PACKAGE",
    "NEVER_TOUCH",
    "RENAMES",
    "SELECTION_ENV",
    "SELECTION_FILE",
    "SELECT_ALL",
    "Rename",
    "Step",
    "Supervisors",
    "assert_never_touches_listen",
    "by_local",
    "default_install_argv",
    "explain",
    "is_selected",
    "log_path",
    "log_slug",
    "logging_dropin_text",
    "parse_selection",
    "plan",
    "plan_one",
    "selection",
    "selection_path",
    "systemd_user_available",
    "units_for",
    "verify_exactly_one",
]
