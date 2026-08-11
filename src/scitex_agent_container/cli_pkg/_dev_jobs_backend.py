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
   ``ecosystem service`` / ``ecosystem timer`` with these verbs. Anything
   sac hard-codes now becomes the thing that has to be un-picked later.

So sac RESOLVES a delegation target and shells ``scitex-dev ecosystem
<group> <verb>``. Where that surface does not exist yet, this module says
so precisely — naming what it probed and what it found — and prints the
exact command the operator can run by hand. It never silently substitutes
a different mechanism, because a verb that quietly does something OTHER
than what it says is worse than one that is honestly missing.

READ-ONLY IS NOT THE SAME AS OWNING IT
======================================
Nothing here mutates host state. The capability probe is an in-process
introspection of the installed scitex-dev's own Click tree — it answers
"does this surface exist" from the code that would serve it, not from a
hard-coded table that would drift the moment scitex-dev ships the verbs.
That is the same doctrine ``_jobs_audit`` follows: read the real thing.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

import click

#: The ecosystem subcommand that serves a kind on the surface that has
#: SHIPPED (``scitex-dev`` <= 0.43.x): ``cron`` for cron jobs, and
#: ``systemd`` for both unit kinds, which it lumps together.
#:
#: This is a FALLBACK, consulted only after the preferred per-kind
#: surface (``ecosystem service`` / ``ecosystem timer``) is probed for
#: and found absent. It disappears on its own the moment scitex-dev
#: ships those groups — no coordinated release, no flag.
LEGACY_GROUP_FOR_KIND: dict[str, str] = {
    "service": "systemd",
    "timer": "systemd",
    "cron": "cron",
}

#: Verbs the shipped ecosystem surface has always had. Used ONLY when
#: introspection is impossible, so a working install is never refused
#: just because we could not read its Click tree.
SHIPPED_VERBS: frozenset[str] = frozenset({"list", "install", "uninstall"})

# Sentinel distinguishing "not probed yet" from "probed, unavailable".
_UNPROBED: object = object()
_VERBS_CACHE: object = _UNPROBED


@dataclass(frozen=True)
class Delegation:
    """One resolved ``scitex-dev ecosystem <group> <verb>`` target.

    ``evidence`` is mandatory and non-empty for the same reason
    ``_jobs_audit.Finding.detail`` is: a verdict that cannot say how it
    was reached is a claim, and a claim is what put a group name in a
    kind filter for weeks.
    """

    group: str
    verb: str
    supported: bool
    evidence: str

    def __post_init__(self) -> None:
        if not self.group or not self.verb:
            raise ValueError("Delegation needs both a group and a verb")
        if not self.evidence:
            raise ValueError(
                f"Delegation({self.group}/{self.verb}) must state its evidence"
            )


def reset_capability_cache() -> None:
    """Forget the probed surface. For tests that install a different one."""
    global _VERBS_CACHE
    _VERBS_CACHE = _UNPROBED


def _leaf_verbs(command) -> frozenset[str]:
    """The subcommand names of one ecosystem group.

    ``Group.list_commands(ctx)`` rather than ``Group.commands``: a LAZY
    group populates the former and leaves the latter empty, and reading
    the empty dict is how a fully-featured group is mistaken for one with
    no verbs. Measured — the same probe reported four cron verbs locally
    and zero in CI, against the same code, purely because the installed
    scitex-dev builds its tree lazily there.
    """
    lister = getattr(command, "list_commands", None)
    if lister is not None:
        try:
            with click.Context(command) as ctx:
                return frozenset(lister(ctx))
        except Exception:  # stx-allow: fallback (reason: another package's Group subclass may need a context we cannot build — fall back to the eager dict)
            pass
    return frozenset(getattr(command, "commands", {}) or {})


def ecosystem_verbs() -> dict[str, frozenset[str]] | None:
    """Return ``{ecosystem subcommand: {verbs}}`` for the INSTALLED scitex-dev.

    ``None`` when the Click tree cannot be built at all (an old or partial
    scitex-dev). ``None`` means "cannot tell", never "unsupported" — the
    three-state discipline from ``_jobs_audit``: a false "unsupported"
    here refuses a command that would have worked.
    """
    global _VERBS_CACHE
    if _VERBS_CACHE is not _UNPROBED:
        return _VERBS_CACHE  # type: ignore[return-value]

    probed: dict[str, frozenset[str]] | None
    try:
        from scitex_dev._cli.ecosystem import register_ecosystem_commands

        @click.group()
        def _probe_root() -> None:  # pragma: no cover - never invoked
            """Throwaway root; we only want the tree it gets wired onto."""

        group = register_ecosystem_commands(_probe_root)
        probed = {name: _leaf_verbs(cmd) for name, cmd in group.commands.items()}
    except Exception:  # stx-allow: fallback (reason: introspecting another package's private CLI tree must degrade to "cannot tell", never to a refusal)
        probed = None

    _VERBS_CACHE = probed
    return probed


