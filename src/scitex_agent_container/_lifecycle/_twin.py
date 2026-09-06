"""Twin-agent derivation + host-side conversation-context inheritance.

A TWIN is a NEW agent spawned FROM a PARENT that INHERITS the parent's LIVE
conversation transcript at birth, then diverges. The parent never stops — a
twin is how an agent splits off context-carrying work without pausing its
own loop. Lifetime is independent of the primitive: an ephemeral triage
twin (``restart.policy: never``) and a persistent companion
(``restart.policy: always``) are the SAME mechanism, different lifetime.
Full design: docs/adr/0019 + the ``33_twin-spawning`` skill.

Two halves live here:

* :func:`derive_twin_spec` / :func:`resolve_twin_name` — PURE spec-doc
  transforms the ``sac agents twin`` CLI + the ``agent_twin`` MCP tool run
  BEFORE the spawn POST: parent spec verbatim (repo/workdir/image/binds/
  model) with the name, ``session: continue``, lifetime, a fresh
  ``a2a.port``, and the IDENTITY-SPLIT env overridden.
* :func:`seed_twin_from_parent` — the HOST-SIDE pre-start step (from
  ``_start.agent_start``, right after ``seed_pinned_session_id``): on FIRST
  boot it seeds the twin's session marker from the parent's current uuid +
  copies that transcript in, so the twin's ``continue`` resumes it. Host-
  side ⇒ paths resolve on the bare host whether ``twin`` ran on the host or
  brokered from a container. Triggered by ``SAC_TWIN_PARENT`` in the twin's
  own env — a strict no-op for every non-twin start.

IDENTITY SPLIT — safety-critical:
  * author = the TWIN — ``SCITEX_CARDS_AGENT_ID = <twin>`` in its env block,
    so scitex-cards writes attribute to the twin (the operator's ask).
  * owner = the PARENT — but scitex-cards has NO env knob for the default
    card owner (``add_task`` fails loud without an explicit ``assignee``;
    ``SCITEX_CARDS_AGENT_ID`` feeds ONLY the author path — verified against
    the card store). So owner=parent CANNOT be enforced from env; it is
    a HARD CONVENTION — the twin passes ``assignee=<parent>`` (==
    ``$SAC_TWIN_PARENT``, injected here) on EVERY card write. WHY: an
    ephemeral twin that owns cards then exits orphans them (the drift
    incident that stranded 75 cards). The boot-kick + skill state the rule.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Env var carrying the parent's name into the twin's container. Presence
# marks a spec as a twin (the sole trigger for :func:`seed_twin_from_parent`)
# AND is the value the twin passes as ``assignee=`` to keep card ownership
# with the parent.
TWIN_PARENT_ENV = "SAC_TWIN_PARENT"
# scitex-cards author-identity var — set to the TWIN so writes attribute to it.
# CANONICAL name only: the retired ``SCITEX_TODO_AGENT_ID`` must never be
# written into a spec sac generates (a spec that declares it is what keeps the
# legacy alias alive).
CARDS_AGENT_ENV = "SCITEX_CARDS_AGENT_ID"
# Retired predecessor of :data:`CARDS_AGENT_ENV`. DROPPED from an inherited
# twin env for the same reason ``SAC_NAME`` is: the parent's copy carries the
# PARENT's name, so leaving it would hand any consumer still reading the old
# name the wrong author — and would re-create a legacy-declaring spec on every
# twin spawn, which is what keeps the alias alive.
RETIRED_AGENT_ENV = "SCITEX_TODO_AGENT_ID"
# sac self-name var — owned by ``listen_env_flags`` (injected from config.name),
# so we must not let an inherited spec.env copy shadow it with the parent's.
SELF_NAME_ENV = "SAC_NAME"
# Dropped from an inherited twin so two agents don't fight one Telegram bot's
# getUpdates long-poll slot (HTTP 409). The twin stays reachable via the a2a
# bus (``server:sac``) instead.
_TELEGRAMMER_CHANNEL = "server:claude-code-telegrammer"

__all__ = [
    "TWIN_PARENT_ENV",
    "TwinSeedError",
    "build_twin_boot_kick",
    "derive_twin_spec",
    "prepare_twin_spawn",
    "resolve_twin_name",
    "seed_twin_from_parent",
]


class TwinSeedError(RuntimeError):
    """Raised when host-side twin context-inheritance cannot complete.

    Fail-loud contract: a twin whose parent has no resolvable live session,
    or whose parent transcript is missing on disk, must NOT boot into an
    empty session pretending to have inherited the parent's context — that
    would silently defeat the entire point of a twin. The twin start aborts
    with this error instead.
    """


def resolve_twin_name(
    parent_name: str,
    requested: str | None,
    existing: Iterable[str] | None,
) -> str:
    """Return the twin's agent name.

    An explicit ``requested`` name is returned verbatim (the caller decides
    whether a clash is an error). Otherwise the default ``<parent>-twin`` is
    used, bumped to ``<parent>-twin-2`` / ``-3`` / ... on the first free
    suffix so a parent can carry several live twins at once.
    """
    if requested:
        return requested
    taken = {str(n) for n in (existing or ())}
    base = f"{parent_name}-twin"
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def build_twin_boot_kick(
    twin_name: str,
    parent_name: str,
    task: str | None,
) -> str:
    """First user message fed to the twin AFTER it resumes the parent's session.

    Carries the two things a freshly-forked twin must know: that it has
    inherited context and diverged, and the IDENTITY-SPLIT rule (author =
    twin, owner = parent). The ownership rule is stated HERE — not only in
    the skill — because scitex-todo cannot enforce owner=parent from env,
    so the boot-kick is the deterministic delivery of the hard rule.
    """
    lines = [
        f"You are {twin_name}, a TWIN forked from {parent_name}'s live session.",
        f"You have INHERITED {parent_name}'s conversation context up to this "
        "moment and now DIVERGE — your future turns are yours alone and are "
        f"NOT shared back to {parent_name}.",
        "",
        "IDENTITY CONTRACT (hard rule — do not deviate):",
        f"  - Your scitex-todo writes are attributed to YOU ({twin_name}); that "
        "is intended.",
        f"  - But card OWNERSHIP must stay with {parent_name}. On EVERY "
        f"add_task / reassign, pass assignee={parent_name} (also available as "
        f"$SAC_TWIN_PARENT). NEVER leave a card owned by {twin_name}: if you "
        "exit, a card you own lands in an inbox nobody drains.",
        f"  - Coordinate results back to {parent_name} via a2a / a shared card "
        f"owned by {parent_name}, not by holding state only you can see.",
    ]
    if task and task.strip():
        lines += ["", f"Your task: {task.strip()}"]
    else:
        lines += [
            "",
            "No task was supplied at spawn — confirm you have inherited context "
            f"and stand by for {parent_name}'s direction.",
        ]
    return "\n".join(lines)


def derive_twin_spec(
    parent_doc: dict[str, Any],
    *,
    twin_name: str,
    parent_name: str,
    persist: bool,
    role: str | None = None,
    task: str | None = None,
    to_home: str | None = None,
) -> dict[str, Any]:
    """Return the twin's inline spec document derived from the parent's.

    Pure — deep-copies ``parent_doc`` and overrides only what a twin must
    change, inheriting repo / workdir / image / binds / model verbatim:

      * ``spec.claude.session = "continue"`` and ``resume_id`` cleared — the
        HOST seeds the twin's session marker from the parent's CURRENT uuid
        + copies that transcript at FIRST start (:func:`seed_twin_from_parent`),
        so it inherits the freshest context; on later restarts ``continue``
        resumes the twin's OWN diverged session (not a re-fork of the parent).
      * ``spec.env`` — ``SCITEX_CARDS_AGENT_ID = <twin>`` (author = twin),
        ``SAC_TWIN_PARENT = <parent>`` (owner-convention value + twin
        trigger); any inherited ``SAC_NAME`` is dropped (``listen_env_flags``
        injects it from the twin's own name), as is any inherited
        ``SCITEX_TODO_AGENT_ID`` (retired, and carrying the PARENT's name).
      * ``spec.restart.policy`` — ``always`` when ``persist`` else ``never``
        (ephemeral default: a stopped twin does not come back).
      * ``spec.a2a.port = "auto"`` — a fresh sidecar port, never the
        parent's (a pinned inherited port would collide).
      * telegrammer channel dropped — two agents must not fight one bot's
        getUpdates slot; the twin stays reachable via ``server:sac``.
      * ``spec.startup_prompts`` — replaced with the twin boot-kick (the
        identity contract + optional ``task``).
      * ``metadata.labels.role`` — set when ``role`` is given.

    The twin's NAME is NOT written into the document: the host materialises
    the inline spec at ``agents/<twin_name>/spec.yaml`` and the loader
    derives the name from that directory (dir-as-SSoT).
    """
    doc = copy.deepcopy(parent_doc)
    if not isinstance(doc, dict):
        raise TwinSeedError(
            f"parent spec of {parent_name!r} did not parse to a mapping "
            f"(got {type(parent_doc).__name__!r}); cannot derive a twin."
        )
    spec = doc.setdefault("spec", {})
    if not isinstance(spec, dict):
        raise TwinSeedError(
            f"parent spec of {parent_name!r} has a non-mapping 'spec' block; "
            "cannot derive a twin."
        )

    claude = spec.setdefault("claude", {})
    if isinstance(claude, dict):
        # ``continue`` (not ``resume``): the host seeds the twin's session_id
        # marker from the parent's live uuid on FIRST boot and copies that
        # transcript in, so ``continue`` resumes it. On every SUBSEQUENT boot
        # ``continue`` resumes the twin's OWN (now-diverged) latest session —
        # a pinned ``resume``/``resume_id`` would instead re-fork from the
        # parent each restart, discarding the twin's history.
        claude["session"] = "continue"
        claude["resume_id"] = ""
        channels = claude.get("channels")
        if isinstance(channels, list):
            claude["channels"] = [
                c for c in channels if str(c).strip() != _TELEGRAMMER_CHANNEL
            ]

    env = spec.setdefault("env", {})
    if isinstance(env, dict):
        env[CARDS_AGENT_ENV] = twin_name
        env[TWIN_PARENT_ENV] = parent_name
        env.pop(SELF_NAME_ENV, None)
        env.pop(RETIRED_AGENT_ENV, None)

    restart = spec.setdefault("restart", {})
    if isinstance(restart, dict):
        restart["policy"] = "always" if persist else "never"

    a2a = spec.setdefault("a2a", {})
    if isinstance(a2a, dict):
        a2a["port"] = "auto"

    # Reuse the PARENT's to_home tree (skills / hooks / .mcp.json) verbatim
    # via its absolute host path, so the twin has the SAME capabilities and
    # MCP wiring as the parent. Per-agent identity in those files is
    # runtime-only (``${SCITEX_CARDS_AGENT_ID}`` etc.) and expands from the
    # twin's OWN container env at boot, so sharing the tree is correct — the
    # author still resolves to the twin. Left unset (parent default) when the
    # caller could not resolve the parent's to_home dir.
    if to_home:
        spec["to_home"] = to_home

    if role:
        metadata = doc.setdefault("metadata", {})
        if isinstance(metadata, dict):
            labels = metadata.setdefault("labels", {})
            if isinstance(labels, dict):
                labels["role"] = role

    spec["startup_prompts"] = [build_twin_boot_kick(twin_name, parent_name, task)]
    return doc


def _resolve_parent_to_home(parent_path: str, parent_doc: dict) -> str | None:
    """Return the parent's to_home tree as an absolute host path, or None.

    Honours ``spec.to_home`` (default ``./to_home`` next to the spec),
    resolved relative to the parent spec dir. Returns the path only when it
    exists on disk — otherwise the twin keeps the default and materialises
    the shared baseline to_home.
    """
    from pathlib import Path

    spec = parent_doc.get("spec", {}) if isinstance(parent_doc, dict) else {}
    raw = (spec.get("to_home") if isinstance(spec, dict) else "") or "./to_home"
    p = Path(str(raw)).expanduser()
    if not p.is_absolute():
        p = Path(parent_path).parent / p
    return str(p) if p.is_dir() else None


def prepare_twin_spawn(
    parent_name: str,
    *,
    twin_name: str | None = None,
    task: str | None = None,
    persist: bool = False,
    role: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Resolve the parent spec + twin name and derive the twin's inline doc.

    The shared front-half of BOTH ``sac agents twin`` and the ``agent_twin``
    MCP tool: reads the parent's on-disk spec (raw YAML), resolves the twin
    name against the existing fleet (default-bumped), and returns
    ``(resolved_twin_name, twin_spec_doc)`` ready to POST via
    :func:`_spawn_client.request_spawn`.

    Fail-loud (:class:`TwinSeedError`) when the parent spec is unresolvable /
    malformed, or when an EXPLICIT ``twin_name`` is already taken (an
    operator-chosen name is never silently bumped).
    """
    import yaml

    from ..config import resolve_config
    from ..config._resolve import enumerate_agent_names

    try:
        parent_path = resolve_config(parent_name)
    except Exception as exc:  # noqa: BLE001 - re-raised as fail-loud TwinSeedError
        raise TwinSeedError(
            f"cannot resolve parent agent {parent_name!r}: {exc}"
        ) from exc
    with open(parent_path, encoding="utf-8") as fh:
        parent_doc = yaml.safe_load(fh)
    if not isinstance(parent_doc, dict):
        raise TwinSeedError(
            f"parent spec {parent_path!r} did not parse to a YAML mapping."
        )

    try:
        existing = enumerate_agent_names()
    except Exception:  # stx-allow: fallback (reason: name enumeration is best-effort; a 409 on spawn is the backstop)
        existing = []
    if twin_name and twin_name in set(existing):
        raise TwinSeedError(
            f"twin name {twin_name!r} is already taken; pick another name or "
            "omit it to auto-bump <parent>-twin-N."
        )
    resolved_name = resolve_twin_name(parent_name, twin_name, existing)

    to_home = _resolve_parent_to_home(parent_path, parent_doc)
    doc = derive_twin_spec(
        parent_doc,
        twin_name=resolved_name,
        parent_name=parent_name,
        persist=persist,
        role=role,
        task=task,
        to_home=to_home,
    )
    return resolved_name, doc


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
    """HOST-SIDE: pin the twin's resume to the parent's live session + copy it.

    A strict no-op (returns ``False``) unless ``config.env`` carries
    :data:`TWIN_PARENT_ENV` — i.e. only for a twin. For a twin it:

      1. resolves the parent's config + CURRENT session uuid (the possibly
         forked live id in ``<parent-state>/session_id``);
      2. seeds the twin's own ``session_id`` marker to that uuid so the
         twin's ``session: continue`` resumes it (SDK marker / TUI ``-c``);
      3. copies the parent's ``<uuid>.jsonl`` transcript into the twin's
         container-home projects store, MIRRORING the parent's project
         subdir name (parent and twin share the workdir, so claude's cwd
         encoding matches — mirroring the on-disk subdir avoids recomputing
         the encoding and any host/container ``realpath`` skew).

    FIRST-BOOT ONLY: if the twin already has its own ``session_id`` marker it
    has booted and diverged, so this returns early WITHOUT re-seeding — later
    restarts ``continue`` the twin's own latest session, and a persistent twin
    keeps starting even after its parent has stopped.

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
    # Seed the twin's marker to the parent's uuid: ``continue`` (SDK marker /
    # TUI ``-c``) resumes the copied transcript on this first boot.
    write_session_id(twin_state, parent_uuid)

    logger.info(
        "twin %s: inherited session %s from parent %s (transcript %s)",
        getattr(config, "name", "?"),
        parent_uuid,
        parent_name,
        src,
    )
    return True


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
