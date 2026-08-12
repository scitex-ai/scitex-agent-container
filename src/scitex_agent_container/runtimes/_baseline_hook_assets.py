"""Deploy sac's OWN packaged hook scripts into the agent's ``$HOME``.

``_baseline_assets/<family>/`` holds the canonical, version-controlled,
CI-self-tested implementations of the fleet's guard hooks. Until this module
existed they reached no agent: the ``to_home`` cascade's lowest layer is the
operator's dotfiles tree (``_shared/to_home/.claude/hooks/pre-tool-use/``),
which is a HAND-MAINTAINED COPY of these files. Merging a hook fix into sac
therefore changed nothing until a human remembered to re-copy it, and nothing
anywhere reported that they had not.

Measured on this host the day the module was written: the dotfiles copy of
``enforce_telegram_use_lists.sh`` was 601 bytes behind the repo's, all three
``hpc_login_hooks`` files were behind, and three whole families
(``claude_worktree_hooks``, ``git_identity_hooks``, ``heavy_job_hooks``) had
never been copied at all. Every one of those is a merged change that armed
nothing.

The fix is not a new mechanism — :func:`.._to_home.deploy_to_home` already
runs on EVERY start. This module adds the missing layer to it: the packaged
assets, deployed on top of the stale mirror, so a merged fix reaches agents at
their next restart with no copy step.

Why the packaged layer WINS over the dotfiles layer
---------------------------------------------------
The dotfiles copy is definitionally a mirror of this directory, not an
override surface — its own README calls these files "canonical". A layer that
lost to its own stale mirror would leave the defect exactly as it was. An
operator who genuinely wants different behaviour changes the asset here (where
CI runs its self-test) or ships a DIFFERENTLY-NAMED hook.

Nothing is destroyed to make that true: a differing file is displaced to
``<hooks_dir>/.old/<timestamp>/`` before it is replaced, so a hand-edit that
turns out to have been deliberate is recoverable.

Safety posture — this code sits upstream of the operator's only channel
---------------------------------------------------------------------
A hook guards ``mcp__claude-code-telegrammer__reply``. An agent left with a
half-written or syntactically broken hook cannot send, and therefore cannot
report that it cannot send. So:

* **Atomic.** Content is written to a sibling temp file, ``chmod``-ed, then
  ``os.replace``-d onto the destination — an atomic rename within one
  directory. A failure to write, or a full disk, aborts BEFORE the
  destination is touched; the old hook stays in place and keeps running.
* **Validated before it lands.** A shell asset that fails ``bash -n`` is
  refused, not deployed. Validation runs ONLY for a file whose content
  actually changed, so the steady state (everything already current) spawns
  no subprocesses at all.
* **Fail-open.** Every failure mode is caught per-file and reported; the
  deploy never raises, so it can never be the reason an agent fails to start.
  A missing new hook is a smaller harm than an agent that cannot boot.
* **Loud once.** Failures are logged at ERROR naming the file and the
  consequence; successful changes at INFO. Steady state is silent.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ._hook_exec_bit import HOOK_MODE as _HOOK_MODE
from ._hook_exec_bit import is_executable as _is_executable

logger = logging.getLogger(__name__)

# Root of the deployed hook tree, relative to the agent's $HOME.
HOOKS_ROOT = Path(".claude") / "hooks"

# Where most hooks live. Not universal: ``claude_worktree_hooks`` arms its
# scripts from ``.claude/hooks/claude_worktree_hooks/`` instead, which is why
# the destination is READ from each family's fragment rather than assumed.
DEFAULT_HOOK_SUBDIR = "pre-tool-use"

# Back-compat alias for the common case.
HOOKS_RELPATH = HOOKS_ROOT / DEFAULT_HOOK_SUBDIR

# The file that ARMS a family's hooks; also the authority on where they live.
FRAGMENT_NAME = "settings.local.json.fragment.json"

# ``.claude/hooks/<subdir>/<file>`` inside a fragment's hook command. Matched
# against the fragment's RAW TEXT on purpose: the fragments use at least two
# different JSON shapes (a bare ``"PreToolUse": [...]`` and a nested
# ``"hooks": {"WorktreeCreate": [...]}``), and a command may be prefixed by an
# interpreter (``python3 $HOME/.claude/hooks/...``). A regex over the text is
# schema-agnostic, so a third shape cannot silently route a hook to the wrong
# directory the way a hard-coded parser would.
_HOOK_PATH_RE = re.compile(r"\.claude/hooks/([^/\s\"]+)/([^/\s\"]+)")

# Only executable hook payloads are deployed. README.md and
# settings.local.json.fragment.json are documentation / registration templates
# that describe how to ARM these scripts; they are not hooks and must not be
# dropped into the hooks directory.
_DEPLOYABLE_SUFFIXES = frozenset({".sh", ".py"})

# Displaced copies of replaced files land here (never deleted).
_DISPLACED_DIRNAME = ".old"

# The deployed-hook mode and the exec-bit predicate are owned by
# :mod:`._hook_exec_bit` (imported above) — that module is the single place
# that reasons about why a hook needs the bit at all.

_BASH_SYNTAX_TIMEOUT_S = 10.0


def baseline_assets_dir() -> Path:
    """Absolute path of the packaged ``_baseline_assets`` tree.

    Resolved relative to this module so it works identically for an editable
    checkout and an installed wheel (sac ships the tree as package data under
    ``packages = ["src/scitex_agent_container"]``).
    """
    return Path(__file__).resolve().parent.parent / "_baseline_assets"


def iter_packaged_hook_assets(assets_dir: "Path | None" = None) -> "list[Path]":
    """Every deployable hook script sac ships — the sources only.

    The "what do we ship?" question, for quality checks and reporting. Use
    :func:`hook_asset_plan` when you also need to know WHERE each one goes;
    destinations are per-family and are not all ``pre-tool-use``.

    ``assets_dir`` overrides the packaged tree. It exists so a caller — a test,
    or an operator staging a candidate bundle — can point the SAME code at a
    different real directory, rather than the source being an unreachable
    module-level constant that can only be faked by patching.
    """
    return [src for src, _ in hook_asset_plan(assets_dir)]


def fragment_hook_paths(family: Path) -> "dict[str, str]":
    """``{script basename: hooks subdir}`` as declared by ``family``'s fragment.

    Empty when the family ships no fragment or none of its commands name a
    path under ``.claude/hooks/``.
    """
    fragment = family / FRAGMENT_NAME
    if not fragment.is_file():
        return {}
    try:
        text = fragment.read_text()
    except OSError as exc:  # stx-allow: fallback (an unreadable fragment must not sink the deploy)
        logger.warning("baseline hook assets: cannot read %s: %s", fragment, exc)
        return {}
    return {name: subdir for subdir, name in _HOOK_PATH_RE.findall(text)}


def _family_subdir(family: Path, declared: "dict[str, str]") -> str:
    """The subdir for assets this family ships but does not NAME in its fragment.

    Core/policy ``.py`` helpers are imported by their wrapper from alongside
    it, so they are never armed directly and never appear in the fragment —
    but they must land in the same directory as the wrapper that imports them,
    or the wrapper resolves nothing. The family's declared destination is
    therefore the right default; ``pre-tool-use`` is the fallback.
    """
    if not declared:
        return DEFAULT_HOOK_SUBDIR
    return Counter(declared.values()).most_common(1)[0][0]


def hook_asset_plan(assets_dir: "Path | None" = None) -> "list[tuple[Path, str]]":
    """``[(source asset, destination subdir under .claude/hooks/)]``, sorted.

    The destination comes from the family's own settings fragment — the same
    file that arms the hook — so a script can never be deployed to a directory
    the registration is not pointing at. Families are an authoring convenience
    and do NOT imply a destination: four of them arm from ``pre-tool-use`` and
    ``claude_worktree_hooks`` arms from its own subdir.

    A basename shipped twice for the SAME destination would make deploy order
    significant; it is refused loudly (logged, both dropped) rather than
    resolved by luck.
    """
    root = assets_dir if assets_dir is not None else baseline_assets_dir()
    if not root.is_dir():
        logger.error(
            "baseline hook assets: packaged asset dir %s is missing — no hooks "
            "will be deployed. sac's install is incomplete.",
            root,
        )
        return []
    by_dest: dict[tuple[str, str], list[Path]] = {}
    for family in sorted(p for p in root.iterdir() if p.is_dir()):
        declared = fragment_hook_paths(family)
        fallback = _family_subdir(family, declared)
        for asset in sorted(family.iterdir()):
            if not asset.is_file() or asset.suffix not in _DEPLOYABLE_SUFFIXES:
                continue
            subdir = declared.get(asset.name, fallback)
            by_dest.setdefault((subdir, asset.name), []).append(asset)
    out: list[tuple[Path, str]] = []
    for (subdir, name), paths in sorted(by_dest.items()):
        if len(paths) > 1:
            logger.error(
                "baseline hook assets: %r is shipped for %s by %d families (%s) "
                "— refusing to deploy an ambiguous hook; rename one.",
                name,
                subdir,
                len(paths),
                ", ".join(p.parent.name for p in paths),
            )
            continue
        out.append((paths[0], subdir))
    return out


def _bash_syntax_ok(src: Path) -> bool:
    """True iff ``src`` is not a shell script, or is one that parses.

    Guards the send path: a syntactically broken hook is refused before it can
    replace a working one. Any failure to RUN the check (no bash, timeout) is
    treated as "cannot disprove" and lets the deploy proceed — the check exists
    to catch a bad asset, not to become a new way for deployment to stall.
    """
    if src.suffix != ".sh":
        return True
    try:
        proc = subprocess.run(
            ["bash", "-n", str(src)],
            capture_output=True,
            timeout=_BASH_SYNTAX_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):  # stx-allow: fallback (checker unavailable -> do not block)
        logger.debug("baseline hook assets: bash -n unavailable for %s", src)
        return True
    if proc.returncode == 0:
        return True
    logger.error(
        "baseline hook assets: REFUSING to deploy %s — it does not parse "
        "(bash -n rc=%d): %s. The previously deployed copy is left untouched.",
        src.name,
        proc.returncode,
        proc.stderr.decode("utf-8", "replace").strip(),
    )
    return False


def _already_displaced(attic_root: Path, name: str, payload: "bytes | None") -> bool:
    """True iff an identical copy of ``name`` is already in the attic.

    Bounds the attic, and it is not a theoretical concern: the ``to_home`` walk
    re-copies the operator's dotfiles version on EVERY start, so this module
    sees the same stale bytes and replaces them again on every start. Without
    this check each restart would deposit another identical copy, and a
    long-lived agent would grow an unbounded ``.old/`` tree — a slow disk leak
    inside the hooks directory, which is the last place one should be hidden.

    Distinct versions are still all preserved; only exact duplicates are
    skipped. ``payload`` is ``None`` for a symlink, which is compared by target.
    """
    if not attic_root.is_dir():
        return False
    for prior in attic_root.glob(f"*/{name}"):
        try:
            if payload is None:
                if prior.is_symlink() and os.readlink(prior) == os.readlink(
                    attic_root.parent / name
                ):
                    return True
            elif not prior.is_symlink() and prior.read_bytes() == payload:
                return True
        except OSError:  # stx-allow: fallback (an unreadable prior copy just means "not a match")
            continue
    return False


def _displace(dst: Path, stamp: str) -> None:
    """Move ``dst`` aside to ``<hooks_dir>/.old/<stamp>/`` before replacement.

    Nothing this module replaces is deleted. A symlink is recorded by copying
    the LINK (not its content) — the target is a host file that still exists,
    so there is nothing to preserve but the pointer. An exact duplicate of a
    copy already in the attic is skipped; see :func:`_already_displaced`.
    """
    if not (dst.exists() or dst.is_symlink()):
        return
    attic_root = dst.parent / _DISPLACED_DIRNAME
    payload = None if dst.is_symlink() else dst.read_bytes()
    if _already_displaced(attic_root, dst.name, payload):
        return
    # The directory carries the content digest as well as the timestamp. The
    # stamp alone is per-SECOND, so two DISTINCT versions displaced inside the
    # same second would land on the same path and the second copy2 would
    # overwrite the first — silently destroying the older one, which is exactly
    # what "nothing is deleted" forbids. Caught by
    # test_a_genuinely_different_prior_version_is_still_kept.
    digest = _digest(dst, payload)
    attic = attic_root / f"{stamp}-{digest}"
    attic.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dst, attic / dst.name, follow_symlinks=False)


def _digest(dst: Path, payload: "bytes | None") -> str:
    """Short content digest of ``dst`` — of the link TARGET for a symlink."""
    raw = os.readlink(dst).encode() if payload is None else payload
    return hashlib.sha256(raw).hexdigest()[:12]


def _deploy_one(src: Path, dst_dir: Path, stamp: str) -> str:
    """Deploy one asset. Returns ``unchanged`` / ``deployed`` / ``failed``.

    Atomic: writes a sibling temp file, sets the mode on it, then renames it
    over the destination. The destination is never observed partially written,
    and an aborted write leaves the running hook in place.

    CONTENT IS NOT THE WHOLE STATE. ``settings.json`` arms these hooks by bare
    path, with no interpreter prefix::

        "command": "$HOME/.claude/hooks/pre-tool-use/enforce_telegram_no_bare_issue.sh"

    so a hook without the execute bit cannot run AT ALL — and its bytes are
    perfect, so every content comparison calls it current. That is the same
    inert-hook failure this module exists to close, arriving by a second route:
    the first was the copy nobody updated, this is the copy nobody could
    execute. Measured in the dotfiles baseline the same day: the live
    ``~/.claude`` copy was 100755 while the ``_shared/to_home/`` SOURCE it is
    materialised from was 100644.

    So "already correct" requires BOTH the bytes and the bit. A byte-identical
    file that merely lost its mode is repaired with a chmod — no rewrite, no
    displacement, because nothing is being replaced.
    """
    dst = dst_dir / src.name
    payload = src.read_bytes()
    if dst.is_file() and not dst.is_symlink() and dst.read_bytes() == payload:
        if _is_executable(dst):
            return "unchanged"
        os.chmod(dst, _HOOK_MODE)
        logger.warning(
            "baseline hook assets: %s had correct content but was NOT "
            "executable (%s arms it by bare path, so it could not run at "
            "all) — mode repaired to %o.",
            dst,
            "settings.json",
            _HOOK_MODE,
        )
        return "deployed"
    if not _bash_syntax_ok(src):
        return "failed"
    tmp = dst_dir / f".{src.name}.sac-deploy-{os.getpid()}"
    try:
        tmp.write_bytes(payload)
        os.chmod(tmp, _HOOK_MODE)
        _displace(dst, stamp)
        os.replace(tmp, dst)
    finally:
        # A failure anywhere above must not leave a temp file loitering in the
        # hooks dir, where a future reader could mistake it for a hook.
        if tmp.exists():
            tmp.unlink()
    return "deployed"


def deploy_baseline_hook_assets(
    workspace_home: "Path | str", *, assets_dir: "Path | None" = None
) -> "dict[str, list[str]]":
    """Deploy every packaged hook asset into ``<workspace_home>``'s hooks dir.

    Idempotent — an already-current file is compared and skipped, so a steady
    state costs one read per asset and writes nothing.

    ``assets_dir`` overrides the packaged source tree (see
    :func:`iter_packaged_hook_assets`).

    NEVER RAISES. Returns ``{"deployed": [...], "unchanged": [...],
    "failed": [...]}``; callers may log it but must not gate the start on it.
    See the module docstring for why fail-open is the correct posture here.
    """
    result: dict[str, list[str]] = {"deployed": [], "unchanged": [], "failed": []}
    home = Path(workspace_home)
    try:
        plan = hook_asset_plan(assets_dir)
        if not plan:
            return result
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for src, subdir in plan:
            dst_dir = home / HOOKS_ROOT / subdir
            try:
                dst_dir.mkdir(parents=True, exist_ok=True)
                result[_deploy_one(src, dst_dir, stamp)].append(src.name)
            except OSError as exc:  # stx-allow: fallback (one bad file must not sink the rest)
                result["failed"].append(src.name)
                logger.error(
                    "baseline hook assets: FAILED to deploy %s -> %s: %s. The "
                    "previously deployed copy (if any) is unchanged and still "
                    "runs; this hook is now STALE on this agent.",
                    src.name,
                    dst_dir,
                    exc,
                )
    except Exception as exc:  # stx-allow: fallback (deploy must never block a start)
        logger.error(
            "baseline hook assets: deployment aborted for %s: %s. The agent "
            "starts anyway with its previously deployed hooks; merged hook "
            "changes have NOT landed on it.",
            workspace_home,
            exc,
        )
        return result

    if result["deployed"]:
        logger.info(
            "baseline hook assets: updated %d hook(s) under %s (%s)",
            len(result["deployed"]),
            home / HOOKS_ROOT,
            ", ".join(result["deployed"]),
        )
    return result


__all__ = [
    "DEFAULT_HOOK_SUBDIR",
    "FRAGMENT_NAME",
    "HOOKS_RELPATH",
    "HOOKS_ROOT",
    "baseline_assets_dir",
    "deploy_baseline_hook_assets",
    "fragment_hook_paths",
    "hook_asset_plan",
    "iter_packaged_hook_assets",
]
