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
    # Mounted READ-ONLY (``:ro``) — master-host single-refresher model
    # (operator 2026-07-08, credential-churn root-cause fix). The agent
    # is a READ-ONLY CONSUMER of the credential: it NEVER refreshes or
    # rotates the OAuth token. Previously this dir was bound ``:rw`` so
    # the in-container Claude CLI could refresh the ``accessToken`` in
    # place — but that made every running agent a refresher, and an
    # agent refresh CONSUMES the single-use OAuth refresh_token: the
    # instant an operator logged a fresh account in, a running agent ate
    # its refresh_token (the "cred churn" disease). Now the host-side
    # ``sac-accounts-refresh`` timer is the SOLE refresher; it rotates
    # every account's snapshot on a fixed cadence, and the DIRECTORY
    # bind (below) makes the timer's atomic-replace refreshes visible to
    # the container on the next open — so a ``:ro`` agent still tracks
    # the freshest token without ever writing one. The fail-loud expiry
    # checks still apply: a ``:ro`` agent bound to an already-expired
    # token cannot work, so an expired snapshot is an operator/timer
    # problem to fix before launch, not something the agent can rescue.
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
    # underlying filesystem on every open, so a host-side tmp+rename
    # inside the dir is visible to the container immediately. Under the
    # ``:ro`` single-refresher model this matters in ONE direction that
    # counts: the host-side ``sac-accounts-refresh`` timer's atomic
    # replace of ``.credentials.json`` is picked up by the container on
    # the next open, so a read-only agent tracks the freshest token
    # without a restart. The container no longer writes back (``:ro``),
    # which is the whole point — no agent-side refresh, no churn.
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
        # READ-ONLY: the agent consumes the credential but never
        # refreshes it (master-host single-refresher model). See the
        # "Mounted READ-ONLY" rationale above. The host-side timer is
        # the sole refresher; the dir bind surfaces its refreshes.
        f"{bind_src}:{bind_dest}:ro",
        "--env",
        "CLAUDE_CONFIG_DIR=/tmp/sac-claude",
    ]
    return argv


class CredentialExpiredError(RuntimeError):
    """Raised when ``spec.claude.credentials_file`` resolves to an expired
    or unverifiable OAuth credential.

    Mirrors :class:`_apptainer_creds.PinnedAccountError`'s fail-loud
    contract for the EXPLICIT-file path. ``resolve_cred_file`` only
    expiry-checks the ``spec.claude.account`` branch; the explicit-file
    branch was not checked, so a pinned ``credentials_file`` whose token
    had expired was still bound ``:rw`` into the SIF. The in-container
    ``claude`` then 401'd and exited before rendering, and
    ``sac agents start`` collapsed to the opaque "runtime.start()
    returned False / <empty — inner process exited before any output>"
    failure with no cause (2026-06-17 ``ywatanabe-scitex-ai``). The
    message names the resolved path and the remedy (``claude /login``
    then re-snapshot) so the operator's next step is unambiguous.
    """


def _assert_credential_unexpired(
    src: Path, *, origin: str, now: float | None = None
) -> None:
    """Fail loud if ``src`` has no verifiable, in-the-future OAuth expiry.

    Reuses :func:`_account.creds_sync._read_oauth_expiry_seconds` (the
    same ms→s normaliser the account-pinned resolver and the host
    snapshot refreshers use) so every credential-freshness check in the
    codebase agrees. ``now`` is a real-time injection seam for tests;
    production passes ``None`` → ``time.time()``.

    Raises :class:`CredentialExpiredError` when the file carries no
    numeric ``claudeAiOauth.expiresAt`` (unverifiable) or that expiry is
    already in the past (stale). Matches the account-pinned contract: a
    launch-time-expired PINNED credential is an operator/refresher
    problem to fix BEFORE launch, not something to bind-and-hope the
    in-container ~1h refresh rescues — a dead refresh token cannot, which
    is exactly how the opaque empty-pane failure arises.
    """
    import time

    from .._account.creds_sync import _read_oauth_expiry_seconds

    expiry = _read_oauth_expiry_seconds(src)
    now_ts = now if now is not None else time.time()
    if expiry is None:
        raise CredentialExpiredError(
            f"credentials file {src} ({origin}) has no numeric "
            "`claudeAiOauth.expiresAt` — refusing to launch with an "
            "unverifiable token. Fix: `claude /login` to that account on "
            "this host, then `sac accounts save <account>` (snapshot it) "
            "or `sac accounts sync-live`, and restart this agent."
        )
    if expiry <= now_ts:
        ago = int(now_ts - expiry)
        raise CredentialExpiredError(
            f"credentials file {src} ({origin}) expired {ago}s ago — "
            "refusing to launch a pinned agent with a stale token. The "
            "in-container claude would 401 and exit before rendering, "
            "surfacing only as an empty pane. Fix: `claude /login` to "
            "that account on this host, then `sac accounts save "
            "<account>` (or `sac accounts sync-live`), and restart this "
            "agent."
        )


