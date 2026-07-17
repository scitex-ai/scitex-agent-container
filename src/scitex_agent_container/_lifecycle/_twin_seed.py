"""Twin host-side SEED — the boot-time half of twin spawning.

Runs on the HOST from :func:`_lifecycle._start.agent_start`, right after
``seed_pinned_session_id`` and BEFORE ``runtime.start``: it gates the twin's
identity, then on FIRST boot points the twin's launch at the parent's current
session with ``--fork-session`` and copies that transcript into the twin's
container home. Host-side ⇒ every runtime path resolves on the bare host
whether ``sac agents twin`` ran on the host or was brokered from inside a
container, and the twin inherits the FRESHEST transcript rather than one
captured at command time.

Triggered solely by ``SAC_TWIN_PARENT`` in the twin's own env — a strict
no-op for every non-twin start. The command-time half is the sibling
:mod:`._twin_derive`. Full design: docs/adr/0019 (+ its 2026-07-17 amendment).
"""

from __future__ import annotations

import logging
from typing import Any

from ._twin_identity import (
    TWIN_PARENT_ENV,
    TwinSeedError,
    assert_twin_identity,
    twin_session_uuid,
)

logger = logging.getLogger(__name__)

__all__ = ["seed_twin_from_parent"]


def _resolve_state_dir(config: Any, runtime: Any):
    """Per-agent state dir, project-scope aware, for BOTH runtime kinds.

    Prefers the runtime's own ``_state_dir`` (the SDK runtime + the test stub
    expose one), else ``tui_session.state_dir_for_config`` (what the ``tui``
    runtime + its resume home-check use). Both honour a project-scope runtime
    root, so the seeded marker + copied transcript land where the runner
    reads them, regardless of runtime kind.
    """
    resolver = getattr(runtime, "_state_dir", None)
    if callable(resolver):
        return resolver(config)
    from ..runtimes.tui_session import state_dir_for_config

    return state_dir_for_config(config)


def _container_home_dir(config: Any, state_dir, *, existing: bool):
    """Host dir backing the container ``$HOME`` for ``config``.

    Mirrors :func:`runtimes._apptainer_inner_argv_tui._home_has_resumable_
    conversation`: a relaxed-directory-overlay agent's home is the overlay
    upper home; every other agent's is the workspace-home bind
    ``<state_dir>/home`` (``state_dir`` is the SAME per-agent state dir the
    session_id marker lives under, so home + marker never diverge).

    ``existing=True`` (parent source) only accepts the overlay upper home
    when it is already materialised on disk; ``existing=False`` (twin
    destination — not built yet at seed time) accepts a DECLARED overlay
    upper home even before it exists, so the copied transcript lands where
    the container ``$HOME`` will actually be.
    """
    from pathlib import Path

    from ..runtimes._to_home_overlay import resolve_overlay_upper_home

    upper = resolve_overlay_upper_home(config)
    if upper is not None and (not existing or upper.is_dir()):
        return upper
    return Path(state_dir) / "home"


