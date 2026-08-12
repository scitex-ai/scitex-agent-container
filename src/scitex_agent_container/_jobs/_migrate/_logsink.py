"""Predictable logging for sac's federated jobs — as a systemd DROP-IN.

THE GAP THIS CLOSES
===================
scitex-dev already owns a job-logging convention
(``scitex_dev.jobs._logsink``): ``~/.scitex/<package>/runtime/logs/<slug>.log``,
rotated at 1 MiB, installed at fd level so a job's children are captured
too. But it is installed by ``ecosystem cron exec <name>``, which
dispatches jobs scitex-dev itself declares. sac's jobs are not dispatched
that way — their ``ExecStart`` is the real command (``sac worktree gc
--apply --all``), so nothing installs that sink and their output goes to
the journal under a unit name that CHANGES WITH EVERY RENAME.

That is the whole failure: "where did last night's worktree-gc go" had no
answer that survived a migration. The operator's TODO asks for exactly
this — "logging periodic jobs can be predictable for this".

WHY A DROP-IN, AND WHY NOT A NEW CONVENTION
===========================================
Two constraints point at the same answer.

*Not the unit file.* scitex-dev owns it and REGENERATES it on every
``install``. Anything written into the unit is erased by the next install
without a word — the difference between a convention and a wish. A
``<unit>.d/`` drop-in is systemd's sanctioned extension point and survives
regeneration, so sac extends the unit without becoming a second owner of
it.

*Not a new path.* Inventing ``~/.scitex/agent-container/logs/jobs/`` would
make sac's logs predictable and the ECOSYSTEM's logs inconsistent — a
second convention is just a second thing to look in. So the path here is
scitex-dev's convention, spelled for a unit file: ``%h`` rather than a
resolved home, because a drop-in must be correct for whichever user
systemd expands it as. ``test_the_dropin_path_matches_scitex_devs_own``
asserts the two agree by calling the REAL ``scitex_dev`` resolver, so a
convention change upstream fails sac's build instead of silently splitting
the tree in two.
"""

from __future__ import annotations

from .. import _names

#: sac's package directory under ``~/.scitex``. Short form, matching the
#: tree sac already uses (``~/.scitex/agent-container/bin/auth-heal.py``)
#: and mirroring scitex-dev's own ``dev``.
LOG_PACKAGE = "agent-container"

#: The runtime log directory as a systemd specifier path. ``%h`` is
#: expanded by systemd to the unit's home, so one drop-in text is correct
#: for every user and can be asserted verbatim in a test.
LOG_DIR = f"%h/.scitex/{LOG_PACKAGE}/runtime/logs"

#: The drop-in filename carrying the logging directives. Numbered low so
#: an operator's own drop-in (``50-``, ``90-``) always wins, and named for
#: what it does so ``ls <unit>.d/`` explains itself.
LOGGING_DROPIN = "10-logging.conf"


def log_slug(name: str, kind: str) -> str:
    """The log basename for a job, without ``.log``.

    ``<kind>-<local name>``, following scitex-dev's own slugs
    (``timer-ecosystem-self-pull``, ``cron-pr-expire``). The kind is in the
    slug because it is in theirs, and because a directory listing that
    says what KIND of thing wrote each file is worth one hyphen.
    """
    if not kind:
        raise ValueError("job kind must be non-empty")
    local = _names.local(name)
    if not local:
        raise ValueError(f"job name {name!r} has no local part")
    return f"{kind}-{local}"


def log_path(name: str, kind: str) -> str:
    """The predictable log file for a job, as a systemd specifier path."""
    return f"{LOG_DIR}/{log_slug(name, kind)}.log"


def logging_dropin_text(name: str, kind: str) -> str:
    """The ``10-logging.conf`` body that makes ``name``'s output predictable.

    ``append:`` rather than ``file:`` so a restart does not truncate the
    history the operator came to read. systemd creates the parent
    directory for ``append:`` targets, so nothing here has to mkdir.
    """
    path = log_path(name, kind)
    return (
        "# Managed by `sac dev migrate-job-names`.\n"
        "#\n"
        "# scitex-dev owns this unit file and rewrites it on every install,\n"
        "# so these directives live in a drop-in that survives. The path is\n"
        "# scitex-dev's own convention (~/.scitex/<pkg>/runtime/logs/<slug>.log)\n"
        "# spelled with %h for a unit file — not a second convention.\n"
        "[Service]\n"
        f"StandardOutput=append:{path}\n"
        f"StandardError=append:{path}\n"
    )


__all__ = [
    "LOGGING_DROPIN",
    "LOG_DIR",
    "LOG_PACKAGE",
    "log_path",
    "log_slug",
    "logging_dropin_text",
]