def credentials_file_bind(
    config: AgentConfig, *, now: float | None = None
) -> list[str]:
    """Render the READ-ONLY file-bind for the agent's credentials file.

    Emitted LAST in :func:`_apptainer_build_argv.build_run_argv` (after
    the overlay-upper-home bind) so a relaxed ``--home`` tmpfs or
    ``--bind <upper>:/home/agent`` cannot shadow the credentials file —
    user binds apply in order and the last bind to a path wins.

    Source resolution (first hit wins):

      1. Explicit ``spec.claude.credentials_file`` — operator-pinned
         host path. Wins for any agent that designates it.
      2. ``spec.claude.account`` — per-host snapshot resolved via the
         SAC account-store CWD cascade (same path the SDK runtime uses
         through :func:`_apptainer_creds.resolve_cred_file`). This is
         the canonical single-source model (operator 2026-06-15,
         lead-learnings/29): each host writable-binds its OWN local
         snapshot; NO COPY between hosts; the snapshot dir is
         server-managed. A pinned-account TUI agent gets this bind
         AUTOMATICALLY without any manual ``credentials_file:`` line.
      3. Neither set or a provider backend is active → no bind.

    Binds the resolved host file READ-ONLY (``:ro``) at
    ``<container_home>/.claude/.credentials.json`` so the in-container
    ``claude`` (both SDK and TUI variants) READS that single file
    directly but NEVER refreshes or rotates it. The SAME path the
    interactive ``claude`` auto-discovers when no ``$CLAUDE_CONFIG_DIR``
    redirect is set — operator-confirmed working shape (figrecipe +
    scitex-todo manual test 2026-06-15).

    Master-host single-refresher model (operator 2026-07-08, cred-churn
    root-cause fix): the agent is a READ-ONLY CONSUMER. Previously this
    file was bound ``:rw`` so the in-container CLI refreshed the OAuth
    ``accessToken`` in place — but a refresh CONSUMES the single-use
    OAuth refresh_token, so a running agent would eat the refresh_token
    of a freshly-logged-in account (the churn). Now the host-side
    ``sac-accounts-refresh`` timer is the SOLE refresher: it rotates the
    account snapshot's access_token on a fixed cadence and writes it
    back to the SAME snapshot file this bind sources, so a ``:ro`` agent
    always reads a timer-kept-fresh token without ever writing one.

    The fail-loud expiry gate (``_assert_credential_unexpired`` for the
    explicit-file branch, ``resolve_cred_file`` → ``PinnedAccountError``
    for the account branch) is unchanged and still load-bearing: a
    ``:ro`` agent bound to an already-expired token cannot work, so an
    expired credential is refused at launch (an operator/timer problem
    to fix first, not one the read-only agent can rescue).

    Caveat: a single-file ``:ro`` bind is on the file's inode. A
    host-side tmp+rename refresh (the timer's atomic-replace) orphans
    the bind — the container keeps reading the pre-rename inode until it
    restarts. For a ``:ro`` agent this is a staleness (not a corruption)
    concern: the token is valid until its natural expiry, and the timer
    keeps the snapshot fresh so a restart re-binds the current inode.
    The account-pinned SDK path additionally gets the DIRECTORY bind at
    ``/tmp/sac-claude`` (:func:`auth_argv`), which resolves the child
    file by name on every open and so DOES reflect atomic-replace
    refreshes without a restart.
    """
    if provider_active(config):
        return []
    claude_spec = getattr(config, "claude", None)
    designated = str(getattr(claude_spec, "credentials_file", "") or "").strip()
    src: Path | None = None
    src_origin = ""
    if designated:
        src = Path(designated).expanduser()
        src_origin = f"spec.claude.credentials_file={designated!r}"
        if not src.is_file():
            raise FileNotFoundError(
                f"spec.claude.credentials_file points at {src}, which is "
                "not a file. Designate an existing .credentials.json "
                "(the agent mounts it writable at $HOME/.claude/"
                ".credentials.json as its single source of truth)."
            )
        # Fail loud on an EXPIRED/unverifiable pinned credential BEFORE
        # binding it into the SIF. The account branch below is already
        # expiry-checked by ``resolve_cred_file`` (→ PinnedAccountError);
        # the explicit-file branch was not, so a dead pinned token bound
        # :rw made the in-container claude 401 and exit, collapsing
        # ``sac agents start`` into the opaque "runtime.start() returned
        # False / empty pane" failure (2026-06-17 ywatanabe-scitex-ai).
        _assert_credential_unexpired(src, origin=src_origin, now=now)
    elif str(getattr(claude_spec, "account", "") or "").strip():
        # Account-pinned auto-resolution (operator+lead 2026-06-15):
        # delegate to the SDK-path resolver so SDK and TUI agree on
        # snapshot location (CWD cascade via ``_store_path``). Raises
        # :class:`PinnedAccountError` when the snapshot is absent or
        # expired — the canonical fail-loud contract.
        from pathlib import Path as _Path

        # Import lazily so the absence of a snapshot doesn't impact
        # unpinned/provider-backed callers, and to break import cycles
        # with ``_apptainer_creds`` (which itself imports from this
        # module's siblings).
        from ._apptainer_creds import resolve_cred_file

        # state_dir is unused by ``resolve_cred_file`` for the pinned
        # branch — pass a sentinel that satisfies the signature.
        src = resolve_cred_file(config, _Path("/dev/null"))
        src_origin = (
            f"spec.claude.account={getattr(claude_spec, 'account', '')!r} → {src}"
        )
    if src is None:
        return []
    if not src.is_file():
        raise FileNotFoundError(
            f"resolved credentials path {src} ({src_origin}) is not a "
            "file. Refusing to launch with an unverifiable credential."
        )
    from ._to_home_overlay import resolve_container_home

    container_home = resolve_container_home(config).rstrip("/")
    dest = f"{container_home}/.claude/.credentials.json"
    # READ-ONLY: the agent consumes the credential but never refreshes
    # it (master-host single-refresher model — see the docstring). The
    # host-side ``sac-accounts-refresh`` timer is the sole refresher.
    return ["--bind", f"{src}:{dest}:ro"]


