"""Sibling-copy staleness warning (sac-drift family).

2026-08-15 incident: the operator edited ``~/.dotfiles/src/.scitex/.../
spec.yaml`` — the dotfiles SOURCE copy — and restarted the agent; nothing
changed, because sac loads ``~/.scitex/.../spec.yaml`` (the runtime copy,
written four hours earlier). Nothing anywhere said the file he edited was
inert. Four hours were spent discovering that by hand.

The live spec tree is DELIBERATELY not a git repo (the deploy flow
classifies ``.scitex`` as runtime state), so the git-drift check in
:mod:`._local` cannot see this at all — and must not be "fixed" into a
refusal, which would stop every agent from starting. The useful check is
different, and it is always applicable:

    a DIFFERENT copy of this spec exists at <path>, it is NEWER than the
    one I load, and I do not read it.

That is one ``stat`` per candidate, it fires exactly in the case that cost
four hours, and it is a WARNING naming both paths and both mtimes — never
a refusal, since a stale sibling copy is normal and harmless most of the
time.

Sibling discovery has two composed mechanisms; NEITHER hardcodes a host
layout (the check must keep applying when the layout changes):

* **zero-config** — the spec's intra-``.scitex`` tail (everything after
  the LAST ``.scitex`` path component of the loaded path; the canonical
  ``agent-container/agents/<agent>/spec.yaml`` location when the path has
  no ``.scitex`` anchor) re-rooted at the canonical scitex scopes:
  ``scitex_config.local_state.user_root()`` (``$SCITEX_DIR`` /
  ``~/.scitex``) and the project-scope ``.scitex`` root.
* **configured** — :data:`SIBLING_ROOTS_ENV` (``SAC_SPEC_SIBLING_ROOTS``):
  a colon-separated list of directories, each naming a ``.scitex`` tree
  itself or its PARENT (the source-tree root, e.g. ``~/.dotfiles/src``).
  Both depths are probed, so the operator points at wherever the source
  tree actually lives without knowing which form the check expects.

ADVISORY CONTRACT: this module never raises and never refuses a launch.
It degrades to a silent no-op on any error — a warning that crashes a
launch is a launch-killer wearing a warning's clothes.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from pathlib import Path

# Operator-configurable list of ``.scitex`` tree roots (or their parents).
# See the module docstring; entries are os.pathsep-separated.
SIBLING_ROOTS_ENV = "SAC_SPEC_SIBLING_ROOTS"

# This package's local-state pkg-short — the scope its specs live under.
_PKG_SHORT = "agent-container"


def spec_rel_tail(spec_path: str | Path, *, agent: str | None = None) -> Path | None:
    """The spec's path INSIDE its ``.scitex`` tree, or ``None``.

    Derived from the spec's own path — no hardcoded host layout: the
    innermost ``.scitex`` component is the tree root, and everything after
    it is the tail that re-roots identically under any other ``.scitex``
    tree. When the path carries no ``.scitex`` anchor at all, fall back to
    the canonical ``agent-container/agents/<agent>/spec.yaml`` location
    (requires ``agent``); without either an anchor or an agent there is
    nothing to compare and the check is a silent no-op.
    """
    parts = Path(spec_path).parts
    for idx in range(len(parts) - 1, -1, -1):
        if parts[idx] == ".scitex":
            if idx + 1 >= len(parts):
                return None  # the path ends AT the tree root — not a spec
            return Path(*parts[idx + 1:])
    if agent:
        return Path(_PKG_SHORT) / "agents" / agent / "spec.yaml"
    return None


def _canonical_bases() -> list[Path]:
    """The ``.scitex`` trees this host can read, with zero configuration.

    The two canonical scopes from ``scitex_config.local_state``:
    ``user_root()`` (``$SCITEX_DIR`` / ``~/.scitex``) and the project
    scope's ``.scitex`` root (``<git-repo>/.scitex`` when the cwd is in a
    repo that has one). Both are known-to-be-``.scitex``-dir roots, so a
    candidate is simply ``base / tail``.
    """
    bases: list[Path] = []
    # Import lazily so a missing scitex_config (optional dep elsewhere)
    # never breaks this advisory — the check just loses its canonical
    # scopes and degrades to configured-roots-only (or silent).
    try:
        from scitex_config._ecosystem import local_state
    except (
        Exception
    ):  # stx-allow: fallback (reason: advisory check — a missing optional dep means fewer probes, never a launch failure)
        return bases
    try:
        bases.append(local_state.user_root())
    except (
        Exception
    ):  # stx-allow: fallback (reason: a broken resolver must not crash a launch — skip this probe)
        pass
    try:
        project_scope = local_state.find_project_scope(_PKG_SHORT)
    except (
        Exception
    ):  # stx-allow: fallback (reason: the project-scope probe is best-effort — skip it on any resolver error)
        project_scope = None
    if project_scope is not None:
        bases.append(project_scope.parent)  # <repo>/.scitex
    return bases


def _configured_roots() -> list[Path]:
    """Operator-declared sibling roots from :data:`SIBLING_ROOTS_ENV`.

    Each entry may name a ``.scitex`` tree itself or its parent; the
    prober expands both depths. ``expanduser`` is applied so the operator
    can write ``~/.dotfiles/src``.
    """
    raw = os.environ.get(SIBLING_ROOTS_ENV, "")
    roots: list[Path] = []
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if entry:
            roots.append(Path(entry).expanduser())
    return roots


def candidate_sibling_paths(
    spec_path: str | Path,
    *,
    agent: str | None = None,
    extra_roots: Sequence[str | Path] = (),
) -> list[Path]:
    """Every path at which another copy of this spec could live.

    The spec's own intra-``.scitex`` tail re-rooted at: the canonical
    scitex scopes (``base / tail``) plus each configured/extra root at
    BOTH depths (``root / tail`` and ``root / .scitex / tail``). The
    loaded spec's own resolved path is excluded, as are probes resolving
    to the same file twice — a file is not a sibling of itself, and a
    symlinked copy must not be warned about its own target.
    """
    tail = spec_rel_tail(spec_path, agent=agent)
    if tail is None:
        return []
    loaded = Path(spec_path).resolve()
    probes: list[Path] = []
    for base in _canonical_bases():
        probes.append(base / tail)
    for root in [*extra_roots, *_configured_roots()]:
        root = Path(root).expanduser()
        probes.append(root / tail)
        probes.append(root / ".scitex" / tail)
    seen: set[Path] = set()
    candidates: list[Path] = []
    for probe in probes:
        try:
            resolved = probe.resolve()
        except OSError:  # stx-allow: fallback (reason: an unresolvable probe path is skipped — advisory check, never a launch failure)
            continue
        if resolved in seen or resolved == loaded:
            continue
        seen.add(resolved)
        candidates.append(probe)
    return candidates


def find_newer_siblings(
    spec_path: str | Path,
    *,
    agent: str | None = None,
    extra_roots: Sequence[str | Path] = (),
) -> list[tuple[Path, float]]:
    """Sibling copies STRICTLY newer than the loaded spec.

    Returns ``(sibling_path, sibling_mtime)`` pairs. Strictly-newer is
    deliberate: an equal or older sibling is not a signal — it is the
    normal state. The comparison is on the mtime the copy actually
    carries (symlinks resolve to their target, whose mtime is what any
    reader through that path would see). A missing spec or a candidate
    that cannot be stat'ed is skipped; this function never raises.
    """
    try:
        loaded_mtime = Path(spec_path).stat().st_mtime
    except OSError:
        return []  # no loadable spec => nothing to compare against
    results: list[tuple[Path, float]] = []
    for sibling in candidate_sibling_paths(
        spec_path, agent=agent, extra_roots=extra_roots
    ):
        try:
            stat = sibling.stat()
        except OSError:
            continue  # candidate absent on this host
        if stat.st_mtime > loaded_mtime:
            results.append((sibling, stat.st_mtime))
    return results


def _fmt_mtime(mtime: float) -> str:
    """Human-readable local timestamp for the warning's mtime columns."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))