def seed_twin_from_parent(config: Any, runtime: Any) -> bool:
    """HOST-SIDE: fork the twin's first session off the parent's live one.

    A strict no-op (returns ``False``) unless ``config.env`` carries
    :data:`TWIN_PARENT_ENV` — i.e. only for a twin. For a twin it:

      0. GATES THE IDENTITY (:func:`assert_twin_identity`) — on EVERY boot,
         before anything else, because the transcript we are about to hand it
         insists it is the parent;
      1. resolves the parent's config + CURRENT session uuid (the possibly
         forked live id in ``<parent-state>/session_id``);
      2. copies the parent's ``<uuid>.jsonl`` transcript into the twin's
         container-home projects store, MIRRORING the parent's project
         subdir name (parent and twin share the workdir, so claude's cwd
         encoding matches — mirroring the on-disk subdir avoids recomputing
         the encoding and any host/container ``realpath`` skew). This copy is
         NOT optional and is NOT what ``--fork-session`` replaces: parent and
         twin have SEPARATE container homes, and ``--resume <uuid>`` resolves
         the transcript from the LOCAL ``~/.claude/projects``. The copy is the
         cross-home transport; the fork is the id handling.
      3. points this ONE boot at ``claude --resume <parent-uuid>
         --fork-session --session-id <derived>`` (SDK: the same three as
         ``ClaudeAgentOptions``) by overriding the in-memory config, which
         ``_start.agent_start`` hands straight to ``runtime.start``.

    WHY THE FORK FLAGS (operator, 2026-07-17: 「claude code にオフィシャルで
    fork オプションがありますね」): the twin must inherit the parent's
    conversation but write to a session of its OWN from turn one. Reusing the
    parent's uuid as the twin's live session id — as this seed used to — is
    hand-rolled state surgery standing in for a supported, documented
    mechanism. ``--fork-session`` ("When resuming, create a new session ID
    instead of reusing the original") is exactly that mechanism, and
    ``--session-id`` pins the fork to :func:`twin_session_uuid` so the twin's
    session is derivable from its name instead of discovered.

    The ``session_id`` MARKER is still seeded to the PARENT's uuid, and that
    is correct rather than leftover: it is the in-container SDK runner's only
    channel for "which session to resume FROM" on this first boot. The fork
    then advances it to the twin's own id after turn one (see
    ``_session_seed``), so the parent's uuid is transient, never the twin's
    identity.

    FIRST-BOOT ONLY (steps 1-3): if the twin already has a ``session_id``
    marker it has booted, so this returns early WITHOUT re-seeding — later
    restarts ``continue`` the twin's own diverged session, and a persistent
    twin keeps starting even after its parent has stopped. Re-forking on every
    restart would discard the twin's history. The identity gate (step 0) is
    the exception: it runs on every boot.

    Called from :func:`_lifecycle._start.agent_start` before
    ``runtime.start``. Fail-loud (:class:`TwinSeedError`) on the first boot
    when the parent spec is unresolvable, the parent has no live session, or
    its transcript is missing — a twin with no inherited context is pointless,
    so we abort the start rather than boot an empty session.

    Returns ``True`` iff twin seeding ran.
    """
    env = getattr(config, "env", None) or {}
    parent_name = str(env.get(TWIN_PARENT_ENV, "") or "").strip()
    if not parent_name:
        return False

    # EVERY boot, and FIRST: an inherited transcript claims the parent's
    # identity for the twin's whole life, not just its first turn.
    assert_twin_identity(config)

    from .._runners._session_state import read_session_id, write_session_id

    # First-boot ONLY. Once the twin has its OWN session marker it has already
    # booted and diverged; re-seeding would discard that history (and re-fork
    # from the parent) on every restart. ``continue`` then resumes the twin's
    # own latest session — and this early-return also lets a persistent twin
    # keep starting even after its parent has stopped.
    twin_state = _resolve_state_dir(config, runtime)
    if read_session_id(twin_state) is not None:
        return False

    from ..config import load_config, resolve_config

    try:
        parent_config = load_config(resolve_config(parent_name))
    except Exception as exc:  # noqa: BLE001 - re-raised as fail-loud TwinSeedError below
        raise TwinSeedError(
            f"twin {getattr(config, 'name', '?')!r}: cannot resolve parent "
            f"{parent_name!r} spec ({exc}); refusing to boot a twin whose "
            "parent is unknown."
        ) from exc

    # BOTH the seeded marker and the copied transcript derive from the SAME
    # per-agent state dir (``_resolve_state_dir`` — project-scope aware, and
    # the resolver the SDK runner reads its marker from / the TUI home-check
    # uses), so marker and transcript never land under divergent roots.
    parent_state = _resolve_state_dir(parent_config, runtime)
    parent_uuid = read_session_id(parent_state)
    if not parent_uuid:
        raise TwinSeedError(
            f"twin {getattr(config, 'name', '?')!r}: parent {parent_name!r} has "
            "no resolvable live session id (is it running and past its first "
            "turn?). Refusing to boot a twin with no context to inherit."
        )

    parent_home = _container_home_dir(parent_config, parent_state, existing=True)
    src = _find_transcript(parent_home, parent_uuid)
    if src is None:
        raise TwinSeedError(
            f"twin {getattr(config, 'name', '?')!r}: parent {parent_name!r} "
            f"session {parent_uuid} has no transcript under "
            f"{parent_home}/.claude/projects/. Refusing to boot a twin with no "
            "context to inherit."
        )

    twin_home = _container_home_dir(config, twin_state, existing=False)
    _copy_transcript(src, twin_home, parent_uuid)
    # Seed the marker to the parent's uuid — the in-container SDK runner's
    # only "resume FROM" channel on this first boot. The fork advances it to
    # the twin's own id after turn one, so this value is transient.
    write_session_id(twin_state, parent_uuid)
    forked_uuid = _apply_fork_launch(config, parent_uuid)

    logger.info(
        "twin %s: forked session %s from parent %s's session %s "
        "(transcript %s, --fork-session --session-id)",
        getattr(config, "name", "?"),
        forked_uuid,
        parent_name,
        parent_uuid,
        src,
    )
    return True


