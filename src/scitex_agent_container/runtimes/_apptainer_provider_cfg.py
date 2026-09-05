"""The config dir a provider-backed engine points Claude Code at — seeded.

``provider_env_flags`` redirects the in-container ``claude`` to a per-agent
``CLAUDE_CONFIG_DIR`` (the last-wins conflict-breaker against the OAuth
bind). Claude Code reads its ``.claude.json`` from THAT dir, not from
``$HOME`` — so the onboarding seed sac writes into ``<home>/.claude.json``
(:mod:`.onboarding`) never reaches a provider agent, and the TUI runs its
first-run wizard. The wizard's login step ignores ``ANTHROPIC_API_KEY``:

    Browser didn't open? Use the url below to sign in ...
    The recommended sign-in isn't available on this machine
    (ANTHROPIC_API_KEY is set in this environment), so this sign-in
    will create an API key.  Paste code here if prompted >

Measured 2026-09-05 (business on scitex-compute-01, Claude Code 2.1.261):
the container carried the gateway URL, the model and the key, and parked
there until ``runtime.start()`` gave up. The one provider agent that ran
(handyman-01) had been signed in BY HAND once — its config dir held a
minted ``primaryApiKey`` and the env key sat in ``customApiKeyResponses.
rejected`` — and only worked because the proxy of the day checked no key.

This module makes the dir sac's own:

* it lives on the HOST at ``<state_dir>/provider-cfg`` and is bind-mounted
  at :func:`container_config_dir`, so it exists in every tmpfs mode (the
  old location survived restarts only because sac happened to relocate
  the container ``/tmp`` onto ``<state_dir>/tmp-scratch/tmp``);
* before launch it is seeded with the same global-onboarding gate and
  workspace-trust entry the home gets, so the TUI boots straight to the
  prompt;
* the provider key is pre-APPROVED in ``customApiKeyResponses`` (Claude
  Code identifies a key by its last 20 characters) and a stale rejection
  of the same key is dropped — the spec declares the key; an answer given
  at a prompt must not override it.

A dir left at the legacy scratch location is moved into place once, so an
agent's history and file-history survive the change.

The CONVERSATION STORE is shared, not seeded. Claude Code keeps transcripts
under ``$CLAUDE_CONFIG_DIR/projects``; a provider config dir would start with
none, so a spec pinned to ``session: resume`` exits at boot with "No
conversation found with session ID" (measured 2026-09-05 04:44 UTC, business:
its 129 MB transcript sat in the overlay home's ``.claude/projects``). The seed
therefore makes ``projects`` a symlink to ``<container_home>/.claude/projects``
-- the store every other engine writes -- so a conversation survives an
engine switch in both directions.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from ._to_home_overlay import DEFAULT_CONTAINER_HOME
from .onboarding import ensure_project_onboarding

logger = logging.getLogger(__name__)

#: Sub-directory of the agent's state dir that backs the container config dir.
HOST_DIRNAME = "provider-cfg"

#: Claude Code records an approved / rejected key by its trailing 20 chars.
KEY_SUFFIX_LEN = 20


def container_config_dir(name: str) -> str:
    """The in-container ``CLAUDE_CONFIG_DIR`` for agent ``name``.

    Distinct from the OAuth path's ``/tmp/sac-claude`` so a stale OAuth
    bind can never win.
    """
    return f"/tmp/sac-{name}-provider-cfg"


def host_config_dir(state_dir: Path) -> Path:
    """The host dir bound at :func:`container_config_dir`."""
    return Path(state_dir).expanduser() / HOST_DIRNAME


def legacy_scratch_config_dir(state_dir: Path, name: str) -> Path:
    """Where the dir landed before it was bound: the relocated container ``/tmp``."""
    return (
        Path(state_dir).expanduser()
        / "tmp-scratch"
        / "tmp"
        / f"sac-{name}-provider-cfg"
    )


def approve_api_key(claude_json: Path, api_key: str) -> bool:
    """Record ``api_key`` as approved in ``customApiKeyResponses``.

    Adds the key's suffix to ``approved`` when absent and removes it from
    ``rejected`` when present; every other key in the file is kept. Never
    raises — an unreadable or unwritable file is logged and left alone.

    Returns:
        True iff the file was rewritten.
    """
    suffix = api_key[-KEY_SUFFIX_LEN:]
    try:
        data: dict = {}
        if claude_json.exists():
            with claude_json.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        responses = data.get("customApiKeyResponses")
        if not isinstance(responses, dict):
            responses = {}
        approved = [s for s in responses.get("approved", []) if isinstance(s, str)]
        rejected = [s for s in responses.get("rejected", []) if isinstance(s, str)]
        changed = False
        if suffix not in approved:
            approved.append(suffix)
            changed = True
        if suffix in rejected:
            rejected = [s for s in rejected if s != suffix]
            changed = True
        if not changed:
            return False
        data["customApiKeyResponses"] = {
            **responses,
            "approved": approved,
            "rejected": rejected,
        }
        tmp_path = claude_json.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, claude_json)
        return True
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("cannot approve the provider key in %s: %s", claude_json, exc)
        return False


def link_conversation_store(
    host: Path, container_home: str, transcript_home: Path | None = None
) -> bool:
    """Make ``<host>/projects`` resolve to the agent's own transcript store.

    The link targets the CONTAINER path ``<container_home>/.claude/projects``
    (it dangles on the host by design). ``transcript_home`` is the host dir
    backing the container home, when known; its ``.claude/projects`` is
    created so the link never dangles inside the container either -- Node's
    recursive mkdir refuses to create a directory through a dangling link,
    which would strand a provider agent that has never run another engine.

    A real ``projects`` directory already present (an agent that ran on a
    provider before the bind) is left alone: its conversations live there.

    Returns:
        True iff the link was created by this call.
    """
    if transcript_home is not None:
        try:
            (Path(transcript_home) / ".claude" / "projects").mkdir(
                parents=True, exist_ok=True
            )
        except OSError as exc:
            logger.warning(
                "cannot create the transcript store under %s: %s", transcript_home, exc
            )
    link = host / "projects"
    if link.exists() or link.is_symlink():
        return False
    target = f"{container_home.rstrip('/')}/.claude/projects"
    link.symlink_to(target, target_is_directory=True)
    logger.info("provider config dir %s: projects -> %s", host, target)
    return True


def seed_provider_config_dir(
    *,
    state_dir: Path,
    name: str,
    workdir: str,
    api_key: str,
    container_home: str = DEFAULT_CONTAINER_HOME,
    transcript_home: Path | None = None,
) -> Path:
    """Materialise and seed the host config dir for a provider agent.

    Idempotent: seeds only what is absent, so the TUI's own writes
    (session stats, history, a machine id) survive every start.

    Returns:
        The host dir to bind at :func:`container_config_dir`.
    """
    host = host_config_dir(state_dir)
    legacy = legacy_scratch_config_dir(state_dir, name)
    if not host.exists() and legacy.is_dir():
        host.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(host))
        logger.info("moved provider config dir %s -> %s", legacy, host)
    host.mkdir(parents=True, exist_ok=True)
    ensure_project_onboarding(workdir, home=host)
    approve_api_key(host / ".claude.json", api_key)
    link_conversation_store(host, container_home, transcript_home)
    return host


def provider_config_dir_flags(
    *,
    state_dir: Path,
    name: str,
    workdir: str,
    api_key: str,
    container_home: str = DEFAULT_CONTAINER_HOME,
    transcript_home: Path | None = None,
) -> list[str]:
    """Seed the host dir and render the ``--bind`` that mounts it.

    Writable: Claude Code keeps its history and stats there.
    """
    host = seed_provider_config_dir(
        state_dir=state_dir,
        name=name,
        workdir=workdir,
        api_key=api_key,
        container_home=container_home,
        transcript_home=transcript_home,
    )
    return ["--bind", f"{host}:{container_config_dir(name)}:rw"]


__all__ = [
    "HOST_DIRNAME",
    "KEY_SUFFIX_LEN",
    "approve_api_key",
    "container_config_dir",
    "host_config_dir",
    "legacy_scratch_config_dir",
    "link_conversation_store",
    "provider_config_dir_flags",
    "seed_provider_config_dir",
]
