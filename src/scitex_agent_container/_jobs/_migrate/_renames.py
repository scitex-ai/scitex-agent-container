"""THE MIGRATION TABLE: every old job name, and what it becomes.

A rename here is not cosmetic. ``scitex_dev.jobs._systemd.systemd_unit_name``
derives the unit FILENAME from ``JobSpec.name`` verbatim::

    return f"{job.name}.timer" if job.kind == "timer" else f"{job.name}.service"

so renaming a job renames its unit, and systemd treats
``sac.worktree-gc.timer`` and ``scitex-agent-container-worktree-gc.timer``
as two unrelated units with independent enablement, state and triggers.
That is why this table exists at all, and why it is the one place the old
names survive.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import _names

#: The unit suffixes a job of each kind materialises.
#:
#: A ``timer`` job is TWO units — a systemd timer triggers a companion
#: service, and scitex-dev's renderer writes both. A migration that
#: displaced only the ``.timer`` would leave the old ``.service`` behind as
#: a runnable orphan: the two-supervisor bug wearing a different hat.
KIND_UNIT_SUFFIXES: dict[str, tuple[str, ...]] = {
    "timer": (".service", ".timer"),
    "service": (".service",),
    "cron": (),
}


def units_for(name: str, kind: str) -> tuple[str, ...]:
    """Every unit filename ``name`` materialises for ``kind``."""
    if kind not in KIND_UNIT_SUFFIXES:
        raise ValueError(f"kind={kind!r} not in {sorted(KIND_UNIT_SUFFIXES)}")
    if not name:
        raise ValueError("job name must be non-empty")
    return tuple(name + suffix for suffix in KIND_UNIT_SUFFIXES[kind])


@dataclass(frozen=True)
class Rename:
    """One job's old name, new name, and whether it is cut over now.

    ``hold`` is a REASON, not a flag. A held job is one whose cutover is
    deliberately deferred to a supervised window; stating why, in the
    table, is what stops the next reader from "finishing the job" and
    taking the fleet down. A blank hold is rejected for the same reason a
    park with no reason is not a park — it is indistinguishable from an
    oversight.
    """

    old: str
    new: str
    kind: str
    hold: str | None = None

    def __post_init__(self) -> None:
        if not self.old or not self.new:
            raise ValueError("Rename needs both an old and a new name")
        if self.old == self.new:
            raise ValueError(f"Rename({self.old!r}) renames nothing")
        if self.kind not in KIND_UNIT_SUFFIXES:
            raise ValueError(
                f"Rename({self.old!r}).kind={self.kind!r} not in "
                f"{sorted(KIND_UNIT_SUFFIXES)}"
            )
        if self.hold is not None and not self.hold.strip():
            raise ValueError(
                f"Rename({self.old!r}).hold must state WHY the cutover is "
                "deferred — a hold with no reason is indistinguishable from "
                "an oversight"
            )

    @property
    def held(self) -> bool:
        """True when this job is left for an operator-supervised cutover."""
        return self.hold is not None

    @property
    def local(self) -> str:
        """The short name an operator types for this job."""
        return _names.local(self.new)

    def old_units(self) -> tuple[str, ...]:
        """Unit filenames the PRE-rename name materialises."""
        return units_for(self.old, self.kind)

    def new_units(self) -> tuple[str, ...]:
        """Unit filenames the POST-rename name materialises."""
        return units_for(self.new, self.kind)


#: Every job sac declared under the legacy prefix, and what it becomes.
#:
#: EXPLICIT, NOT DERIVED, and that is the point. After the rename lands,
#: ``provide_jobs()`` no longer mentions a single old name — so a table
#: computed from the declared specs would compute the IDENTITY mapping,
#: and the migration would become a no-op that reports success while eight
#: orphaned units keep firing on every host. The old names survive ONLY
#: here, which makes this table the historical record as well as the plan.
#:
#: The cost of explicitness is drift, so drift is machine-checked:
#: ``test_table_covers_exactly_the_declared_jobs`` asserts these ``new``
#: names are exactly what ``provide_jobs()`` declares, reading the REAL
#: provider through the REAL validator. Adding a job without a row, or a
#: row without a job, fails the build.
RENAMES: tuple[Rename, ...] = (
    Rename(
        old="sac.accounts-refresh",
        new="scitex-agent-container-accounts-refresh",
        kind="timer",
        hold=(
            "THE FLEET'S SOLE OAUTH REFRESHER, against a SINGLE-USE refresh "
            "token. Two racing refreshers revoke each other's access token; "
            "zero expires every account within hours (measured 2026-07-09/10, "
            "a total fleet stall). It is also the ONLY sac timer actually "
            "enabled and active on compute-04, so this cutover has a live "
            "blast radius the other eight do not have. OPERATOR-SUPERVISED: "
            "run `sac dev migrate-job-names --only accounts-refresh "
            "--include-held --yes` in a watched window, then confirm "
            "`sac accounts status` still resolves every account BEFORE "
            "walking away. Until then the spec keeps its legacy name ON "
            "PURPOSE, so every CLI verb keeps naming the unit that is really "
            "running — a spec renamed ahead of its unit would report the "
            "refresher as absent while it refreshes."
        ),
    ),
    Rename(
        old="sac.accounts-keepalive",
        new="scitex-agent-container-accounts-keepalive",
        kind="timer",
    ),
    Rename(
        old="sac.fleet-reconcile",
        new="scitex-agent-container-fleet-reconcile",
        kind="timer",
    ),
    Rename(
        old="sac.freshness-refresh",
        new="scitex-agent-container-freshness-refresh",
        kind="timer",
    ),
    Rename(
        old="sac.heal-agent-auth",
        new="scitex-agent-container-heal-agent-auth",
        kind="timer",
    ),
    Rename(
        old="sac.host-sync-check",
        new="scitex-agent-container-host-sync-check",
        kind="timer",
    ),
    Rename(
        old="sac.restart-login-expired-agents",
        new="scitex-agent-container-restart-login-expired-agents",
        kind="timer",
    ),
    Rename(
        old="sac.spartan-sif-bake",
        new="scitex-agent-container-spartan-sif-bake",
        kind="timer",
    ),
    Rename(
        old="sac.worktree-gc",
        new="scitex-agent-container-worktree-gc",
        kind="timer",
    ),
)

#: Units this migration must NEVER touch.
#:
#: ``sac-listen.service`` is hand-written host state that brokers
#: ``host_exec`` / spawn / restart for every agent in the fleet. It is
#: deliberately not a JobSpec (see ``_jobs_plugin.provide_jobs``) and on
#: compute-04 it carries ``sac-listen.service.d/50-secrets-envrc.conf``
#: with 28 secret paths — host state no PR can reproduce. ``sac.listen``
#: is listed too: it is the name a JobSpec WOULD have derived, and the
#: near-miss between the two is exactly what once put two supervisors on
#: 127.0.0.1:7878.
NEVER_TOUCH: frozenset[str] = frozenset(
    {
        "sac-listen.service",
        "sac-listen.timer",
        "sac.listen.service",
        "sac.listen.timer",
    }
)


def by_local(local: str) -> Rename:
    """Look up one row by its short name; raise naming the real ones."""
    for rename in RENAMES:
        if rename.local == local or rename.new == local or rename.old == local:
            return rename
    raise KeyError(
        f"no job named {local!r} in the migration table; available: "
        + ", ".join(sorted(r.local for r in RENAMES))
    )


__all__ = [
    "KIND_UNIT_SUFFIXES",
    "NEVER_TOUCH",
    "RENAMES",
    "Rename",
    "by_local",
    "units_for",
]
