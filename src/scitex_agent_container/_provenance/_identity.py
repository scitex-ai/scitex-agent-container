#!/usr/bin/env python3
# File: src/scitex_agent_container/_provenance/_identity.py

"""The ``sac --version`` fast path: WHICH code is loaded, and from WHERE.

A declared version string cannot answer "is my fix deployed?". ``0.21.13``
reads identically on a machine where the fix shipped and one where it did
not, because a fix that does not bump the version does not move the
number. Every layer of this project has been burned by that: an operator
concluding "OK, it's the latest"; a SIF-baked 0.9.4 accepted as 0.9.5; a
test run reporting 1087 passed while importing site-packages instead of
the worktree.

So this reports three things instead of one:

* ``version``  — what the distribution DECLARES (unchanged, still first
  on the line, so ``sac --version | cut -d' ' -f3`` keeps working).
* ``commit``   — what the code IS. Live-read from ``.git`` when running
  from a checkout, else the sha baked in at build time.
* ``origin``   — where the loaded module actually CAME FROM.

COST: ~0.5 ms on top of the ``importlib.metadata`` lookup ``--version``
already paid. No subprocess (a ``git rev-parse`` fork is ~89 ms), no tree
walk (hashing the tree is ~35 ms). The expensive, exhaustive checks live
in ``sac provenance``.

WHY LIVE GIT WINS OVER THE BAKED STAMP: in an editable install the stamp
is written once at ``pip install -e`` time and is stale the moment you
commit. The checkout on disk is the truth. In a wheel/SIF there is no
checkout, so the stamp is the truth. Each source is preferred exactly
where it is the authoritative one.
"""

from __future__ import annotations

import sys as _sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path

from ._git import head_sha, repo_root_for_package

__all__ = [
    "DIST_NAME",
    "baked",
    "declared_version",
    "format_terse",
    "identity",
    "origin_mismatch",
    "package_dir",
    "running_version",
]

DIST_NAME = "scitex-agent-container"


def package_dir() -> Path:
    """Absolute path of the LOADED ``scitex_agent_container`` package."""
    return Path(__file__).resolve().parent.parent


def origin_mismatch(project_root: str | Path) -> str | None:
    """Fail-loud message when the LOADED package did not come from ``<root>/src``.

    Returns ``None`` when the import resolved inside ``project_root`` — the only
    acceptable outcome for a test run — else the text a developer needs, with
    BOTH paths named.

    WHY THIS IS NOT ``_audit._check_shadowed``. That check asks "is the loaded
    module the INSTALLED distribution?" and answers it correctly. This asks a
    DIFFERENT question — "did the loaded module come from the repo whose tests
    are running?" — and only the caller knows the answer to "which repo is
    that", which is why the root is a parameter.

    The difference is not academic; it is the whole bug. Under a bare ``pytest``
    the import resolves to site-packages AND site-packages IS the installed
    distribution, so ``audit()`` sees no shadowing and returns ``ok=True`` with
    zero anomalies (measured 2026-07-14) — on the exact scenario ``_audit``'s
    own docstring cites. It detects a worktree shadowing an install; the bug is
    an install shadowing the worktree, and nothing in ``audit()`` has any notion
    of "the worktree" to compare against.

    The verdict is the PATH. Never the version string: ``0.21.13`` on this host's
    site-packages copy against ``0.21.20`` in the tree, and two host binaries
    reporting 0.21.11 and 0.21.13 while executing the same working tree. A stale
    ``.dist-info`` advertises a number whose code is gone. ``__file__`` cannot.
    """
    src_root = Path(project_root).resolve() / "src"
    origin = package_dir()

    if origin.is_relative_to(src_root):
        return None

    return (
        "\n"
        "=================== WRONG PACKAGE UNDER TEST ===================\n"
        "`import scitex_agent_container` did NOT resolve inside the repo\n"
        "whose tests are running. This run would report PASS/FAIL for code\n"
        "that is not the code you are editing — a green here means nothing.\n"
        "\n"
        f"  imported from : {origin}\n"
        f"  expected under: {src_root}\n"
        "\n"
        "This is an ERROR, not a warning. A test run against the wrong\n"
        "package is not a weaker signal than no run; it is a FALSE one.\n"
        "\n"
        'Fix: `pythonpath = ["src"]` under [tool.pytest.ini_options] in\n'
        "pyproject.toml puts this checkout ahead of site-packages. If it is\n"
        "already there, something is prepending to sys.path before pytest --\n"
        "check for a stale editable `.pth` in site-packages (on this fleet one\n"
        "pointed at an unrelated agent's worktree) or a real copy of the\n"
        "package installed into site-packages, which shadows both.\n"
        "\n"
        "NOTE: the version string cannot detect this. It is a fossil -- a\n"
        "stale .dist-info happily reports a number that matches nothing on\n"
        "disk. The module PATH above is the only truth. Run `sac provenance`\n"
        "for the full picture (duplicate .dist-info, patched bytes, ...).\n"
        "===============================================================\n"
    )


def baked() -> dict:
    """Return the build stamp baked in by the build hook, or ``{}``.

    Absent in a plain git checkout that was never built or installed —
    which is fine, because that case reads its commit live from ``.git``.
    """
    try:
        from ._build_info import STAMP  # type: ignore[import-not-found]
    except ImportError:  # stx-allow: fallback (reason: an unbuilt source tree has no stamp by design; the live-git path covers it)
        return {}
    return dict(STAMP) if isinstance(STAMP, dict) else {}


