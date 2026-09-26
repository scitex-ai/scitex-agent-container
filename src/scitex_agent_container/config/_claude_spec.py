"""``ClaudeSpec`` — the SINGLE-BACKEND ``spec.claude`` block.

Extracted from ``_types.py`` under the project's 512-line per-file cap,
exactly as ``_apptainer_spec`` / ``_proxy_types`` / ``_provider_types`` /
``_acl_types`` were before it. ``_types`` re-exports the name, so every
existing ``from ...config._types import ClaudeSpec`` keeps resolving.

This is the block a spec writes when it runs ONE backend. The
MULTI-backend surface is ``spec.engines`` (``config._engine_types``);
the two compose — the selected engine is folded ONTO the fields below
before the runtime is built, so every reader downstream of the loader
sees one resolved backend and needs to know nothing about engines.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ._provider_types import ProviderSpec

__all__ = ["ClaudeSpec"]


@dataclass
class ClaudeSpec:
    # v3-realign: model lives under spec.claude.model (promoted from
    # top-level spec.model — §3). Empty = runtime default.
    model: str = ""
    channels: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    # v3 escape hatch (§1 invariant): splat ``**raw_options`` into
    # ``ClaudeAgentOptions`` so power users can reach any SDK option
    # sac doesn't model. Merged on top of curated keys; raw_options wins.
    raw_options: dict = field(default_factory=dict)
    # Session continuity strategy. One of:
    #   fresh        never pass ``-c``/``--continue`` — every launch is an
    #                independent session (DEFAULT). This is the right
    #                behaviour for experiment trials, where each capsule
    #                run must be hermetic and must not inherit the prior
    #                conversation.
    #   continue     resume the latest session for this cwd (TUI: ``claude
    #                -c``; SDK: auto-resume the persisted session_id). The
    #                right behaviour for LONG-LIVED coordinator agents
    #                (lead/head/worker/telegrammer/project-maintainer/...)
    #                that must keep their working memory across restarts.
    #   resume       pass --resume <resume_id> (explicit session ID).
    # Aliases accepted at load time (parse_claude):
    #   new-session, new  -> fresh   (back-compat: the pre-2026-06 names)
    #   continue-or-new   -> continue
    # NOTE the default flipped from ``continue`` to ``fresh`` (2026-06-22,
    # "fresh by default, opt-in continue"): a spec that omits ``session``
    # now starts fresh. Coordinator/long-lived roles are mapped back to
    # ``continue`` BY ROLE so omitting the field keeps those agents
    # continuous WITHOUT editing every deployed spec, while experiment
    # capsules (non-coordinator roles) get fresh. See
    # ``config/_session_continuity.py`` (``role_wants_continuity`` /
    # ``default_session_for_role`` / ``_CONTINUITY_ROLES``) for the
    # role-default, applied in ``config/_loaders.py`` after ``parse_claude``;
    # and ``runtimes/_apptainer_inner_argv._tui_runner_argv`` for where the
    # resolved mode becomes ``claude -c`` (continue) or no flag (fresh).
    session: str = "fresh"
    # Only resume if the most recent session jsonl is newer than this many minutes.
    # None = no age check (always resume if session exists).
    continue_max_age_minutes: int | None = None
    # Explicit session ID to pass to --resume. Only used when session="resume".
    resume_id: str = ""
    auto_accept: bool = True
    # Saved-account name (from ``sac account list``) whose credential
    # snapshot this agent runs on. ``""`` = the host's live
    # ``~/.claude/.credentials.json`` (current default).
    #
    # When set, the runtime COPIES that account's ``.credentials.json``
    # into the agent's own state dir at start (frozen boot-copy, not a
    # live bind), so two agents pinned to two accounts never fight one
    # mount. The copy is bound RW so in-container ~1h token refresh keeps
    # working on the agent's private copy.
    #
    # Takes effect on next start/restart — a host ``/login`` does NOT
    # move a pinned agent (that is the point of pinning), and changing
    # this field requires ``sac agent restart`` to re-copy the snapshot.
    account: str = ""
    # Explicit credentials-file designation (host path to a
    # ``.credentials.json``). When set, the runtime BIND-MOUNTS that
    # exact file WRITABLE at the in-container
    # ``$HOME/.claude/.credentials.json`` — single source of truth, no
    # copy. The in-container ``claude`` reads/refreshes that file
    # directly, so an OAuth refresh persists straight back to the
    # designated file and never desyncs from / corrupts a shared
    # ``~/.claude/.credentials.json``. Designating different files for
    # different agents is how multiple accounts run side by side
    # (rotate by pointing agents at different credential files).
    #
    # Precedence: when set, this file-mount REPLACES the account /
    # host dir-bind auth path (no ``CLAUDE_CONFIG_DIR`` redirect) — the
    # designated file IS the agent's credentials. Mutually exclusive
    # with ``provider`` (an API-key backend needs no OAuth file).
    #
    # Caveat (documented in ``_apptainer_auth``): a single-file bind is
    # on the file's inode; a host-side atomic tmp+rename on the source
    # orphans the bind. In-container ``claude`` refresh that writes
    # in-place is safe; designate a path NOT concurrently atomic-renamed
    # by host-side ``sac accounts``/watch-live tooling.
    credentials_file: str = ""
    # Account POOL: a list of host paths to ``.credentials.json`` files
    # (one per saved account). When non-empty, the start pre-flight picks
    # ONE of them QUOTA-CONDITIONAL — token-fresh, avoiding 5h-blocked
    # and 7d-near-capped accounts, load-balanced across the fleet per
    # agent name (see ``_creds.pick_healthy_account`` +
    # ``_lifecycle._start_preflight._rotate_to_healthy_account``) — and
    # binds the PICKED file exactly as if it had been named in the singular
    # ``credentials_file`` field. Each entry's ACCOUNT SLUG is its parent
    # directory name (the fleet layout is
    # ``~/.scitex/agent-container/accounts/<slug>/.credentials.json``), and
    # that slug is the account name the quota-aware picker keys off. The
    # singular ``credentials_file`` remains supported and is treated as a
    # 1-element pool (pick returns it) for back-compat. Fail-loud: when NO
    # listed entry has a usable (non-expired) snapshot the start aborts with
    # ``_creds.NoHealthyAccountError``. Mutually exclusive with ``provider``
    # (an API-key backend needs no OAuth).
    credentials_files: list[str] = field(default_factory=list)
    # Vendor-agnostic backend override (see :class:`ProviderSpec`).
    # When set, the SDK session runs against an Anthropic-SDK-compatible
    # backend (DeepSeek, a gateway, ...) on an API key instead of
    # Anthropic OAuth. ``None`` = default Anthropic backend. Mutually
    # exclusive with ``account`` (an API-key backend needs no OAuth).
    provider: ProviderSpec | None = None

