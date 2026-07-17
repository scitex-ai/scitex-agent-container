"""Which repos does ``sac worktree gc --all`` sweep? The fleet's own specs.

sac already knows every repo it cares about, and it has known all along:
each agent's ``spec.yaml`` carries a ``spec.workdir``, and for the
maintainer agents that is literally the repo they work in
(``/home/<user>/proj/scitex-todo``, ...). Those workdirs ARE the repos
that grow worktrees, because they are the repos agents run tools in. So
``--all`` reads the spec tree rather than inventing a second registry
that would immediately drift from it.

Two filters make that source clean rather than merely convenient:

* **Must exist locally.** Specs describe agents on other hosts too
  (Spartan paths, in-container paths like ``/workdir``). A workdir that
  is not a directory HERE is silently skipped — this GC only ever touches
  the machine it runs on.
* **Must be a git repo TOPLEVEL.** ``git rev-parse --show-toplevel`` must
  succeed AND equal the workdir itself. A workdir that merely sits inside
  some repo (or is the default per-agent runtime workspace, which is not
  a repo at all) is not swept: sweeping the enclosing repo because an
  agent happened to chdir into a subdirectory of it would silently widen
  the blast radius past what the spec author declared.

The result is de-duplicated (many specs, one repo) and sorted, so a pass
is deterministic and reads the same twice.

Deliberately NOT discovered: repos with no agent spec pointing at them.
If a repo grows worktrees and no agent declares it, ``--all`` will not
see it — pass ``--repo <path>`` explicitly. That gap is honest and
narrow: worktree sprawl comes from agent tools, and agent tools run in
agent workdirs.
"""

from __future__ import annotations

from pathlib import Path

from ._worktree_gc_probe import run_git

__all__ = ["discover_repos", "spec_workdirs"]


def _spec_roots() -> list[Path]:
    """The agent-spec trees to read, project-scope first then user-scope.

    Mirrors ``cli_pkg._helpers._agent_list_discover._discover_defined_agents``
    — the same two roots the rest of sac treats as the canonical "agents
    defined on disk" surface.
    """
    roots: list[Path] = []
    # stx-allow: fallback (reason: project-scope is optional; absent -> skip, exactly as the agent-list discovery does)
    try:
        from scitex_config._ecosystem import local_state as _ls

        project = _ls.find_project_scope("agent-container")
        if project is not None:
            roots.append(project / "agents")
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        pass
    roots.append(Path.home() / ".scitex" / "agent-container" / "agents")
    return roots


def spec_workdirs(roots: list[Path] | None = None) -> list[str]:
    """Every ``spec.workdir`` declared by an agent on disk, in spec order.

    A tolerant raw-YAML read, NOT the full config loader: this must not
    fail because one unrelated spec is malformed, and it needs exactly one
    string. Unreadable / unparseable / workdir-less specs are skipped
    silently — a broken spec is the agent-list's problem to surface, not
    the GC's to crash on.

    ``roots`` is the test seam (real temp spec trees; no mocks).
    """
    out: list[str] = []
    for root in roots if roots is not None else _spec_roots():
        if not root.is_dir():
            continue
        # stx-allow: fallback (reason: an unreadable spec root must not crash a scheduled GC pass; skip it)
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            spec = child / "spec.yaml"
            if not spec.is_file():
                continue
            workdir = _workdir_of(spec)
            if workdir:
                out.append(workdir)
    return out


def _workdir_of(spec_path: Path) -> str:
    """``spec.workdir`` from one spec.yaml, or "" — never raises."""
    # stx-allow: fallback (reason: one malformed spec must not break discovery of the other 99; skip it silently)
    try:
        import yaml

        blob = yaml.safe_load(spec_path.read_text())
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return ""
    if not isinstance(blob, dict):
        return ""
    spec = blob.get("spec")
    if not isinstance(spec, dict):
        return ""
    workdir = spec.get("workdir")
    return workdir.strip() if isinstance(workdir, str) else ""


def _is_repo_toplevel(path: Path) -> bool:
    """True iff ``path`` is itself the toplevel of a git repo.

    Not "is inside a repo": a workdir nested in a repo must NOT drag the
    enclosing repo into the sweep. Compares resolved paths so a symlinked
    workdir (common on this fleet) still matches its own toplevel.
    """
    ok, out = run_git(path, "rev-parse", "--show-toplevel")
    if not ok or not out.strip():
        return False
    # stx-allow: fallback (reason: an unresolvable path is simply not a sweepable repo)
    try:
        return Path(out.strip()).resolve() == path.resolve()
    except (OSError, RuntimeError):
        return False


def discover_repos(roots: list[Path] | None = None) -> list[str]:
    """Local git-repo toplevels declared as some agent's ``spec.workdir``.

    De-duplicated (many agents can share a repo) and sorted, so ``--all``
    is deterministic. Returns ``[]`` when no spec declares a local repo —
    the CLI turns that into a loud "nothing to sweep, name a --repo",
    never a silent success.
    """
    seen: set[str] = set()
    for raw in spec_workdirs(roots):
        # stx-allow: fallback (reason: a malformed workdir string is not a sweepable repo; skip it)
        try:
            path = Path(raw).expanduser()
        except (OSError, RuntimeError, ValueError):
            continue
        if not path.is_dir():
            continue  # another host's path, or an in-container path
        if not _is_repo_toplevel(path):
            continue  # not a repo, or only nested inside one
        seen.add(str(path))
    return sorted(seen)
