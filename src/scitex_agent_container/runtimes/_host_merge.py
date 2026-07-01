"""Deep-merge the host operator's ``~/.claude/{commands,skills,hooks}`` into a
FULL-DEVELOPER agent's materialized ``$HOME/.claude``.

Why
---
A "full developer" SAC agent (``sac`` maintainer, a project-maintainer, a
contributor) is the operator working in an isolated worktree — full-host-bound,
"the workspace is the starting cwd, not a jail" (dev-agent bind policy). Such
an agent should see the operator's OWN slash-commands, skills, and (agent-safe)
hooks, not just the curated ``_shared`` agent layer. A *capsule* / *solitary*
agent gets the ``_shared`` + per-agent layers ONLY — no host bleed — preserving
hermetic isolation.

This module is the host side of the ``to_home`` materialization (ADR-0006 /
ADR-0018). The per-agent + ``_shared`` layers are materialized first by the
:mod:`_to_home` two-pass walk (real files); THEN, for a full developer, this
module overlays the host ``~/.claude/{commands,skills,hooks}`` as per-file
**symlinks** with ABSOLUTE host targets (e.g.
``/home/ywatanabe/.claude/commands/where.md``). The links resolve in-container
through the existing full-home bind — exactly like the historical
``_shared/to_home/.claude/skills -> ~/.claude/skills`` symlink resolved its
target. (Unlike the per-agent ``to_home`` symlinks, which the walk
dereference-COPIES to hermetic real content, these host-merge links are an
EXPLICIT, gated, developer-only pull and are kept AS symlinks so they always
reflect the live host.)

Merge rule (per directory, per file basename)
---------------------------------------------
  * union of host files + agent-layer files
  * the AGENT LAYER WINS on a name collision (it is materialized first as a
    real file; this module never overwrites an agent-layer entry)
  * the agent layer may EXCLUDE a host file (see ``exclude_host`` below)
  * everything host-only lands as an absolute symlink into the agent dir

Hook safety (host-session hooks must never leak)
------------------------------------------------
The host ``~/.claude/hooks`` carries operator-session-only hooks that would
misbehave inside an agent (speak-on-stop TTS, the telegram relay hooks, the
operator-message tagger, the periodic-report-metrics nag). Those are
DENY-LISTED here (:data:`_HOST_HOOK_DENY_SUBSTRINGS`) and never linked. The
agent-safe hooks are authoritative in ``_shared/to_home/.claude/hooks/`` — a
host hook with the same basename is therefore SKIPPED (agent layer wins), so a
host-session hook can never override its agent-safe counterpart.

Drift detection (fail loud / fast — operator requirement)
---------------------------------------------------------
:func:`verify_host_merge` recomputes the EXPECTED set of host-merge symlinks
from the CURRENT host + agent layers and compares it to what is materialized.
On ANY drift — a host file added/removed, a dangling link, a stale link whose
host target vanished — it returns the drift report. The boot path
(:func:`apply_host_merge`) re-derives the links from scratch every start (so a
single start self-heals), and :func:`assert_no_host_merge_drift` raises
:class:`HostMergeDriftError` for an out-of-band periodic check. No silent
fallback: a partial/stale view is never served.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

# Directory basenames pruned from the host walk (git worktrees etc.) — shared
# with the rest of SAC's heavy walkers.
from .._walk_exclusions import is_excluded_walk_dir
from ..config import AgentConfig

logger = logging.getLogger(__name__)

# The ``~/.claude`` subdirectories that get the host deep-merge for full
# developers. ``commands`` (slash-commands) and ``skills`` are pure additive
# overlays; ``hooks`` is filtered by the deny-list below.
_MERGED_SUBDIRS = ("commands", "skills", "hooks")

# A host hook whose RELATIVE path (under ~/.claude/hooks) contains any of these
# substrings is operator-session-only and is NEVER linked into an agent. This
# is the explicit exclusion set the task requires. Substring match (not exact)
# so the whole telegram family and any future variant is covered.
#
# Rationale per entry:
#   speak_on_stop / speak_on_notification — TTS on the operator's box; an agent
#       firing host espeak/say is wrong (and the deny_raw_tts guard would block).
#   telegram / encourage_telegram / limit_telegram — the operator's Telegram
#       relay + style nags; agents have their own comms surface.
#   tag_operator_messages — tags inbound operator turns; meaningless in-agent.
#   enforce_periodic_report_metrics — nags the operator session to post metrics.
#   report_to_lead / check_ci_status / check_develop_branch — operator-loop
#       session hooks, not agent behavior.
_HOST_HOOK_DENY_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "speak_on_stop",
        "speak_on_notification",
        "telegram",
        "tag_operator_messages",
        "enforce_periodic_report_metrics",
        "report_to_lead",
        "check_ci_status",
        "check_develop_branch",
    }
)

# Claude Code discovers hooks ONLY from these event-named subdirs of
# ~/.claude/hooks (each ``settings.json`` hook command references
# ``hooks/<event>/<script>``). The host's ``~/.claude/hooks`` ALSO holds large
# NON-hook subtrees — ``docs/`` (hundreds of guideline files), ``lib/`` helper
# scripts, and loose ``settings.json`` / ``*.example`` — which are NOT hooks
# and must never be linked in as such (noise + the F-CS8 bloat class). So the
# hooks merge is RESTRICTED to these top-level event dirs; everything else
# under ~/.claude/hooks is ignored. ``claude_worktree_hooks`` is included
# because the ``_shared`` agent layer ships it as a hook helper package.
_HOOK_EVENT_SUBDIRS: frozenset[str] = frozenset(
    {
        "pre-tool-use",
        "post-tool-use",
        "stop",
        "notification",
        "session-start",
        "user-prompt-submit",
        "project-switch",
        "subagent-stop",
        "pre-compact",
        "claude_worktree_hooks",
    }
)

# Roles that, ABSENT an explicit ``metadata.labels.group``, qualify an agent as
# a full developer (host deep-merge ON). Decoupled from the ACL ``lineage.group``
# knob on purpose — this gate is about WHO the agent is, not its comms ACL.
_DEVELOPER_ROLES: frozenset[str] = frozenset(
    {"project-maintainer", "maintainer", "dev-agent", "contributor"}
)

# Env override for the host ``~/.claude`` root (test seam — NO monkeypatch).
# When set, host files are read from ``$SAC_HOST_CLAUDE_DIR`` instead of the
# real ``~/.claude``. The materialized symlink TARGETS still point at this dir,
# so a test can stand up a fake host tree under tmp_path and assert the links.
_HOST_CLAUDE_DIR_ENV = "SAC_HOST_CLAUDE_DIR"

# Marker file dropped beside the linked entries recording that this dir is a
# host-merge target — lets drift detection know which links it owns (vs. a
# symlink the operator hand-placed under to_home). Hidden, one per merged dir.
_MERGE_MARKER_NAME = ".sac-host-merge"


class HostMergeDriftError(RuntimeError):
    """The materialized host deep-merge no longer matches host + agent layers.

    Raised by :func:`assert_no_host_merge_drift` (the out-of-band periodic
    check). The operator requirement is fail-loud: a developer agent must never
    silently serve a stale or partial view of the host's commands/skills/hooks.
    The message lists every drifted entry (host file added but unlinked, a link
    whose host target vanished, a dangling link). The boot path re-materializes
    from scratch, so a restart self-heals; this error is for detecting drift
    WITHOUT a restart.
    """


# --- gate ------------------------------------------------------------------


def is_full_developer(config: AgentConfig) -> bool:
    """True iff ``config`` is a FULL-DEVELOPER agent (host deep-merge ON).

    Gate (decoupled from the ACL PR):

      * ``metadata.labels.group == "developer"``                         → True
      * ``metadata.labels.group == "solitary"``                          → False
        (a capsule/solitary agent gets the ``_shared``/per-agent layers ONLY)
      * group UNSET and ``metadata.labels.role`` in :data:`_DEVELOPER_ROLES`
                                                                          → True
      * otherwise                                                        → False

    ``metadata.labels`` lands on :attr:`AgentConfig.labels` at load time.
    """
    labels = getattr(config, "labels", None) or {}
    group = str(labels.get("group", "") or "").strip().lower()
    if group == "developer":
        return True
    if group:  # any explicit non-developer group (e.g. "solitary") → no merge
        return False
    role = str(labels.get("role", "") or "").strip().lower()
    return role in _DEVELOPER_ROLES


# --- host root resolution --------------------------------------------------


def host_claude_dir() -> Path | None:
    """Resolve the host ``~/.claude`` root used as the deep-merge source.

    Honours ``$SAC_HOST_CLAUDE_DIR`` (test seam); else ``~/.claude``. Returns
    ``None`` when the resolved dir does not exist (no host = no merge — a clean
    no-op rather than a crash).
    """
    override = (os.environ.get(_HOST_CLAUDE_DIR_ENV, "") or "").strip()
    base = Path(override).expanduser() if override else Path("~/.claude").expanduser()
    return base if base.is_dir() else None


# --- merge planning --------------------------------------------------------


def _hook_is_denied(rel: Path) -> bool:
    """True iff a host hook at relative path ``rel`` is session-only (deny-list)."""
    rel_str = str(rel)
    return any(sub in rel_str for sub in _HOST_HOOK_DENY_SUBSTRINGS)


def _iter_host_files(host_subdir: Path, *, is_hooks: bool):
    """Yield ``(rel_path, host_abs_path)`` for every eligible host file.

    Walks ``host_subdir`` recursively, pruning excluded dirs (worktrees) and
    hidden/`.old` log junk. ``rel`` is the path RELATIVE to ``host_subdir`` so
    it maps 1:1 into the agent dir.

    For ``hooks`` the walk is RESTRICTED to the recognized event subdirs
    (:data:`_HOOK_EVENT_SUBDIRS`) — Claude Code only loads hooks from those, and
    the host ``~/.claude/hooks`` otherwise carries large non-hook subtrees
    (``docs/``, ``lib/``, loose ``settings.json``) that must never be linked as
    hooks. Within an event dir, deny-listed scripts and non-script docs
    (``*.md`` / ``*.log`` / ``.gitignore``) are skipped.
    """
    if not host_subdir.is_dir():
        return
    if is_hooks:
        roots = [
            host_subdir / name
            for name in sorted(_HOOK_EVENT_SUBDIRS)
            if (host_subdir / name).is_dir()
        ]
    else:
        roots = [host_subdir]
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune excluded + hidden dirs in place so os.walk skips them.
            dirnames[:] = [
                d
                for d in dirnames
                if not is_excluded_walk_dir(d) and not d.startswith(".")
            ]
            dp = Path(dirpath)
            for fn in sorted(filenames):
                if fn.startswith("."):
                    continue  # hidden / .run.log artifacts
                src = dp / fn
                if not src.is_file():
                    continue
                # rel is always relative to the SUBDIR root (commands/skills/
                # hooks), so an event-dir hook lands at hooks/<event>/<script>.
                rel = src.relative_to(host_subdir)
                if is_hooks:
                    if _hook_is_denied(rel):
                        continue
                    # Only ship executable hook scripts, not docs/markers.
                    if fn.endswith((".md", ".log")) or fn == ".gitignore":
                        continue
                yield rel, src


def plan_host_merge(config: AgentConfig, home_dir: str | Path) -> "dict[Path, Path]":
    """Compute the EXPECTED host-merge symlinks for ``config`` at ``home_dir``.

    Returns a map ``{abs_link_path_in_agent_home: abs_host_target}`` — every
    host file that should be materialized as a symlink, AFTER applying:

      * the gate (empty map when not a full developer, or no host dir),
      * agent-layer-wins (a basename already present in the agent's
        ``$HOME/.claude/<subdir>/<rel>`` as a NON-host-merge entry is skipped),
      * the hooks deny-list (handled in :func:`_iter_host_files`),
      * per-agent exclusions (``exclude_skills`` / ``exclude_hooks`` substrings;
        the "agent layer may EXCLUDE a host file" rule).

    Pure (no filesystem writes) so both :func:`apply_host_merge` and
    :func:`verify_host_merge` derive from the SAME plan — drift cannot diverge
    from materialization.
    """
    plan: dict[Path, Path] = {}
    if not is_full_developer(config):
        return plan
    host_root = host_claude_dir()
    if host_root is None:
        return plan
    dest_claude = Path(home_dir) / ".claude"
    excl_skills = [s for s in (getattr(config, "exclude_skills", []) or []) if s]
    excl_hooks = [h for h in (getattr(config, "exclude_hooks", []) or []) if h]
    for subdir in _MERGED_SUBDIRS:
        host_subdir = host_root / subdir
        is_hooks = subdir == "hooks"
        excl = excl_hooks if is_hooks else (excl_skills if subdir == "skills" else [])
        dest_subdir = dest_claude / subdir
        for rel, host_abs in _iter_host_files(host_subdir, is_hooks=is_hooks):
            rel_str = str(rel)
            if any(pat in rel_str for pat in excl):
                continue  # agent layer EXCLUDES this host file
            link = dest_subdir / rel
            # Agent layer WINS: an already-materialized NON-host-merge entry
            # (a real file the walk laid down) is left untouched.
            if _is_agent_layer_entry(link):
                continue
            plan[link] = host_abs
    return plan


def _is_agent_layer_entry(link: Path) -> bool:
    """True iff ``link`` already holds an AGENT-LAYER (non-host-merge) entry.

    A real file/dir from the ``_shared``/per-agent walk wins over the host. A
    pre-existing host-merge symlink (ours) does NOT count — it is replaced so
    the link always reflects the current plan.
    """
    if link.is_symlink():
        # Our own host-merge links are managed (replaced); a symlink the
        # operator placed under to_home was already dereference-copied by the
        # walk, so any symlink here is ours.
        return False
    return link.exists()


# --- materialization -------------------------------------------------------


def apply_host_merge(config: AgentConfig, home_dir: str | Path) -> "list[Path]":
    """Materialize the host deep-merge symlinks for ``config`` into ``home_dir``.

    Idempotent + self-healing: every start REMOVES the previously-materialized
    host-merge links (tracked via the per-dir marker) and re-creates them from a
    fresh :func:`plan_host_merge`. So a host file added/removed since last start
    is reflected, and a stale/dangling link from a prior host is cleared — the
    boot half of the fail-loud drift requirement (re-materialize, never serve
    stale). Returns the list of links created (empty for a non-developer / no
    host).

    Runs AFTER the :mod:`_to_home` walk so (a) agent-layer files already exist
    for the agent-layer-wins check, and (b) the walk's symlink-deref step has
    already run and will not dereference these links.
    """
    dest_claude = Path(home_dir) / ".claude"
    # Clear prior host-merge links first (idempotency / drift self-heal).
    _clear_prior_links(dest_claude)
    plan = plan_host_merge(config, home_dir)
    created: list[Path] = []
    linked_dirs: set[Path] = set()
    for link, target in plan.items():
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            # Defensive: a leftover (e.g. dangling) link not caught by the
            # marker sweep. Replace it so the target is current.
            _unlink(link)
        link.symlink_to(target)
        created.append(link)
        linked_dirs.add(link.parent)
    # Drop a marker in each subdir we linked into so the next start's
    # _clear_prior_links knows which tree it owns.
    for d in linked_dirs:
        (d / _MERGE_MARKER_NAME).write_text("scitex-agent-container host-merge\n")
    if created:
        logger.info(
            "host-merge: linked %d host file(s) into %s/.claude (developer agent %s)",
            len(created),
            home_dir,
            config.name,
        )
    return created


def _clear_prior_links(dest_claude: Path) -> None:
    """Remove all symlinks under any dir carrying the host-merge marker.

    Only touches SYMLINKS (our managed links) — never the agent-layer real
    files. Removes the marker too; :func:`apply_host_merge` re-drops it.
    """
    if not dest_claude.is_dir():
        return
    for subdir in _MERGED_SUBDIRS:
        d = dest_claude / subdir
        if not d.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(d):
            dp = Path(dirpath)
            if not (dp / _MERGE_MARKER_NAME).exists():
                continue
            for fn in filenames:
                p = dp / fn
                if fn == _MERGE_MARKER_NAME:
                    _unlink(p)
                elif p.is_symlink():
                    _unlink(p)


def _unlink(p: Path) -> None:
    """Best-effort remove of a symlink/file (idempotent)."""
    try:
        p.unlink()
    except FileNotFoundError:
        pass


# --- drift detection -------------------------------------------------------


def verify_host_merge(config: AgentConfig, home_dir: str | Path) -> "list[str]":
    """Return a list of DRIFT findings for the materialized host deep-merge.

    Compares the EXPECTED plan (current host + agent layers) against what is on
    disk under ``<home_dir>/.claude``. A finding is emitted for each:

      * EXPECTED link MISSING — host added a file (or the link was deleted).
      * EXPECTED link points at the WRONG target.
      * MATERIALIZED host-merge link that is NO LONGER expected — host removed
        the file (the link is now stale / would dangle).
      * EXPECTED link whose host TARGET no longer exists — dangling.

    Empty list ⇒ the materialized view matches host + agent layers exactly.
    Pure read — never mutates. The single source of truth for both the boot
    self-check and the periodic out-of-band check.
    """
    findings: list[str] = []
    expected = plan_host_merge(config, home_dir)
    dest_claude = Path(home_dir) / ".claude"

    # Expected links: present? correct target? target exists?
    for link, target in expected.items():
        if not link.is_symlink():
            findings.append(f"missing host-merge link: {link} -> {target}")
            continue
        actual = Path(os.readlink(link))
        if actual != target:
            findings.append(
                f"host-merge link target drift: {link} -> {actual} (expected {target})"
            )
            continue
        if not target.exists():
            findings.append(
                f"dangling host-merge link: {link} -> {target} (host target removed)"
            )

    # Materialized-but-no-longer-expected links (host file removed).
    expected_links = set(expected.keys())
    for stale in _materialized_links(dest_claude):
        if stale not in expected_links:
            tgt = Path(os.readlink(stale)) if stale.is_symlink() else None
            findings.append(
                f"stale host-merge link: {stale} -> {tgt} "
                "(no longer in host ∪ agent layers)"
            )
    return findings


def _materialized_links(dest_claude: Path) -> "list[Path]":
    """All host-merge symlinks currently on disk (dirs carrying the marker)."""
    out: list[Path] = []
    if not dest_claude.is_dir():
        return out
    for subdir in _MERGED_SUBDIRS:
        d = dest_claude / subdir
        if not d.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(d):
            dp = Path(dirpath)
            if not (dp / _MERGE_MARKER_NAME).exists():
                continue
            for fn in filenames:
                if fn == _MERGE_MARKER_NAME:
                    continue
                p = dp / fn
                if p.is_symlink():
                    out.append(p)
    return out


def assert_no_host_merge_drift(config: AgentConfig, home_dir: str | Path) -> None:
    """Raise :class:`HostMergeDriftError` if the host deep-merge has drifted.

    The out-of-band / periodic fail-loud check. On drift it ALSO re-materializes
    (apply_host_merge) before raising, so the agent is left consistent AND the
    operator is alerted — never a silent stale view. No drift ⇒ no-op.
    """
    findings = verify_host_merge(config, home_dir)
    if not findings:
        return
    # Re-materialize so the live view is correct, THEN fail loud.
    apply_host_merge(config, home_dir)
    bullet = "\n  - ".join(findings)
    raise HostMergeDriftError(
        f"host deep-merge drift for agent {config.name!r} at "
        f"{home_dir}/.claude (re-materialized; alerting):\n  - {bullet}"
    )


__all__ = [
    "HostMergeDriftError",
    "apply_host_merge",
    "assert_no_host_merge_drift",
    "host_claude_dir",
    "is_full_developer",
    "plan_host_merge",
    "verify_host_merge",
]