def declared_version() -> str:
    """The version the INSTALLED distribution advertises.

    This is the number that LIES, and it is kept — under a name that says
    what it is — precisely so the lie can be shown next to the truth. For an
    editable install it is frozen at ``pip install -e`` time and never moves
    again, however many times you ``git pull``. Use :func:`running_version`
    for the answer to "what am I actually executing?".
    """
    try:
        return _dist_version(DIST_NAME)
    except PackageNotFoundError:  # stx-allow: fallback (reason: running straight off a source tree with nothing installed)
        return baked().get("version") or "0.0.0+unknown"


def running_version() -> tuple[str | None, str]:
    """``(version, source)`` for the code actually executing. Never raises.

    Delegates to ``scitex_dev.versioning`` via ``_freshness`` — sac does not
    reimplement the judgment. When the primitive is unavailable this returns
    the metadata claim tagged ``"metadata"`` rather than silently passing a
    fossil off as verified; the tag is what lets the caller say so.
    """
    from .._freshness import running_version as _running

    return _running()


def identity(*, verify_content: bool = True) -> dict:
    """Resolve the identity of the code that is actually imported.

    ``install`` is one of:

    * ``src``     — loaded from a live git checkout (editable install, or
      a bare ``PYTHONPATH=<worktree>/src``). ``commit`` is HEAD, read
      live. Note the WORKING TREE is not verified here — uncommitted
      edits do not move HEAD. ``sac provenance`` hashes the bytes.
    * ``wheel``   — an installed/copied tree carrying a build stamp.
    * ``unknown`` — no checkout and no stamp: a tree built before this
      existed, or hand-copied. Says so rather than guessing.

    ``version`` is the RUNNING version, not the declared one. That swap is
    the point of this function now: on the operator's host five sac installs
    reported 0.21.24 / 0.21.22 / 0.21.21 / 0.21.11 / none depending on how
    you invoked it, and his own editable ``.venv`` advertised 0.21.21 while
    executing current develop, because ``importlib.metadata`` reads a
    ``.dist-info`` frozen at install time. ``declared`` keeps that claim
    alongside, so a disagreement between the two is visible rather than
    merely resolved.

    ``executable`` names the interpreter that answered. Together with
    ``origin`` it is what makes a currency verdict actionable: "0.21.21 is
    behind 0.21.24" is unusable when five installs could be speaking.

    ``verify_content=False`` skips the content probe and reports the
    declared number tagged ``"metadata"``. It exists for callers that need
    the sub-millisecond path and can tolerate a fossil; the default is the
    honest answer, because ``--version`` is an explicit request whose whole
    job is to be right.
    """
    origin = package_dir()
    stamp = baked()
    root = repo_root_for_package(origin)

    if verify_content:
        version, version_source = running_version()
    else:
        version, version_source = None, "metadata"
    declared = declared_version()
    if not version:
        version, version_source = declared, "metadata"

    common = {
        "version": version,
        "declared": declared,
        "version_source": version_source,
        "executable": _sys.executable,
    }

    if root is not None:
        commit = head_sha(root)
        if commit:
            return {
                **common,
                "commit": commit,
                "commit_source": "git",
                "code_hash": None,
                "built_at": None,
                "install": "src",
                "origin": str(origin),
                "repo_root": str(root),
            }

    commit = stamp.get("commit")
    return {
        **common,
        "commit": commit,
        "commit_source": stamp.get("commit_source") if commit else None,
        "code_hash": stamp.get("code_hash"),
        "built_at": stamp.get("built_at"),
        "install": "wheel" if stamp else "unknown",
        "origin": str(origin),
        "repo_root": None,
    }


def short_id(info: dict) -> str:
    """The token that MOVES when the code moves.

    ``g<sha>`` when a commit is known; else ``h<digest>`` from the
    build-time content hash — so even a wheel built with no ``.git``
    anywhere still reports something that changes when the code changes.
    """
    if info.get("commit"):
        return "g" + info["commit"][:8]
    if info.get("code_hash"):
        return "h" + info["code_hash"][:8]
    return "unknown"


def format_terse(info: dict) -> str:
    """One line. Keeps click's ``<prog>, version <X.Y.Z>`` prefix intact.

    Scripts that parse the third whitespace field still get the version —
    and now they get the RUNNING one, so the habit is no longer a trap.

    Three things are appended, each because its absence caused a real
    misdiagnosis:

    * ``from <origin>``   — WHICH install is speaking. Five of them on one
      host reported five different versions.
    * ``(python <exe>)``  — under WHICH interpreter. Which of the five you
      get depends on how you invoked sac (login shell, direct argv, systemd,
      cron), and the interpreter is what distinguishes them.
    * ``metadata claims <X>`` — shown ONLY when the frozen ``.dist-info``
      disagrees with the running code. That disagreement IS the bug the
      operator kept hitting, so it is stated rather than quietly corrected;
      seeing it once explains every confusing version report that preceded
      it.
    """
    marker = short_id(info)
    bits = [marker] if marker == "unknown" else [marker, info["install"]]
    built = info.get("built_at")
    if built:
        bits.append(built.split("T")[0])

    line = (
        f"{DIST_NAME}, version {info['version']} "
        f"({' '.join(bits)}) from {info['origin']}"
    )

    executable = info.get("executable")
    if executable:
        line += f" (python {executable})"

    declared = info.get("declared")
    if declared and declared != info.get("version"):
        line += f" [metadata claims {declared} — fossil, ignored]"
    elif info.get("version_source") == "metadata":
        # Unverified: say so. An unlabelled number that happens to come
        # from the fossil path is indistinguishable from a verified one,
        # and that indistinguishability is the whole problem.
        line += " [unverified: metadata only]"

    return line


# EOF
