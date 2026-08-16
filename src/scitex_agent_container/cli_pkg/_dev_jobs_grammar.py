"""The SSoT tables for the ``sac dev <group> <verb>`` job grammar.

Extracted from :mod:`._dev_jobs`, which now holds only the Click command
builders. The split is along the line that already existed: these tables
are IMPORTED BY ``_jobs_audit`` to machine-check the grammar, so they
were never private detail of the command builders — they are a shared
declaration that the builders happen to be the first consumer of.

THE GROUP NAME IS THE ``JobSpec.kind``. That identity is the whole design
(operator decision, 2026-08-11 — every SciTeX package exposes
``scitex-<pkg> dev {service,timer,cron} <verb>``) and it is a
countermeasure, not a rename: the groups used to be named after the
DELIVERY MECHANISM (``cron``/``systemd``) while the filter was the KIND,
and ``_load_sac_jobs`` was then called with the GROUP NAME. ``sac dev
systemd list`` therefore asked for ``kind="systemd"``, a value
``JobSpec.validate()`` rejects, so every timer sac owns was invisible to
its own CLI while reporting "No sac systemd-kind jobs." and exit 0.

``_jobs_audit.audit_jobs`` imports :data:`GROUP_KINDS` from here rather
than re-declaring it, because an audit that restated the mapping would be
checking its own opinion — a declaration with no live counterpart, i.e.
the exact disease it exists to detect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The scitex-dev release that first ships ``scitex_dev.jobs`` (PR #91).
# scitex-dev 0.15.0 is the last release WITHOUT it; the jobs contract is
# in scitex-dev's Unreleased section -> first available in 0.16.0.
_JOBS_MIN_VERSION = "0.16.0"

#: ``sac dev <group>`` -> the ``JobSpec.kind`` values that group lists.
#:
#: Every entry except the deprecated alias maps a group to EXACTLY the
#: kind of the same name. That identity is the invariant; it is checked
#: by ``_jobs_audit`` (``Form.GROUP_IS_NOT_ITS_KIND``), not trusted.
GROUP_KINDS: dict[str, frozenset[str]] = {
    "service": frozenset({"service"}),
    "timer": frozenset({"timer"}),
    "cron": frozenset({"cron"}),
    # Deprecated alias — see DEPRECATED_GROUPS. Covers both unit kinds,
    # which is what `scitex-dev ecosystem systemd` has always selected.
    "systemd": frozenset({"service", "timer"}),
}

_YYYY_MM = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class Deprecation:
    """A dated retirement plan for a CLI surface.

    Every field is load-bearing and validated at construction, because a
    deprecation with a missing or malformed date is indistinguishable
    from no deprecation at all — which is how "temporary" aliases become
    permanent API.
    """

    since: str
    remove_after: str
    replacement: str

    def __post_init__(self) -> None:
        for field, value in (
            ("since", self.since),
            ("remove_after", self.remove_after),
        ):
            if not _YYYY_MM.match(value):
                raise ValueError(
                    f"Deprecation.{field}={value!r} must be YYYY-MM — a vague "
                    "date is the same as no date"
                )
        if self.remove_after <= self.since:
            raise ValueError(
                f"Deprecation.remove_after={self.remove_after!r} must be after "
                f"since={self.since!r}"
            )
        if not self.replacement:
            raise ValueError(
                "Deprecation.replacement must name what to use instead — a "
                "deprecation with no replacement is just a complaint"
            )

    def is_expired(self, today: str) -> bool:
        """True once ``today`` (``YYYY-MM``) is past ``remove_after``."""
        if not _YYYY_MM.match(today):
            raise ValueError(f"today={today!r} must be YYYY-MM")
        return today > self.remove_after

    def notice(self, group: str) -> str:
        """The stderr line printed on every use of the deprecated group."""
        return (
            f"DEPRECATED: `sac dev {group}` is deprecated since {self.since} "
            f"and will be REMOVED after {self.remove_after}. "
            f"Use: {self.replacement}. "
            "(The group name is now the JobSpec kind — `systemd` is a "
            "delivery mechanism, not a kind.)"
        )


#: Groups kept only for compatibility. A group listed here MUST also be
#: in GROUP_KINDS; the audit checks that a deprecated alias never becomes
#: the only way to reach a kind.
#:
#: The notice goes to STDERR, never stdout: ``sac`` commands are parsed by
#: machines, and a courtesy message on stdout is indistinguishable from
#: data. "当面" becomes permanent unless a date is written down, and a
#: date nobody enforces is the same thing — so ``test__dev_jobs.py``
#: fails the build once ``remove_after`` passes.
DEPRECATED_GROUPS: dict[str, Deprecation] = {
    "systemd": Deprecation(
        since="2026-08",
        remove_after="2026-10",
        replacement="`sac dev service` / `sac dev timer`",
    ),
}

#: Verbs that take a job NAME and act on ONE job.
#:
#: ``enable`` is DELIBERATELY ABSENT and ``disable`` deliberately present
#: — see :data:`_BULK_VERBS` for why the two halves of one switch are not
#: symmetric.
_NAMED_VERBS: frozenset[str] = frozenset(
    {"status", "start", "stop", "restart", "disable"}
)

#: How each verb reads at the start of its one-line help. Spelled out
#: rather than ``verb.capitalize()`` because that produces "Status one of
#: sac's timer jobs", which is not a sentence.
_VERB_SUMMARY: dict[str, str] = {
    "status": "Show the status of",
    "start": "Start",
    "stop": "Stop",
    "restart": "Restart",
    "enable": "Enable",
    "disable": "Disable",
    "install": "Install",
    "uninstall": "Uninstall",
}

#: Verbs that act on EVERY job of the kind unless given an optional name,
#: and mutate the host, so they require ``--yes``.
#:
#: WHY ``enable`` IS HERE AND ``disable`` IS NOT
#: ============================================
#: ``install`` has always been bulk; ``enable`` was per-name until
#: 2026-08-15. That asymmetry was not cosmetic, because APPLIED and ARMED
#: are different states and only the second one fires: scitex-dev's
#: ``_jobs_units.do_install`` writes the unit files and then merely
#: PRINTS ``systemctl --user enable --now <unit>`` to stderr without
#: running it. So ONE ``install`` command applied all nine of sac's
#: timers, and arming them took NINE more commands typed by hand — which
#: is how seven of ten came to be sitting ``disabled`` on
#: scitex-compute-04 while every declaration, registration and unit file
#: was perfectly correct. A collective apply whose arming half is
#: per-name is not a collective apply.
#:
#: ``disable`` stays strictly per-name, and that asymmetry is the point.
#: Bulk ENABLE is convergence: it drives the host toward the DECLARED
#: state, it is idempotent, and its worst case is a job that was already
#: running. Bulk DISABLE is not its inverse — it is a fleet outage with
#: one word of typing. ``sac.accounts-refresh`` is the fleet's SOLE OAuth
#: refresher against a single-use refresh token, and every account
#: expires within one access-token lifetime once it stops. No convergence
#: story requires disarming everything at once, so the verb does not
#: offer it.
_BULK_VERBS: frozenset[str] = frozenset({"install", "uninstall", "enable"})

#: ``sac dev <group>`` -> its verbs. Per KIND, deliberately not uniform.
#:
#: MATCHED VERBATIM to scitex-dev's counterpart (PR #566), because the
#: grammar is only worth anything if the two agree. A verb sac exposed
#: that the shared layer will never serve is a permanent exit-4 — a
#: declaration with no live counterpart, which is precisely what
#: ``_jobs_audit`` exists to eliminate. Two deliberate consequences:
#:
#: * ``service`` has NO ``enable``/``disable``. systemd services support
#:   them and an earlier revision here exposed them, but #566 does not,
#:   so they would be inert. If #566 adds them, add them back. The live
#:   cost is recorded in ``_dev_jobs_apply.apply_verbs``: a service is
#:   APPLIED by the provisioning path and NOT ARMED by it.
#: * ``cron`` has NO ``exec``, which #566 does have. sac declares no
#:   ``kind="cron"`` job, and ``exec``'s argv shape is positional rather
#:   than the uniform ``--name`` every other verb takes — measured on
#:   scitex-dev 0.43.1. Wiring an untestable special case for an empty
#:   group is the same disease in the other direction; it belongs with
#:   the first cron job sac owns.
#:
#: The rest is per-kind reasoning:
#:
#: * ``service`` — a long-running unit: the runtime lifecycle applies.
#: * ``timer``   — scheduled, not run by hand. ``enable``/``disable`` is
#:   the systemd idiom for a timer (``enable --now`` starts it), so
#:   ``start``/``stop``/``restart`` would be three ways to say the same
#:   thing with different edge cases.
#: * ``cron``    — a crontab line is present or commented out. There is
#:   no runtime object to start, stop or ask for status, so those verbs
#:   do not exist here instead of existing and erroring.
#: * ``systemd`` — the deprecated alias keeps EXACTLY its historical
#:   surface. New verbs are reachable only through the kind groups, so
#:   nothing new can be built on the alias. In particular it does NOT
#:   gain the bulk ``enable``.
GROUP_VERBS: dict[str, tuple[str, ...]] = {
    "service": (
        "list",
        "status",
        "start",
        "stop",
        "restart",
        "install",
        "uninstall",
    ),
    "timer": ("list", "status", "enable", "disable", "install", "uninstall"),
    "cron": ("list", "enable", "disable", "install", "uninstall"),
    "systemd": ("list", "install", "uninstall"),
}


__all__ = [
    "DEPRECATED_GROUPS",
    "Deprecation",
    "GROUP_KINDS",
    "GROUP_VERBS",
]
