"""Backend-auth argv emission for the apptainer runtime.

Extracted from ``_apptainer_runtime.py`` (512-line cap) — mirrors the
existing helper-module split (``_apptainer_creds``, ``_apptainer_build``,
``_apptainer_listen_env``, ``_apptainer_iso_flags``, ``_apptainer_provider``).
The credentials FILE-BIND half (``credentials_file_bind`` /
``ensure_credentials_bind_target`` / ``CredentialExpiredError``) lives in
``_apptainer_auth_bind`` (same cap, openai-compat-3 split) and is
re-exported below so existing imports keep resolving unchanged.

:func:`auth_argv` renders the ``--env`` / ``--bind`` flags that wire the
in-container agent SDK to its backend. It branches on the provider
story, most specific first:

* ``spec.provider: openai`` (TOP-LEVEL agent-SDK-family axis, or the
  ``SAC_PROVIDER`` ops-only override — openai-compat-3) → emit the
  OpenAI env columns (``SAC_OPENAI_API_KEY`` + ``OPENAI_API_KEY`` +
  optional ``OPENAI_BASE_URL`` / ``OPENAI_ORG_ID`` / ``OPENAI_PROJECT_ID``
  / ``SAC_OPENAI_MODEL`` pass-throughs) and NOTHING Anthropic: no OAuth
  env, no credentials bind — an ``openai``-family agent has no Anthropic
  backend to auth to. See ``_apptainer_provider.openai_env_flags``.

* ``spec.claude.provider`` set (nested Anthropic-COMPATIBLE backend
  override) → run against an Anthropic-SDK-compatible backend
  (DeepSeek, gateway, ...) on an API key. Emits the provider
  env flags (``ANTHROPIC_BASE_URL`` + ``SAC_ANTHROPIC_API_KEY`` + a clean
  ``CLAUDE_CONFIG_DIR``) and SKIPS the OAuth credentials bind entirely —
  an API-key backend needs no OAuth. See ``_apptainer_provider``.

* neither → existing Anthropic OAuth path: forward host
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
from ._apptainer_auth_bind import (  # noqa: F401 (re-export — see module docstring)
    CredentialExpiredError,
    credentials_file_bind,
    ensure_credentials_bind_target,
)
from ._apptainer_provider import (
    openai_env_flags,
    openai_provider_active,
    provider_active,
    provider_env_flags,
)


def auth_argv(config: AgentConfig, state_dir: Path) -> list[str]:
    """Render the backend-auth ``--env`` / ``--bind`` flags for ``config``.

    See the module docstring for the openai-family / provider / OAuth
    branch order. Raises :class:`_apptainer_provider.ProviderEnvError`
    (fail-loud) when a declared backend cannot be satisfied — key env
    var unset, ``spec.claude.account`` collision, or an ``openai``-family
    launch composed with an Anthropic-compat ``spec.claude.provider``
    override.
    """
    if openai_provider_active(config):
        # openai agent-SDK family (openai-compat-3): OPENAI_* columns
        # only. No Anthropic OAuth env and no credentials bind — the
        # helper owns SAC_OPENAI_API_KEY/OPENAI_API_KEY dual injection,
        # the SAC_PROVIDER marker, and the optional routing
        # pass-throughs (base URL / org / project / model).
        return openai_env_flags(config)

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
    # of truth — see :func:`_apptainer_auth_bind.credentials_file_bind`,
    # emitted last in build_run_argv so the relaxed ``--home`` tmpfs /
    # overlay-upper bind cannot shadow it). When designated we SKIP the
    # account/host dir-bind + ``CLAUDE_CONFIG_DIR`` redirect entirely: the
    # file IS the agent's credentials at its default ``$HOME`` location.
    # Still forward the pay-per-token env below (harmless; the OAuth file
    # wins for Pro/Max).
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
    # Mounted WRITABLE (``:rw``) — shared-credential model (operator
    # 2026-07-11, reversing the 2026-07-08 ``:ro`` flip after INCIDENT
    # 2026-07-10). A ``:ro`` bind cannot stop the in-container claude
    # from POSTing a refresh — the server rotates and invalidates the
    # old refresh_token chain regardless — it can only stop the rotation
    # from being RECORDED, stranding the host snapshot on a dead
    # refresh_token (worst of both worlds). A shared writable
    # credentials file is the normal, supported Claude shape (several
    # ``claude`` processes share one ``~/.claude/.credentials.json`` on
    # any workstation): whoever refreshes writes the rotated pair back
    # and every other consumer re-reads it. The host-side
    # ``sac-accounts-refresh`` timer keeps refreshing on cadence with
    # flock + atomic write (``_account.token_refresh``), and its
    # ``refresh_account_credentials`` re-reads-and-retries once on
    # ``invalid_grant`` so a concurrently-rotated token is recovered
    # rather than declared dead. The fail-loud expiry checks still
    # apply: an agent bound to an already-expired token cannot work, so
    # an expired snapshot is an operator/timer problem to fix before
    # launch, not something the agent can rescue.
    #
    # OAuth credentials bind shape: DIRECTORY bind, unconditionally
    # (operator task #11 + task #13).
    #
    # The per-account snapshot AND the host-live ``~/.claude/`` file are
    # both rewritten by host-side atomic-replace paths —
    # ``_account.creds_sync._atomic_copy`` (sync-live + watch-live),
    # ``_state.account_store.switch_account``,
    # ``_account.token_refresh._refresh_access_token_at`` — all using
    # ``tmp + os.replace``. A single-file bind mount is on the file's
    # dentry/inode; an atomic-replace orphans that inode (the bind
    # surfaces as ``...credentials.json//deleted`` in
    # ``/proc/<pid>/mountinfo``) and every already-running agent
    # silently loses the shared file, regressing into the per-copy
    # collision-401 disease the snapshot model was meant to fix.
    # A DIRECTORY bind resolves child files by name through the
    # underlying filesystem on every open, so a host-side tmp+rename
    # inside the dir is visible to the container immediately — the
    # host-side ``sac-accounts-refresh`` timer's atomic replace of
    # ``.credentials.json`` is picked up by the container on the next
    # open, and (since the 2026-07-11 ``:rw`` restore) a rotation the
    # container performs is likewise visible to the host and to every
    # co-bound agent.
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
        # WRITABLE: the in-container claude shares the credential file
        # like any co-resident claude process — a rotation it performs
        # is written back instead of silently invalidating the stored
        # refresh_token (see the "Mounted WRITABLE" rationale above).
        f"{bind_src}:{bind_dest}:rw",
        "--env",
        "CLAUDE_CONFIG_DIR=/tmp/sac-claude",
    ]
    return argv


__all__ = [
    "CredentialExpiredError",
    "auth_argv",
    "credentials_file_bind",
    "ensure_credentials_bind_target",
]