def ensure_credentials_bind_target(
    config: AgentConfig,
    *,
    home_host: Path,
    overlay_upper_home: Path | None = None,
    bind_flags: list[str] | None = None,
) -> Path | None:
    """Pre-create the host-side placeholder for the credentials file-bind.

    apptainer FILE binds require the in-container destination to ALREADY
    EXIST on the underlying filesystem — a fresh directory-overlay agent
    otherwise FATALs at boot with::

        mount … /home/agent/.claude/.credentials.json … destination doesn't
        exist in container

    The destination of :func:`credentials_file_bind` is
    ``<container_home>/.claude/.credentials.json``. The host filesystem
    backing that container ``$HOME`` is the overlay upper-home for relaxed
    directory-overlay specs (bound OVER ``--home`` in
    :func:`_apptainer_build_argv.build_run_argv`), else the workspace-home
    dir (``<state>/home``, bound at ``/home/agent``). This ensures an empty
    0-byte ``.claude/.credentials.json`` exists at that host path so the
    ``:ro`` bind lands; the bind then SHADOWS this placeholder with the real
    operator credential — the placeholder's contents never matter.

    CRITICAL — placement: the placeholder is written into the overlay
    upper-home / workspace-home (the bind DESTINATION backing), NEVER into
    ``to_home/``. The to_home credential-leak guard
    (:func:`_to_home._scan_for_credential_leak`) REFUSES any
    ``.credentials.json`` under ``to_home/`` — and rightly so: credentials
    are runtime bind state, not static workspace content. These two homes
    are deploy destinations, outside that guard's scan roots.

    ``bind_flags`` is the ALREADY-RESOLVED output of
    :func:`credentials_file_bind` — the caller passes it so the source
    resolution (and its fail-loud expiry check) runs only once per launch.
    When ``None`` we resolve it ourselves (the convenience path used by
    runtimes that don't pre-compute it). The in-container destination is
    parsed from the bind so the host placeholder mirrors the EXACT path
    apptainer will mount onto (``.claude/.credentials.json`` under $HOME).

    No-op (returns ``None``) when no credentials bind will be emitted
    (no ``spec.claude.credentials_file`` / ``account``, or a provider
    backend is active) — there is no file-bind target to satisfy. Returns
    the placeholder path it ensured, otherwise.

    Best-effort on the placeholder write itself: a failure here must not
    block launch (the bind may still succeed if the target exists for
    another reason), so an OSError is swallowed after logging.
    """
    flags = bind_flags if bind_flags is not None else credentials_file_bind(config)
    if not flags:
        return None
    # flags == ["--bind", "<src>:<container_home>/.claude/.credentials.json:ro"]
    from ._to_home_overlay import resolve_container_home

    container_home = resolve_container_home(config).rstrip("/")
    # Relative path of the bind target under the container $HOME, derived
    # from the emitted destination so the placeholder mirrors it exactly.
    dest_in_container = _bind_destination(flags)
    rel = ".claude/.credentials.json"
    if dest_in_container and dest_in_container.startswith(container_home + "/"):
        rel = dest_in_container[len(container_home) + 1 :]
    # Host dir backing the container $HOME: overlay upper-home (relaxed
    # directory-overlay) wins; else the workspace-home bind.
    backing = (
        overlay_upper_home
        if overlay_upper_home is not None and overlay_upper_home.is_dir()
        else home_host
    )
    placeholder = backing / rel
    try:
        placeholder.parent.mkdir(parents=True, exist_ok=True)
        if not placeholder.exists():
            placeholder.touch()
    except OSError as exc:  # stx-allow: fallback (reason: a placeholder-create failure must not block launch — the bind may still land if the target exists for another reason; logged for the operator)
        import logging

        logging.getLogger(__name__).warning(
            "credentials bind-target placeholder %s could not be created "
            "(container_home=%s): %s",
            placeholder,
            container_home,
            exc,
        )
        return None
    return placeholder


def _bind_destination(bind_flags: list[str]) -> str:
    """Extract the in-container destination path from a ``--bind`` flag pair.

    ``["--bind", "<src>:<dst>:rw"]`` → ``"<dst>"``. The source path may
    itself contain ``:`` only on exotic filesystems; apptainer bind specs
    are ``src:dst[:opts]`` and our emitted dst + opts are fixed, so we take
    the destination as the second colon-field from the spec's tail. Returns
    ``""`` when the shape is unrecognised (the caller falls back to the
    canonical ``.claude/.credentials.json`` relative path).
    """
    if len(bind_flags) < 2:
        return ""
    spec = bind_flags[1]
    # Strip a trailing ``:rw`` / ``:ro`` mount-option, then the dst is the
    # last colon-field of what remains (src may be absolute with no colon).
    body = spec
    for opt in (":rw", ":ro"):
        if body.endswith(opt):
            body = body[: -len(opt)]
            break
    parts = body.rsplit(":", 1)
    return parts[1] if len(parts) == 2 else ""


__all__ = [
    "CredentialExpiredError",
    "auth_argv",
    "credentials_file_bind",
    "ensure_credentials_bind_target",
]
