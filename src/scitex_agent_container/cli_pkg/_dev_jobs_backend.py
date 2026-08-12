"""The delegation seam between ``sac dev <kind> <verb>`` and scitex-dev.

WHY SAC DOES NOT CALL ``systemctl`` ITSELF
==========================================
The obvious shortcut for ``sac dev timer enable X`` is to shell
``systemctl --user enable --now X.timer``. This module deliberately does
not, and the argument is worth stating because the shortcut looks free:

1. **One owner, or two disagreeing owners.** scitex-dev generates the
   unit files and is the SSoT for what a job's unit *is*; it explicitly
   declines to run ``systemctl`` (its ``ecosystem systemd install``
   prints the enable hint instead). If sac ran ``systemctl`` while
   scitex-dev owned the file, runtime state and file state would have two
   owners with no arbiter — the same double-owner shape that made
   ``restart.policy`` dead code and put two supervisors on ``sac listen``.
2. **N packages, N copies of the same bug.** The decided grammar is
   ``scitex-<pkg> dev <kind> <verb>`` for EVERY SciTeX package. A verb
   implemented in the package rather than in the shared layer is
   implemented once per package — N copies of the ``--user`` vs
   ``--system`` split, the unit-name derivation, the XDG_RUNTIME_DIR
   handling, and the escaping. The aggregator exists precisely to stop
   that.
3. **The verb set is not stable yet.** scitex-dev is growing
   ``ecosystem dev service`` / ``ecosystem dev timer`` with these verbs.
   Anything sac hard-codes now becomes the thing that has to be un-picked
   later.

So sac RESOLVES a delegation target and shells ``scitex-dev ecosystem
<path...> <verb>``. Where that surface does not exist yet, this module
says so precisely — naming what it probed and what it found — and prints
the exact command the operator can run by hand. It never silently
substitutes a different mechanism, because a verb that quietly does
something OTHER than what it says is worse than one that is honestly
missing.

THE TARGET IS A PATH, NOT A NAME — AND IT ALREADY MOVED
=======================================================
scitex-dev relocated its job CLI one level down, under ``ecosystem dev``
(its §13 doctrine). MEASURED on the installed scitex-dev 0.43.1, by
introspecting the real Click objects rather than reading a PR:

    ecosystem cron        -> click.Command  (NOT a Group)
                             help: "(deprecated) Forwards to
                             'ecosystem dev cron'. Removed in v0.50."
    ecosystem systemd     -> click.Command, same deprecation + removal
    ecosystem dev         -> SpecGroup      ['cron', 'systemd']
    ecosystem dev cron    -> SpecGroup      ['exec','install','list','uninstall']
    ecosystem dev systemd -> SpecGroup      ['install','list','uninstall']
    ecosystem dev service -> ABSENT         (arrives in scitex-dev #566)
    ecosystem dev timer   -> ABSENT         (arrives in scitex-dev #566)

Three traps follow, and each of them will bite a name-keyed probe:

* The old names still RESOLVE, so "does ``ecosystem systemd`` exist"
  answers YES — but they are FORWARDING SHIMS, deprecated, and
  **removed in scitex-dev v0.50**. Targeting them is a time bomb even
  though they work today.
* A shim is a ``Command``, not a ``Group``, so it has no
  ``list_commands`` and enumerates as ZERO VERBS while
  ``ecosystem systemd install`` runs fine. Zero verbs is therefore
  evidence about the PROBE, never about the surface — which is why
  :func:`resolve` treats an all-empty read as "cannot tell".
* ``ecosystem dev`` exists while its per-KIND children do not. That is
  the live state today, not a hypothetical: an all-or-nothing reading
  would refuse ``install`` on a perfectly working scitex-dev.

An earlier revision of this module blamed "lazy Click groups" for the
zero-verb reading. That was wrong — stated here rather than quietly
edited out, because the next person to see a zero will reach for the
same wrong explanation.

So the probe WALKS to ``ecosystem dev`` and keys everything by PATH
(``("dev", "timer")``, ``("systemd",)``, …), preferring the deepest,
least-deprecated match. Nothing here depends on which level the
ecosystem currently mounts its groups at.

READ-ONLY IS NOT THE SAME AS OWNING IT
======================================
Nothing here mutates host state. The capability probe is an in-process
introspection of the installed scitex-dev's own Click tree — it answers
"does this surface exist" from the code that would serve it, not from a
hard-coded table that would drift the moment scitex-dev moves the groups
again. That is the same doctrine ``_jobs_audit`` follows: read the real
thing.

``--dry-run`` / ``--yes`` ARE FORWARDED, NOT RE-IMPLEMENTED
===========================================================
scitex-dev gates every mutating job verb behind ``--dry-run`` /
``--yes``, and that gate is load-bearing: ``timer disable
sac.accounts-refresh`` stops the fleet's SOLE OAuth refresher against a
single-use refresh token. A pass-through that dropped those flags would
turn a guarded command into an unguarded one, so both travel verbatim
and ``--dry-run`` is offered on every mutating verb sac exposes.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

import click

#: The ecosystem subcommand that serves a kind on the surface that has
#: SHIPPED: ``cron`` for cron jobs, and ``systemd`` for both unit kinds,
#: which it lumps together.
#:
#: This is a FALLBACK, consulted only after the preferred per-kind
#: surface (``dev service`` / ``dev timer``) is probed for and found
#: absent. It disappears on its own the moment scitex-dev ships those
#: groups — no coordinated release, no flag.
LEGACY_GROUP_FOR_KIND: dict[str, str] = {
    "service": "systemd",
    "timer": "systemd",
    "cron": "cron",
}

#: The subgroup scitex-dev moved its job CLI under. Probed for, never
#: assumed: an older scitex-dev mounts the groups at the top level and a
#: newer one under here, and both must work without a sac release.
DEV_SUBGROUP = "dev"

#: Verbs the shipped ecosystem surface has always had. Used ONLY when
#: introspection cannot decide, so a working install is never refused
#: just because we could not read its Click tree.
SHIPPED_VERBS: frozenset[str] = frozenset({"list", "install", "uninstall"})

#: Verbs that change host state. scitex-dev gates each of these behind
#: ``--dry-run`` / ``--yes``; sac offers both and forwards them verbatim
#: rather than deciding on the operator's behalf.
MUTATING_VERBS: frozenset[str] = frozenset(
    {"install", "uninstall", "enable", "disable", "start", "stop", "restart"}
)

# Sentinel distinguishing "not probed yet" from "probed, unavailable".
_UNPROBED: object = object()
_VERBS_CACHE: object = _UNPROBED


@dataclass(frozen=True)
class Delegation:
    """One resolved ``scitex-dev ecosystem <path...> <verb>`` target.

    ``path`` is a TUPLE, not a name, because the ecosystem moved its job
    groups under ``ecosystem dev`` and may move them again. Storing the
    resolved path means :func:`invoke` never has to know which level they
    are mounted at.

    ``evidence`` is mandatory and non-empty for the same reason
    ``_jobs_audit.Finding.detail`` is: a verdict that cannot say how it
    was reached is a claim, and a claim is what put a group name in a
    kind filter for weeks.
    """

    path: tuple[str, ...]
    verb: str
    supported: bool
    evidence: str

    def __post_init__(self) -> None:
        if not self.path or not all(self.path) or not self.verb:
            raise ValueError("Delegation needs a non-empty path and a verb")
        if not self.evidence:
            raise ValueError(
                f"Delegation({self.group}/{self.verb}) must state its evidence"
            )

    @property
    def group(self) -> str:
        """The path as it is typed: ``"cron"``, ``"dev timer"``, …"""
        return " ".join(self.path)


def reset_capability_cache() -> None:
    """Forget the probed surface. For tests that install a different one."""
    global _VERBS_CACHE
    _VERBS_CACHE = _UNPROBED


def _child(command, name: str):
    """Return the named subcommand of ``command``, or None."""
    getter = getattr(command, "get_command", None)
    if getter is not None:
        try:
            with click.Context(command) as ctx:
                return getter(ctx, name)
        except Exception:  # stx-allow: fallback (reason: another package's Group subclass may need a context we cannot build — fall back to the eager dict)
            pass
    return (getattr(command, "commands", {}) or {}).get(name)


def _leaf_verbs(command) -> frozenset[str]:
    """The subcommand names of one ecosystem group.

    ``Group.list_commands(ctx)`` rather than ``Group.commands`` so a LAZY
    group is read correctly — the eager dict can be empty while the group
    is fully featured.

    Returns an EMPTY set for a node that is a plain ``Command`` (a
    forwarding shim) rather than a ``Group``. That empty is honest — a
    shim genuinely has no enumerable verbs — and callers must read it as
    "cannot tell", never as "serves nothing"; see :func:`resolve`.

    CORRECTION, so the next reader does not inherit a wrong mechanism:
    an earlier revision claimed laziness EXPLAINED the "four cron verbs
    locally, zero in CI" split. It did not. Measured afterwards, the real
    cause was scitex-dev relocating those verbs under ``ecosystem dev``
    and leaving a deprecated ``Command`` shim behind at the old name.
    Reading ``list_commands`` is still correct; it was simply not the fix
    for that symptom — walking into ``dev`` is.
    """
    lister = getattr(command, "list_commands", None)
    if lister is not None:
        try:
            with click.Context(command) as ctx:
                return frozenset(lister(ctx))
        except Exception:  # stx-allow: fallback (reason: another package's Group subclass may need a context we cannot build — fall back to the eager dict)
            pass
    return frozenset(getattr(command, "commands", {}) or {})


def _walk(ecosystem) -> dict[tuple[str, ...], frozenset[str]]:
    """Map ``path under ecosystem -> its verbs``, one level into ``dev``.

    Depth is bounded to the job groups on purpose: enumerating all ~51
    ecosystem subcommands' children would pay for building trees nobody
    here consults.
    """
    tree: dict[tuple[str, ...], frozenset[str]] = {}
    for name in sorted(_leaf_verbs(ecosystem)):
        if name not in _PROBED_GROUPS and name != DEV_SUBGROUP:
            continue
        group = _child(ecosystem, name)
        if group is None:
            continue
        tree[(name,)] = _leaf_verbs(group)
        if name != DEV_SUBGROUP:
            continue
        for sub in sorted(tree[(name,)]):
            if sub not in _PROBED_GROUPS:
                continue
            child = _child(group, sub)
            if child is not None:
                tree[(name, sub)] = _leaf_verbs(child)
    return tree


#: Group names worth descending into. Everything else under ``ecosystem``
#: is unrelated to jobs.
_PROBED_GROUPS: frozenset[str] = frozenset({"service", "timer", "cron", "systemd"})


def ecosystem_verbs() -> dict[tuple[str, ...], frozenset[str]] | None:
    """Return ``{path under `ecosystem` -> verbs}`` for the INSTALLED scitex-dev.

    Keys are PATHS — ``("cron",)``, ``("dev", "timer")`` — because the job
    groups moved under ``ecosystem dev`` and the old names survive as
    empty shells. A name-keyed map cannot tell those two apart.

    ``None`` when the Click tree cannot be built at all (an old or partial
    scitex-dev). ``None`` means "cannot tell", never "unsupported" — the
    three-state discipline from ``_jobs_audit``: a false "unsupported"
    here refuses a command that would have worked.
    """
    global _VERBS_CACHE
    if _VERBS_CACHE is not _UNPROBED:
        return _VERBS_CACHE  # type: ignore[return-value]

    probed: dict[tuple[str, ...], frozenset[str]] | None
    try:
        from scitex_dev._cli.ecosystem import register_ecosystem_commands

        @click.group()
        def _probe_root() -> None:  # pragma: no cover - never invoked
            """Throwaway root; we only want the tree it gets wired onto."""

        probed = _walk(register_ecosystem_commands(_probe_root))
    except Exception:  # stx-allow: fallback (reason: introspecting another package's private CLI tree must degrade to "cannot tell", never to a refusal)
        probed = None

    _VERBS_CACHE = probed
    return probed


def candidate_paths(kind: str) -> tuple[tuple[str, ...], ...]:
    """Every ``ecosystem`` path that could serve ``kind``, best first.

    Ordered so the surface the ecosystem is moving TO wins the moment it
    exists, with no sac release: the per-kind group under ``dev``, then
    the per-kind group at top level, then the same two for the legacy
    lump-group.
    """
    legacy = LEGACY_GROUP_FOR_KIND.get(kind)
    paths: list[tuple[str, ...]] = [(DEV_SUBGROUP, kind), (kind,)]
    if legacy is not None and legacy != kind:
        paths += [(DEV_SUBGROUP, legacy), (legacy,)]
    return tuple(paths)


def _fallback_path(kind: str, tree: dict | None) -> tuple[str, ...] | None:
    """The path to ATTEMPT when the probe could not decide.

    Prefers ``dev <legacy>`` whenever a ``dev`` subgroup is known to
    exist, because that is where the shipped verbs live once the move has
    happened; otherwise the pre-move top-level name.
    """
    legacy = LEGACY_GROUP_FOR_KIND.get(kind)
    if legacy is None:
        return None
    if tree is not None and (DEV_SUBGROUP,) in tree:
        return (DEV_SUBGROUP, legacy)
    return (legacy,)


def resolve(kind: str, verb: str) -> Delegation:
    """Resolve ``sac dev <kind> <verb>`` onto a scitex-dev ecosystem target.

    Walks :func:`candidate_paths` in order and takes the first path whose
    verb set CONTAINS ``verb``.

    THE THIRD STATE IS LOAD-BEARING. If NO candidate path reports a single
    verb, that is a probe that could not read the tree, not a scitex-dev
    with no verbs — the shipped surface has had
    ``list``/``install``/``uninstall`` since 0.16.0, so zero everywhere is
    not a credible reading. Treating it as absence would refuse commands
    that work, so it degrades to attempting the best-known shipped path.

    This is exactly the state ``ecosystem dev`` present with its
    kind-children ABSENT must land in: an older scitex-dev whose groups
    have not moved yet reports nothing on ``dev service``/``dev timer``,
    and refusing there would break a working install.
    """
    tree = ecosystem_verbs()
    paths = candidate_paths(kind)
    seen = {p: ((tree or {}).get(p) or frozenset()) for p in paths}
    probed_desc = ", ".join("`ecosystem " + " ".join(p) + f" {verb}`" for p in paths)

    for path in paths:
        if verb in seen[path]:
            return Delegation(
                path=path,
                verb=verb,
                supported=True,
                evidence=(
                    "`scitex-dev ecosystem " + " ".join(path) + f" {verb}` exists"
                ),
            )

    if tree is None or not any(seen.values()):
        fallback = _fallback_path(kind, tree)
        if verb in SHIPPED_VERBS and fallback is not None:
            return Delegation(
                path=fallback,
                verb=verb,
                supported=True,
                evidence=(
                    f"could not read any job surface for kind={kind!r} "
                    f"(none of {probed_desc} reported a verb); {verb!r} has "
                    "shipped since 0.16.0, so `ecosystem "
                    + " ".join(fallback)
                    + f" {verb}` is attempted rather than refused"
                ),
            )
        return Delegation(
            path=paths[0],
            verb=verb,
            supported=False,
            evidence=(
                f"could not read any job surface for kind={kind!r} (none of "
                f"{probed_desc} reported a verb), and {verb!r} is not one of "
                f"the verbs known to have shipped "
                f"({', '.join(sorted(SHIPPED_VERBS))})"
            ),
        )

    have = "; ".join(
        "`ecosystem " + " ".join(p) + "` has: " + (str(sorted(seen[p])) or "-")
        if seen[p]
        else "`ecosystem " + " ".join(p) + "` absent or empty"
        for p in paths
    )
    return Delegation(
        path=paths[0],
        verb=verb,
        supported=False,
        evidence=(f"the installed scitex-dev serves none of {probed_desc} — {have}"),
    )


def manual_hint(kind: str, verb: str, job_name: str) -> str:
    """The command the operator can run by hand while the verb is missing.

    Reporting a command is not running it. The unit name mirrors
    scitex-dev's renderer, which derives the filename from
    ``JobSpec.name`` verbatim.
    """
    if kind == "cron":
        return "crontab -e   # scitex-dev owns the generated block"
    unit = f"{job_name}.timer" if kind == "timer" else f"{job_name}.service"
    if verb == "status":
        return f"systemctl --user status {unit}"
    if verb == "enable":
        return f"systemctl --user enable --now {unit}"
    if verb == "disable":
        return f"systemctl --user disable --now {unit}"
    return f"systemctl --user {verb} {unit}"


def build_argv(
    delegation: Delegation,
    *,
    name: str | None,
    yes: bool,
    dry_run: bool = False,
    exe: str = "scitex-dev",
) -> list[str]:
    """The exact argv :func:`invoke` will run. Separated so it is testable.

    ``--dry-run`` and ``--yes`` are FORWARDED, never interpreted here.
    scitex-dev's gate on mutating verbs is what stops ``timer disable
    sac.accounts-refresh`` from silently stopping the fleet's sole OAuth
    refresher; a pass-through that dropped either flag would convert a
    guarded command into an unguarded one, which is strictly worse than
    not offering the verb.
    """
    argv = [exe, "ecosystem", *delegation.path, delegation.verb]
    if name is not None:
        argv += ["--name", name]
    if dry_run:
        argv.append("--dry-run")
    if yes:
        argv.append("--yes")
    return argv


def invoke(
    delegation: Delegation,
    *,
    name: str | None,
    yes: bool,
    dry_run: bool = False,
) -> int:
    """Run the resolved ``scitex-dev ecosystem`` command; return its exit code.

    ``scitex-dev`` is a hard dependency of this package, so the console
    script is expected on PATH; a missing binary is a clean
    ClickException rather than a stack trace.
    """
    if not delegation.supported:
        raise ValueError("refusing to invoke an unsupported delegation")
    exe = shutil.which("scitex-dev")
    if exe is None:
        raise click.ClickException(
            "`scitex-dev` console script not found on PATH; install "
            "scitex-dev to use `sac dev` job verbs"
        )
    return subprocess.call(
        build_argv(delegation, name=name, yes=yes, dry_run=dry_run, exe=exe)
    )


__all__ = [
    "DEV_SUBGROUP",
    "Delegation",
    "LEGACY_GROUP_FOR_KIND",
    "MUTATING_VERBS",
    "SHIPPED_VERBS",
    "build_argv",
    "candidate_paths",
    "ecosystem_verbs",
    "invoke",
    "manual_hint",
    "reset_capability_cache",
    "resolve",
]
