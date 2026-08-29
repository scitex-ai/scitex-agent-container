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

import os
import shutil
import sys
from pathlib import Path
import subprocess
from dataclasses import dataclass

import click

# Reading the installed scitex-dev's Click tree lives in its own module;
# this one builds and runs delegations. Re-exported below so every
# existing `from ._dev_jobs_backend import ...` keeps resolving.
from ._dev_jobs_capability import (  # noqa: F401  (re-exported, see below)
    DEV_SUBGROUP,
    LEGACY_GROUP_FOR_KIND,
    _child,
    _leaf_verbs,
    _walk,
    ecosystem_verbs,
    leaf_command,
    name_is_an_option,
    name_style_for,
    reset_capability_cache,
)

# The underscore-prefixed three are re-exported ON PURPOSE. Callers reach
# for them to build a REAL Click tree and point the probe at it — a seam
# that predates this split and is not a mock: `_walk` is handed an actual
# Group. Dropping them here would have moved a working seam out from under
# its users, so they stay reachable at the name they were always at.

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
    adopt: bool = False,
    force: bool = False,
    exe: str = "scitex-dev",
    leaf: object | None = None,
) -> list[str]:
    """The exact argv :func:`invoke` will run. Separated so it is testable.

    ``leaf`` is the resolved Click command the delegation targets. It is a
    PARAMETER rather than an internal lookup so a caller — a test, most
    obviously — can hand in a real command of either argument shape and
    exercise both scitex-dev generations against whichever single version
    happens to be installed. ``None`` means "look it up", which is what
    production always does.

    ``--dry-run`` and ``--yes`` are FORWARDED, never interpreted here.
    scitex-dev's gate on mutating verbs is what stops ``timer disable
    sac.accounts-refresh`` from silently stopping the fleet's sole OAuth
    refresher; a pass-through that dropped either flag would convert a
    guarded command into an unguarded one, which is strictly worse than
    not offering the verb.

    ``--adopt`` and ``--force`` are forwarded for the same reason, and the
    argument is not hypothetical. MEASURED 2026-08-20 on scitex-compute-04:
    ``sac dev timer install host-sync-check -y`` refused because a unit
    already existed, and printed scitex-dev's own remedy — "Use --adopt to
    keep the existing supervisor (writes nothing), or --force to overwrite."
    Both flags are real options on ``scitex-dev ecosystem timer install``;
    neither existed on the wrapper, so the command answered its own advice
    with ``Error: No such option '--force'``.

    A dropped flag is worse there than a missing verb, in the same way and
    for a sharper reason: the reader meets that text while repairing a unit,
    trusts it, and the failure it produces looks like a broken CLI rather
    than a message describing a command that is not the one they ran.

    The job NAME follows :func:`name_style_for` — ``--name X`` or a bare
    positional, as the INSTALLED scitex-dev actually declares it. It was
    unconditionally ``--name`` until scitex-dev 0.48.0 made the shape
    mixed; emitting the old shape at a positional verb is rejected by
    Click before the command runs, which looks identical to sac refusing
    the verb and is not.
    """
    argv = [exe, "ecosystem", *delegation.path, delegation.verb]
    if name is not None:
        if leaf is None:
            style = name_style_for(delegation.path, delegation.verb)
        else:
            style = "option" if name_is_an_option(leaf) else "positional"
        if style == "positional":
            argv.append(name)
        else:
            argv += ["--name", name]
    if dry_run:
        argv.append("--dry-run")
    if yes:
        argv.append("--yes")
    if adopt:
        argv.append("--adopt")
    if force:
        argv.append("--force")
    return argv


def resolve_scitex_dev(
    *, executable: str | None = None, path: str | None = None
) -> str | None:
    """The ``scitex-dev`` that belongs to the interpreter we are running in.

    SIBLING OF ``sys.executable`` FIRST, then PATH. Returns ``None`` when
    neither resolves, so the caller can raise a clean ClickException.

    ``executable`` and ``path`` default to ``sys.executable`` and ``$PATH``
    and exist so this can be TESTED WITHOUT PATCHING ANYTHING. PA-306 forbids
    mocks, and it counts the ``monkeypatch`` fixture itself — correctly: a
    test that reaches in and rewrites ``sys.executable`` is asserting on a
    world it invented. Taking both as arguments lets a test lay down two real
    binaries and ask which one this function picks, which is the actual
    question.
    """
    sibling = Path(executable or sys.executable).with_name("scitex-dev")
    if sibling.exists():
        return str(sibling)
    return shutil.which("scitex-dev", path=path or os.environ.get("PATH"))


def invoke(
    delegation: Delegation,
    *,
    name: str | None,
    yes: bool,
    dry_run: bool = False,
    adopt: bool = False,
    force: bool = False,
) -> int:
    """Run the resolved ``scitex-dev ecosystem`` command; return its exit code.

    RESOLVED AS A SIBLING OF ``sys.executable`` FIRST, then PATH.

    The invariant this order exists to keep: **execute the verb in the same
    interpreter that answered the questions leading up to it.** Three reads
    precede this one write, and all three are in-process --
    :func:`_dev_jobs_capability.ecosystem_verbs` imports scitex-dev's Click
    tree to ask whether the verb exists, and ``_dev_jobs._resolve_one``
    resolves the short name against ``scitex_dev.jobs`` entry points. If the
    write then lands in a DIFFERENT installation, sac has asked A and acted
    on B, and the failure is silent and confusing rather than loud.

    MEASURED 2026-08-26, which is why this is no longer a PATH lookup. In an
    agent container, ``shutil.which("scitex-dev")`` resolved to
    ``/uvwork/bin/scitex-dev`` -- a hand-added shim whose last line execs a
    DIFFERENT package's venv. That venv's ``scitex_dev.jobs`` group contains
    ``scitex-cards`` and not ``scitex-agent-container``, so::

        sac dev timer list                      -> all 11 sac timers (in-process)
        sac dev timer status accounts-snapshot-live
            Error: no kind='timer' job named 'scitex-agent-container-...'
            Discovered: <the other package's 6 timers>

    Same host, same venv, seconds apart. scitex-dev was not wrong: it
    answered truthfully about the environment it was pointed at.

    ORDER IS THE INVERSE OF :mod:`.._sac_binary`, DELIBERATELY. That module
    tries PATH first so a test which prepends a fake binary gets exactly what
    it asked for. Here PATH-first would still find the shim and fix nothing,
    and no test shims ``scitex-dev`` (checked: the only ``shutil.which``
    reference to it under ``tests/`` is a comment in ``test_audit.py``
    recording a check that was REMOVED). Sibling-first also degrades
    correctly: when sac is not in a venv, the sibling does not exist and PATH
    still answers.

    A missing binary stays a clean ClickException rather than a stack trace.
    """
    if not delegation.supported:
        raise ValueError("refusing to invoke an unsupported delegation")
    exe = resolve_scitex_dev()
    if exe is None:
        raise click.ClickException(
            "`scitex-dev` console script not found on PATH; install "
            "scitex-dev to use `sac dev` job verbs"
        )
    return subprocess.call(
        build_argv(
            delegation,
            name=name,
            yes=yes,
            dry_run=dry_run,
            adopt=adopt,
            force=force,
            exe=exe,
        )
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
    "leaf_command",
    "manual_hint",
    "name_is_an_option",
    "name_style_for",
    "reset_capability_cache",
    "resolve",
]