def resolve(kind: str, verb: str) -> Delegation:
    """Resolve ``sac dev <kind> <verb>`` onto a scitex-dev ecosystem target.

    Order, preferring the surface the ecosystem is moving TO:

    1. ``ecosystem <kind> <verb>`` — the per-kind groups scitex-dev is
       growing. Chosen the moment they exist, with no sac release.
    2. ``ecosystem <legacy> <verb>`` — today's shipped surface.
    3. unsupported, with the evidence naming both probes.

    THE THIRD STATE IS LOAD-BEARING. If NEITHER candidate group reports a
    single verb, that is a probe that could not read the tree, not a
    scitex-dev with no verbs — the shipped surface has had
    ``list``/``install``/``uninstall`` since 0.16.0, so zero everywhere is
    not a credible reading. Treating it as absence would refuse commands
    that work, so it degrades to the same "attempt what has shipped" path
    as a tree that failed to build at all.
    """
    verbs = ecosystem_verbs()
    legacy = LEGACY_GROUP_FOR_KIND.get(kind)
    kind_verbs = (verbs or {}).get(kind) or frozenset()
    legacy_verbs = ((verbs or {}).get(legacy) or frozenset()) if legacy else frozenset()
    unreadable = verbs is None or not (kind_verbs or legacy_verbs)

    if unreadable:
        if verb in SHIPPED_VERBS and legacy is not None:
            return Delegation(
                group=legacy,
                verb=verb,
                supported=True,
                evidence=(
                    "could not read the installed scitex-dev's ecosystem tree "
                    f"for kind={kind!r} (neither `ecosystem {kind}` nor "
                    f"`ecosystem {legacy}` reported a single verb); {verb!r} "
                    "has shipped on `ecosystem "
                    f"{legacy}` since 0.16.0, so it is attempted rather than "
                    "refused"
                ),
            )
        return Delegation(
            group=kind,
            verb=verb,
            supported=False,
            evidence=(
                f"could not read `ecosystem {kind} {verb}` nor `ecosystem "
                f"{legacy} {verb}` from the installed scitex-dev's tree, and "
                f"{verb!r} is not one of the verbs known to have shipped "
                f"({', '.join(sorted(SHIPPED_VERBS))})"
            ),
        )

    if verb in kind_verbs:
        return Delegation(
            group=kind,
            verb=verb,
            supported=True,
            evidence=f"`scitex-dev ecosystem {kind} {verb}` exists",
        )

    if legacy is not None and verb in legacy_verbs:
        return Delegation(
            group=legacy,
            verb=verb,
            supported=True,
            evidence=(
                f"`scitex-dev ecosystem {kind}` has no {verb!r}; falling back "
                f"to `ecosystem {legacy} {verb}`, which serves kind={kind!r} "
                "on the shipped surface"
            ),
        )

    have_kind = sorted(kind_verbs)
    have_legacy = sorted(legacy_verbs)
    return Delegation(
        group=kind,
        verb=verb,
        supported=False,
        evidence=(
            f"the installed scitex-dev serves neither `ecosystem {kind} "
            f"{verb}` (has: {have_kind or 'no such group'}) nor "
            f"`ecosystem {legacy} {verb}` (has: {have_legacy or 'no such group'})"
        ),
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


def invoke(delegation: Delegation, *, name: str | None, yes: bool) -> int:
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
    cmd = [exe, "ecosystem", delegation.group, delegation.verb]
    if name is not None:
        cmd += ["--name", name]
    if yes:
        cmd.append("--yes")
    return subprocess.call(cmd)


__all__ = [
    "Delegation",
    "LEGACY_GROUP_FOR_KIND",
    "SHIPPED_VERBS",
    "ecosystem_verbs",
    "invoke",
    "manual_hint",
    "reset_capability_cache",
    "resolve",
]