def _apply_fork_launch(config: Any, parent_uuid: str) -> str:
    """Point this ONE boot at ``--resume <parent> --fork-session --session-id``.

    Overrides the IN-MEMORY config (never the on-disk spec): ``agent_start``
    calls us with the very object it then hands to ``runtime.start``, so the
    argv builders see these values, and nothing persists to disk where it
    would wrongly re-fork on the next restart.

    Sets ``session: resume`` + ``resume_id`` rather than leaving the spec's
    ``continue``: ``-c`` means "the latest session for this cwd", which is
    only the parent's transcript by luck of it being the twin's sole one.
    Naming the uuid is exact, and it is what ``--fork-session`` needs to fork
    FROM. On later boots this seed early-returns, the on-disk ``continue``
    stands, and the twin resumes its own diverged session.

    Returns the twin's derived (forked-into) session uuid.
    """
    # Local import: this module reaches ``..config`` lazily throughout to keep
    # the lifecycle→config edge out of import time.
    from ..config._session_continuity import SESSION_RESUME

    claude = getattr(config, "claude", None)
    if claude is None:
        raise TwinSeedError(
            f"twin {getattr(config, 'name', '?')!r}: config has no 'claude' "
            "block to point at the parent's session; cannot fork."
        )
    forked_uuid = twin_session_uuid(str(getattr(config, "name", "") or ""))
    claude.session = SESSION_RESUME
    claude.resume_id = parent_uuid
    claude.fork_session = True
    claude.session_id = forked_uuid
    return forked_uuid


def _find_transcript(home, uuid: str):
    """Return the ``<uuid>.jsonl`` transcript under ``home/.claude/projects``.

    Globs ``projects/*/<uuid>.jsonl`` so the parent's cwd-encoded project
    subdir is discovered from disk rather than recomputed. Returns the
    :class:`pathlib.Path` or ``None`` when absent.
    """
    from pathlib import Path

    projects = Path(home) / ".claude" / "projects"
    if not projects.is_dir():
        return None
    matches = sorted(projects.glob(f"*/{uuid}.jsonl"))
    return matches[0] if matches else None


def _copy_transcript(src, twin_home, uuid: str) -> None:
    """Copy ``src`` transcript into the twin home, mirroring its project subdir.

    ``src`` is ``<parent_home>/.claude/projects/<subdir>/<uuid>.jsonl``; the
    destination is ``<twin_home>/.claude/projects/<subdir>/<uuid>.jsonl`` —
    the SAME ``<subdir>``, because parent and twin share the workdir so
    claude encodes the same project dir name in-container.
    """
    import shutil
    from pathlib import Path

    subdir = src.parent.name
    dest = Path(twin_home) / ".claude" / "projects" / subdir / f"{uuid}.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
