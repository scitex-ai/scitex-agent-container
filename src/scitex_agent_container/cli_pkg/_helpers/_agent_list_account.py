"""Account-column resolution for the agent list.

Extracted from :mod:`_agent_list` (which sits at the 512-line per-file
cap) so the two Account-column resolvers live together in one focused
module. :mod:`_agent_list` re-imports both names so existing
``_al._safe_account_for`` / ``_al._runtime_account_for`` access (and the
test swap seams that rebind them) keep working unchanged.

Two resolvers, used in tandem by ``get_agent_list_data``:

* :func:`_safe_account_for` — the SPEC-derived label (env override →
  ``spec.claude.account`` pin → host shared OAuth identity). This is what
  every row showed before; for a pool-based agent (no ``account`` pin) it
  collapses to the one host OAuth email, so the whole column reads the
  same account.
* :func:`_runtime_account_for` — the ACTUAL account a RUNNING agent is
  authenticated as, read from its own ``<runtime>/home/.claude.json``.
  This is what makes the column reflect the per-agent load-balanced pick.
"""

from __future__ import annotations


def _safe_account_for(cfg) -> str:
    """Resolve the agent's effective Anthropic-account label.

    Surfaces which account the agent authenticates as (operator request
    4581) so the operator can spot agents sharing one account — and thus
    one server-side rate limit. Resolution mirrors the runtime auth
    precedence: agent ``spec.env`` override → host shared OAuth identity
    → ``default``/``unknown`` fallback. See
    ``_account.agent_account.resolve_agent_account_label`` for the rule.

    Tolerant: a missing config or any resolver hiccup maps to
    ``"unknown"`` so the list command never crashes on account lookup.
    """
    # stx-allow: fallback (reason: list output must never crash on an
    # account-resolution hiccup; ``"unknown"`` cell is the right UX.)
    try:
        from ..._account.agent_account import resolve_agent_account_label

        env = getattr(cfg, "env", None) if cfg is not None else None
        assigned = (
            getattr(getattr(cfg, "claude", None), "account", "") or None
            if cfg is not None
            else None
        )
        return resolve_agent_account_label(env, assigned_account=assigned)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return "unknown"


def _runtime_account_for(name: str) -> str | None:
    """Resolve the ACTUAL Anthropic account a RUNNING agent uses, or None.

    :func:`_safe_account_for` reads the agent's SPEC, which for the fleet's
    pool-based agents (``spec.claude.credentials_files`` with no singular
    ``spec.claude.account`` pin) has no account to resolve — so it falls
    through to the HOST's shared OAuth identity, and EVERY such agent reads
    as the same account (the operator's TG-1490 complaint: every row shows
    one email). But the runtime quota-aware picker
    (:func:`_creds.pick_healthy_account`, ``spread_key=<agent>``) binds a
    DIFFERENT pool account per agent at container start, so the live pick
    genuinely VARIES. Claude Code writes that picked account's identity
    into the agent's OWN ``<runtime>/home/.claude.json`` (``oauthAccount.
    emailAddress``), which is host-readable through the per-agent
    runtime-home bind. This reads THAT so the Account column reflects what
    a live agent is really authenticated as.

    Returns the resolved account label, or ``None`` when no per-agent
    runtime identity is resolvable (agent never started, no runtime dir,
    or auth not yet written) — the caller then falls back to the
    spec-derived :func:`_safe_account_for` label. Never raises.
    """
    # stx-allow: fallback (reason: the runtime-account probe is best-effort
    # enrichment for the list column; ANY resolution hiccup degrades to
    # None so the caller uses the spec label — it must never crash the list.)
    try:
        from pathlib import Path

        from ..._account.agent_account import _match_saved_account
        from ..._account.credentials import read_credentials_metadata
        from ..._lifecycle._session_movement import resolve_state_dir

        state_dir = resolve_state_dir(name)
        if state_dir is None:
            return None
        # The picked account's identity lives in the agent's OWN
        # ``<runtime>/home/.claude.json`` (oauthAccount), read directly here.
        runtime_home = state_dir / "home"
        if not (runtime_home / ".claude.json").is_file():
            return None
        email = read_credentials_metadata(home=runtime_home).get("email_address")
        if not (isinstance(email, str) and email):
            return None
        # Match the email against the HOST account store so the label form
        # (``<slug> (<email>)``) is consistent with the spec-derived rows
        # (``_safe_account_for``); a non-matching email shows bare.
        saved = _match_saved_account(email, Path.home(), None)
        return f"{saved} ({email})" if saved else email
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None
