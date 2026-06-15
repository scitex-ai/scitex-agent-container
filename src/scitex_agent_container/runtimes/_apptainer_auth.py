"""Anthropic-auth argv emission for the apptainer runtime.

Extracted from ``_apptainer_runtime.py`` (512-line cap) — mirrors the
existing helper-module split (``_apptainer_creds``, ``_apptainer_build``,
``_apptainer_listen_env``, ``_apptainer_iso_flags``, ``_apptainer_provider``).

:func:`auth_argv` renders the ``--env`` / ``--bind`` flags that wire the
in-container Claude SDK to its backend. It branches on whether a
vendor-agnostic provider override is active:

* ``spec.claude.provider`` set → run against an Anthropic-SDK-compatible
  backend (DeepSeek, gateway, ...) on an API key. Emits the provider
  env flags (``ANTHROPIC_BASE_URL`` + ``SAC_ANTHROPIC_API_KEY`` + a clean
  ``CLAUDE_CONFIG_DIR``) and SKIPS the OAuth credentials bind entirely —
  an API-key backend needs no OAuth. See ``_apptainer_provider``.

* no provider → existing Anthropic OAuth path: forward host
  ``ANTHROPIC_API_KEY`` / ``SAC_ANTHROPIC_API_KEY`` (pay-per-token env),
  then bind the resolved ``.credentials.json`` at ``/tmp/sac-claude``
  and point the SDK at it via ``CLAUDE_CONFIG_DIR``.

This helper only CALLS ``_apptainer_creds.resolve_cred_file`` (public
API) — it does not own per-account credential resolution.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import AgentConfig
from ._apptainer_provider import provider_active, provider_env_flags


def auth_argv(config: AgentConfig, state_dir: Path) -> list[str]:
    """Render the Anthropic-auth ``--env`` / ``--bind`` flags for ``config``.

    See the module docstring for the provider-vs-OAuth branch. Raises
    :class:`_apptainer_provider.ProviderEnvError` (fail-loud) when a
    provider override is declared but its key env var is unset or it
    collides with ``spec.claude.account``.
    """
    if provider_active(config):
        # Provider backend: API key, no OAuth. The provider helper owns
        # ANTHROPIC_BASE_URL + SAC_ANTHROPIC_API_KEY + a clean
        # CLAUDE_CONFIG_DIR (the last-wins conflict-breaker). The OAuth
        # creds bind is intentionally NOT emitted.
        return provider_env_flags(config)

    argv: list[str] = []

    # Designated credentials file (spec.claude.credentials_file): the
    # operator names ONE host ``.credentials.json`` to mount writable at
    # the in-container ``$HOME/.claude/.credentials.json`` (single source
    # of truth — see :func:`credentials_file_bind`, emitted last in
    # build_run_argv so the relaxed ``--home`` tmpfs / overlay-upper bind
    # cannot shadow it). When designated we SKIP the account/host
    # dir-bind + ``CLAUDE_CONFIG_DIR`` redirect entirely: the file IS the
    # agent's credentials at its default ``$HOME`` location. Still forward
    # the pay-per-token env below (harmless; the OAuth file wins for
    # Pro/Max).
    claude_spec = getattr(config, "claude", None)
    designated = str(getattr(claude_spec, "credentials_file", "") or "").strip()
    if designated:
        for auth_env in ("ANTHROPIC_API_KEY", "SAC_ANTHROPIC_API_KEY"):
            val = os.environ.get(auth_env)
            if val:
                argv += ["--env", f"{auth_env}={val}"]
        return argv

    # Forward Anthropic auth (mirrors container.py). Order matters:
    # see runtimes/_sdk_common.py:provision_anthropic_auth — when
    # `~/.claude/.credentials.json` exists (Pro/Max OAuth flow), the
    # SDK reads the file directly and a bare `ANTHROPIC_API_KEY`
    # env shadows it (Anthropic rejects sk-ant-oat* OAuth tokens as
    # bare env). So we only pass *pay-per-token* env values; the
    # credentials.json is bind-mounted below.
    for auth_env in ("ANTHROPIC_API_KEY", "SAC_ANTHROPIC_API_KEY"):
        val = os.environ.get(auth_env)
        if val:
            argv += ["--env", f"{auth_env}={val}"]

    # Mount operator's Pro/Max credentials when present.
    # Target lives under /tmp/ (writable tmpfs / overlay) rather
    # than $HOME — the D2 preflight requires $HOME to be empty, and
    # binding under $HOME would scaffold a host-mirroring directory.
    # CLAUDE_CONFIG_DIR points the SDK at this dir so it finds the
    # credentials file without needing $HOME pollution.
    #
    # Mounted RW (no ``:ro``) so the in-container Claude CLI can
    # refresh the OAuth ``accessToken`` in place when the host's
    # token expires (~1h cadence). Without RW the bind-mounted file
    # is frozen and every container 401s after token-expiry, forcing
    # a manual scp-from-lead dance to re-seed peers. The CLI's
    # refresh code-path itself is responsible for any concurrency
    # locking — the bind is just a file passthrough.
    #
    # OAuth credentials bind shape: DIRECTORY bind, unconditionally
    # (operator task #11 + task #13).
    #
    # The per-account snapshot AND the host-live ``~/.claude/`` file are
    # both rewritten by host-side atomic-replace paths —
    # ``_account.creds_sync._atomic_copy`` (sync-live + watch-live),
    # ``_state.account_store.switch_account``,
    # ``_account.claude_usage._refresh_access_token_at`` — all using
    # ``tmp + os.replace``. A single-file bind mount is on the file's
    # dentry/inode; an atomic-replace orphans that inode (the bind
    # surfaces as ``...credentials.json//deleted`` in
    # ``/proc/<pid>/mountinfo``) and every already-running agent
    # silently loses the shared file, regressing into the per-copy
    # collision-401 disease the snapshot model was meant to fix.
    # A DIRECTORY bind resolves child files by name through the
    # underlying filesystem on every open, so a tmp+rename inside the
    # dir is visible to the container immediately — in BOTH directions
    # (host writes seen by the container, and in-container CLI refresh
    # writes seen by host writers).
    #
    # PR #262 (task #11) made the PINNED branch dir-bind; this module
    # (task #13) makes the UNPINNED/host-live branch dir-bind too. The
    # legacy single-file-bind code path is fully retired.
    #
    # BOTH branches dir-bind (operator task #13, 2026-06-04 cred-refresher
    # storm root cause).
    #
    # Previously the unpinned/host-live branch single-file-bound
    # ``~/.claude/.credentials.json`` at ``/tmp/sac-claude/.credentials.json``.
    # The comment justifying that choice said the host live file is
    # "rewritten only by manual claude /login / sac accounts switch" —
    # this was wrong on the live system: ``_account/creds_watch.py``
    # (the watch-live daemon mirroring ``~/.claude/`` into the snapshot
    # store) AND ``_account/creds_sync._atomic_copy`` both atomic-replace
    # the file. Any such rename orphans the bind's inode → bind goes
    # ``//deleted`` in ``/proc/<pid>/mountinfo`` → container reads the
    # stale pre-rename token forever → 401 at natural expiry. The
    # cred-refresher agents (unpinned by design, since they refresh
    # whatever ``~/.claude`` resolves to) hit this fleet-wide on
    # 2026-06-04 03:00.
    #
    # Fix: the unpinned branch now dir-binds ``~/.claude/`` at
    # ``/tmp/sac-claude``. Same shape as the pinned branch; ONLY the
    # source dir differs. Atomic rename inside the bound dir is
    # immediately visible to the container.
    #
    # Scope acknowledgement: this exposes the operator's host
    # ``~/.claude/`` (settings.json, chat history, projects DB) to the
    # container, where before only ``.credentials.json`` was visible.
    # The bundled in-container ``claude`` CLI already READS these via
    # ``CLAUDE_CONFIG_DIR=/tmp/sac-claude``; the change is that it can
    # now also WRITE to them. The cleaner long-term fix is to resolve
    # the active host login to its snapshot dir (per ADR-0017's
    # one-account-one-refresher invariant), which only exposes
    # ``.credentials.json``. The recommended deployment is to pin via
    # ``spec.claude.account`` and let the unpinned dir-bind be the
    # degraded fallback for the host-active-login case. The watch-live
    # daemon (skill 26 § 6) mirrors operator relogins into the snapshot
    # store so a pinned agent tracks the active login automatically.
    from ._apptainer_creds import resolve_cred_file

    cred_file = resolve_cred_file(config, state_dir)
    if cred_file is None or not cred_file.is_file():
        return argv

    # Dir-bind unconditionally. ``cred_file.parent`` is:
    #   - Pinned:   ~/.scitex/agent-container/accounts/<acct>/  (snapshot dir,
    #               narrowly scoped to .credentials.json + account.json).
    #   - Unpinned: ~/.claude/  (legacy host live, over-binds; see
    #               scope-acknowledgement comment above).
    # CLAUDE_CONFIG_DIR points the in-container CLI at the bound dir
    # so it finds .credentials.json inside it.
    bind_src = cred_file.parent
    bind_dest = "/tmp/sac-claude"

    argv += [
        "--bind",
        f"{bind_src}:{bind_dest}:rw",
        "--env",
        "CLAUDE_CONFIG_DIR=/tmp/sac-claude",
    ]
    return argv


def credentials_file_bind(config: AgentConfig) -> list[str]:
    """Render the writable file-bind for ``spec.claude.credentials_file``.

    Emitted LAST in :func:`_apptainer_build_argv.build_run_argv` (after
    the overlay-upper-home bind) so a relaxed ``--home`` tmpfs or
    ``--bind <upper>:/home/agent`` cannot shadow the credentials file —
    user binds apply in order and the last bind to a path wins.

    Binds the designated host file ``rw`` at ``<container_home>/.claude/
    .credentials.json`` so the in-container ``claude`` reads AND
    refreshes that single file directly (single source of truth). No-op
    when the field is unset, the file is missing, or a provider override
    is active (API-key backend → no OAuth file).

    Caveat: a single-file bind is on the file's inode. An in-container
    refresh that rewrites in-place persists to the source; one that does
    tmp+rename orphans the bind (the source keeps the pre-rename token).
    The designated file should therefore NOT be a path concurrently
    atomic-renamed by host-side ``sac accounts``/watch-live tooling — it
    is the agent's private, operator-rotated credentials file.
    """
    if provider_active(config):
        return []
    claude_spec = getattr(config, "claude", None)
    designated = str(getattr(claude_spec, "credentials_file", "") or "").strip()
    if not designated:
        return []
    src = Path(designated).expanduser()
    if not src.is_file():
        raise FileNotFoundError(
            f"spec.claude.credentials_file points at {src}, which is not "
            "a file. Designate an existing .credentials.json (the agent "
            "mounts it writable at $HOME/.claude/.credentials.json as its "
            "single source of truth)."
        )
    from ._to_home_overlay import resolve_container_home

    container_home = resolve_container_home(config).rstrip("/")
    dest = f"{container_home}/.claude/.credentials.json"
    return ["--bind", f"{src}:{dest}:rw"]


__all__ = ["auth_argv", "credentials_file_bind"]
