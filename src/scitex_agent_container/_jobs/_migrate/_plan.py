"""The ordered plan. THE ORDER IS THE WHOLE MODULE.

WHAT INSTALL-BEFORE-UNINSTALL COSTS
===================================
A rename derives a DIFFERENT unit filename, so installing the new unit
before removing the old one leaves BOTH on the host. Both are enabled,
both fire, both run the same command, and neither knows about the other.

This is not hypothetical. On the head node a crontab line and a systemd
unit were both supervising ``sac listen`` against DIFFERENT venvs, and the
cron one was dormant only because its ``pgrep`` guard happened to match
systemd's process. A rename is precisely the event that WAKES a dormant
second supervisor, because the guard stops matching the moment the name
changes.

So every plan is, per job::

    stop old -> disable old -> carry drop-ins -> displace old
             -> daemon-reload -> install new -> logging -> verify

built in that order BY CONSTRUCTION: each step's ``action`` indexes into
:data:`ACTION_ORDER` and ``test_install_never_precedes_displace`` asserts
the resulting ranks are non-decreasing for every job in the table. An edit
that reorders the steps fails the build instead of quietly arming a race.

Two orderings inside that sequence are also load-bearing:

*stop before disable* — ``disable`` only drops the ``.wants`` symlink. A
disable-first order leaves a RUNNING unit that nothing will start again
but nothing has stopped either.

*carry drop-ins before displace* — a ``<unit>.d/`` directory is host state
no PR can reproduce (on compute-04, 28 secret paths). Displacing the unit
first orphans its drop-ins under a name the new unit never reads: the
config silently stops applying while everything still looks installed.

WHY SAC RUNS ``systemctl`` HERE, HAVING ARGUED IT MUST NOT
==========================================================
``_dev_jobs_backend`` argues that sac must never shell ``systemctl`` for a
job, because scitex-dev owns the unit file and two owners with no arbiter
is the disease. That argument holds for every STEADY-STATE verb and
nothing here weakens it.

It does not reach this case, and the reason is exact: the units stopped
and displaced here carry the PRE-rename names, which after the rename NO
PACKAGE DECLARES. scitex-dev cannot uninstall them — its ``uninstall``
iterates the declared specs, and an undeclared unit is invisible to it. An
orphan with no owner is not a second owner; it is a leak. sac made these
names, so sac retires them — and the ``install`` step DELEGATES, so
ownership returns to scitex-dev the moment the new name exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from ._logsink import LOGGING_DROPIN
from ._renames import NEVER_TOUCH, RENAMES, Rename

#: Actions a step can be, in the ONLY order they may appear for one job.
#: :func:`plan_one` emits a subsequence of this tuple, so "install after
#: displace" is a property of the data rather than of reviewer attention.
ACTION_ORDER: tuple[str, ...] = (
    "stop",
    "disable",
    "carry-dropins",
    "displace",
    "daemon-reload",
    "install",
    "logging",
    "verify",
)


@dataclass(frozen=True)
class Step:
    """One concrete action, with the evidence for why it is in the plan.

    ``why`` is mandatory for the same reason ``Delegation.evidence`` is: a
    step that cannot say what was observed is a step nobody can review,
    and this plan mutates the supervision of the fleet's credential
    machinery.
    """

    action: str
    target: str
    why: str
    argv: tuple[str, ...] | None = None
    path: str | None = None
    dest: str | None = None
    body: str | None = None

    def __post_init__(self) -> None:
        if self.action not in ACTION_ORDER:
            raise ValueError(f"Step.action={self.action!r} not in {list(ACTION_ORDER)}")
        if not self.target:
            raise ValueError("Step needs a target")
        if not self.why:
            raise ValueError(
                f"Step({self.action}, {self.target!r}) must state its evidence"
            )

    @property
    def rank(self) -> int:
        """Position in :data:`ACTION_ORDER` — the orderedness invariant."""
        return ACTION_ORDER.index(self.action)

    def render(self) -> str:
        """One human-readable line, for ``--dry-run`` and the log."""
        if self.argv is not None:
            detail = " ".join(self.argv)
        elif self.dest is not None:
            detail = f"{self.path} -> {self.dest}"
        elif self.path is not None:
            detail = str(self.path)
        else:
            detail = self.target
        return f"{self.action:14s} {detail}"


def default_install_argv(rename: Rename) -> tuple[str, ...]:
    """The delegation that hands the new name back to scitex-dev.

    ``ecosystem dev systemd`` rather than ``ecosystem dev timer``: the
    per-KIND groups arrive in scitex-dev #566, and the scitex-dev
    INSTALLED here is 0.47.0, which predates it and serves only
    ``{cron, systemd}`` under ``dev``. Targeting the group that exists is
    the difference between a migration that runs and one that exits 4 on
    every job.

    The CLI overrides this with the capability-probed delegation from
    ``_dev_jobs_backend``, so a host with a newer scitex-dev uses the
    per-kind group without a sac release.
    """
    return (
        "scitex-dev",
        "ecosystem",
        "dev",
        "systemd",
        "install",
        "--name",
        rename.new,
        "--yes",
    )


def plan_one(
    rename: Rename,
    *,
    present: Iterable[str] = (),
    dropin_dirs: Iterable[str] = (),
    install_argv: Callable[[Rename], tuple[str, ...]] = default_install_argv,
) -> tuple[Step, ...]:
    """Build the ordered plan for ONE job.

    ``present`` is the unit FILENAMES currently on the host and
    ``dropin_dirs`` the ``<unit>.d`` directory names; both are MEASURED by
    the caller and passed in, so this stays pure and the plan is a
    function of observed state rather than of hope.

    A held job plans nothing — see ``Rename.hold``.

    A job whose old units are absent still gets ``install``, ``logging``
    and ``verify``: the rename may have half-completed on this host, and
    saying so is what ``verify`` is for.
    """
    if rename.held:
        return ()

    on_host = frozenset(present)
    have_dropins = frozenset(dropin_dirs)
    live = [u for u in rename.old_units() if u in on_host]
    steps: list[Step] = []

    for unit in live:
        steps.append(
            Step(
                action="stop",
                target=unit,
                why=f"{unit} is on this host and must not outlive its name",
                argv=("systemctl", "--user", "stop", unit),
            )
        )
    for unit in live:
        steps.append(
            Step(
                action="disable",
                target=unit,
                why=f"drop {unit}'s .wants symlink so nothing re-triggers it",
                argv=("systemctl", "--user", "disable", unit),
            )
        )

    for old_unit, new_unit in zip(rename.old_units(), rename.new_units()):
        old_dir = old_unit + ".d"
        if old_unit in live and old_dir in have_dropins:
            steps.append(
                Step(
                    action="carry-dropins",
                    target=old_dir,
                    why=(
                        f"{old_dir} holds host configuration no PR can "
                        "reproduce; orphaning it under the old name stops it "
                        "applying while everything still looks installed"
                    ),
                    path=old_dir,
                    dest=new_unit + ".d",
                )
            )

    for unit in live:
        steps.append(
            Step(
                action="displace",
                target=unit,
                why=f"retire {unit} to .old/<timestamp>/ — displaced, never deleted",
                path=unit,
            )
        )

    if live:
        steps.append(
            Step(
                action="daemon-reload",
                target=rename.old,
                why=(
                    "make systemd forget the displaced units before the new "
                    "ones arrive, so `verify` counts what really exists"
                ),
                argv=("systemctl", "--user", "daemon-reload"),
            )
        )

    steps.append(
        Step(
            action="install",
            target=rename.new,
            why=(
                "hand ownership back to scitex-dev, which renders and writes "
                "the unit for the declared spec"
            ),
            argv=tuple(install_argv(rename)),
        )
    )
    for new_unit in rename.new_units():
        if new_unit.endswith(".service"):
            steps.append(
                Step(
                    action="logging",
                    target=new_unit + ".d/" + LOGGING_DROPIN,
                    why=(
                        "make this job's output land at a path that survives "
                        "the next install, which rewrites the unit file"
                    ),
                    path=new_unit + ".d/" + LOGGING_DROPIN,
                )
            )
    steps.append(
        Step(
            action="verify",
            target=rename.new,
            why="prove exactly one supervisor survives this job's cutover",
        )
    )
    return tuple(steps)


def plan(
    renames: Sequence[Rename] = RENAMES,
    *,
    present: Iterable[str] = (),
    dropin_dirs: Iterable[str] = (),
    install_argv: Callable[[Rename], tuple[str, ...]] = default_install_argv,
) -> tuple[Step, ...]:
    """The full ordered plan across every non-held job in ``renames``."""
    on_host = frozenset(present)
    dirs = frozenset(dropin_dirs)
    steps: list[Step] = []
    for rename in renames:
        steps.extend(
            plan_one(
                rename,
                present=on_host,
                dropin_dirs=dirs,
                install_argv=install_argv,
            )
        )
    out = tuple(steps)
    assert_never_touches_listen(out)
    return out


def assert_never_touches_listen(steps: Iterable[Step]) -> None:
    """Raise if any step names a unit this migration must never touch.

    A GUARD, not a comment. ``sac-listen.service`` supervises the fleet's
    control plane; a plan that reached it would be discovered at the worst
    possible moment. Called by :func:`plan` on every plan it returns, so
    the check cannot be forgotten at a call site.
    """
    for step in steps:
        for forbidden in NEVER_TOUCH:
            if step.target in (forbidden, forbidden + ".d") or step.target.startswith(
                forbidden + ".d/"
            ):
                raise ValueError(
                    f"migration step {step.action!r} targets {step.target!r}, "
                    "which this migration must NEVER touch — it is the "
                    "fleet's control plane and is deliberately not a JobSpec"
                )


__all__ = [
    "ACTION_ORDER",
    "Step",
    "assert_never_touches_listen",
    "default_install_argv",
    "plan",
    "plan_one",
]
