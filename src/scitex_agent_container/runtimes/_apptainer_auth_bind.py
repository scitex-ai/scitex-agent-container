"""Credentials FILE-BIND emission for the apptainer runtime.

Extracted from ``_apptainer_auth.py`` (512-line cap — the openai-compat-3
auth branch pushed it over; mirrors the existing helper-module split:
``_apptainer_creds``, ``_apptainer_provider``, ``_apptainer_listen_env``).
``_apptainer_auth`` re-exports every public name below, so existing
``from ._apptainer_auth import credentials_file_bind`` imports keep
resolving unchanged.

This module owns ONE concern: rendering (and pre-creating the target
for) the shared WRITABLE ``.credentials.json`` file-bind at the
container ``$HOME/.claude/.credentials.json``. The per-launch backend
env argv (``auth_argv``) stays in ``_apptainer_auth``.
"""

from __future__ import annotations

from pathlib import Path

from ..config import AgentConfig
from ._apptainer_provider import openai_provider_active, provider_active


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
    """Render the WRITABLE file-bind for the agent's credentials file.

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
      3. Neither set, an Anthropic-compat backend override is active,
         or the launch resolves to the ``openai`` agent-SDK family
         (openai-compat-3 — no Anthropic backend to auth to) → no bind.

    Binds the resolved host file WRITABLE (``:rw``) at
    ``<container_home>/.claude/.credentials.json`` so the in-container
    ``claude`` (both SDK and TUI variants) shares that single file — the
    SAME path the interactive ``claude`` auto-discovers when no
    ``$CLAUDE_CONFIG_DIR`` redirect is set — operator-confirmed working
    shape (figrecipe + scitex-todo manual test 2026-06-15).

    Why ``:rw`` (INCIDENT 2026-07-10 follow-up, operator 2026-07-11,
    reversing the 2026-07-08 ``:ro`` flip): a read-only bind CANNOT
    prevent the in-container ``claude`` from POSTing a token refresh —
    the server rotates and invalidates the old refresh_token chain
    regardless — it can only prevent the rotation from being RECORDED,
    leaving the host snapshot holding a dead refresh_token (worst of
    both worlds). A shared writable credentials file is the NORMAL,
    supported Claude configuration (any workstation runs several
    ``claude`` processes against one ``~/.claude/.credentials.json``):
    whoever refreshes writes the rotated pair back, and every other
    consumer re-reads it. sac's own refresher takes ``flock`` + atomic
    write (:mod:`.._account.token_refresh`), and
    ``refresh_account_credentials`` re-reads-and-retries once on
    ``invalid_grant`` so a concurrently-rotated token is picked up
    instead of being declared dead. Cross-HOST sharing remains unsolved
    by design — that is the ``sac-host-token-broker-single-writer``
    follow-up, not this bind.

    The fail-loud expiry gate (``_assert_credential_unexpired`` for the
    explicit-file branch, ``resolve_cred_file`` → ``PinnedAccountError``
    for the account branch) is unchanged and still load-bearing: an
    agent bound to an already-expired token cannot work, so an expired
    credential is refused at launch (an operator/timer problem to fix
    first, not one the agent can rescue).

    Caveat: a single-file bind is on the file's inode. A host-side
    tmp+rename refresh (the timer's atomic-replace) orphans the bind —
    the container keeps reading the pre-rename inode until it restarts.
    This is a staleness (not a corruption) concern: the token is valid
    until its natural expiry, and the timer keeps the snapshot fresh so
    a restart re-binds the current inode. The account-pinned SDK path
    additionally gets the DIRECTORY bind at ``/tmp/sac-claude``
    (:func:`_apptainer_auth.auth_argv`), which resolves the child file
    by name on every open and so DOES reflect atomic-replace refreshes
    without a restart.
    """
    if provider_active(config) or openai_provider_active(config):
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
    # WRITABLE: the in-container claude shares the credential file like
    # any co-resident claude process would — whoever refreshes writes
    # the rotated token pair back so no consumer is left holding a
    # server-invalidated refresh_token (see the docstring's 2026-07-11
    # rationale; ``:ro`` could not stop the rotation, only its recording).
    return ["--bind", f"{src}:{dest}:rw"]


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
    credentials bind lands; the bind then SHADOWS this placeholder with the real
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
    # flags == ["--bind", "<src>:<container_home>/.claude/.credentials.json:rw"]
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
    "credentials_file_bind",
    "ensure_credentials_bind_target",
]