def sibling_warning_lines(
    spec_path: str | Path,
    *,
    siblings: Sequence[tuple[Path, float]],
    agent: str | None = None,
) -> list[str]:
    """The warning text: BOTH paths and BOTH mtimes, naming the outcome.

    A stale sibling is a WARNING, never a refusal — most of the time an
    inert sibling copy is normal and harmless. The line exists so that the
    one time it is the copy the operator DID just edit, nobody spends four
    hours finding out that by hand.
    """
    who = f" for agent '{agent}'" if agent else ""
    lines = [
        f"sac-drift WARNING{who}: a DIFFERENT copy of this spec is NEWER "
        f"than the one I load, and I do not read it:",
    ]
    try:
        loaded_mtime = _fmt_mtime(Path(spec_path).stat().st_mtime)
    except OSError:
        loaded_mtime = "unknown"
    lines.append(f"  loaded:  {spec_path}   (mtime {loaded_mtime})")
    for sibling, sibling_mtime in siblings:
        lines.append(f"  sibling: {sibling}   (mtime {_fmt_mtime(sibling_mtime)})")
    lines.append(
        "  if you edited the sibling copy, that edit is NOT in effect — "
        "sac loads the 'loaded' path above."
    )
    return lines


def warn_if_newer_sibling(
    spec_path: str | Path,
    *,
    agent: str | None = None,
    stream=None,
) -> int:
    """Emit the sibling-staleness warning if (and only if) one is due.

    The launch-funnel entry point (called from
    ``warn_if_spec_source_drifted``): reads :data:`SIBLING_ROOTS_ENV`
    itself, emits through scitex-logging when ``stream is None`` (same
    lazy-import idiom as ``_local.py`` — a launch must not pay handler
    setup at import time) or the explicit ``stream``, and returns the
    number of newer siblings found (0 = silent).

    ADVISORY ONLY: never raises and never refuses a launch — any internal
    failure degrades to silence.
    """
    try:
        siblings = find_newer_siblings(spec_path, agent=agent)
        if not siblings:
            return 0
        lines = sibling_warning_lines(spec_path, siblings=siblings, agent=agent)
        if stream is None:
            import scitex_logging

            log = scitex_logging.getLogger(__name__)
            for line in lines:
                log.warning(line)
        else:
            for line in lines:
                print(line, file=stream, flush=True)
        return len(siblings)
    except (
        Exception
    ):  # stx-allow: fallback (reason: advisory check — ANY internal failure degrades to silence; a warning must never crash a launch)
        return 0


__all__ = [
    "SIBLING_ROOTS_ENV",
    "candidate_sibling_paths",
    "find_newer_siblings",
    "sibling_warning_lines",
    "spec_rel_tail",
    "warn_if_newer_sibling",
]
