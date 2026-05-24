"""Resolve which Anthropic account an agent effectively uses.

Fleet-wide account *visibility* (operator request 4581): surface, per
agent, the identity of the Anthropic account it authenticates as, so the
operator can see at a glance which agents share one account — and thus
which agents collide on the same server-side rate limit (429).

Mechanism (mirrors ``runtimes/_sdk_common.py::provision_anthropic_auth``,
the runtime auth precedence):

1. **Agent env override.** If the agent's effective env carries
   ``SAC_ANTHROPIC_API_KEY`` (declared in ``spec.apptainer.env`` and
   promoted by the v3 loader into ``AgentConfig.env``), that is a
   *distinct* credential source from the host's shared OAuth file. We
   label it by the key form:

   * ``sk-ant-api*`` → pay-per-token API key →
     ``apikey:…<last4>`` fingerprint (no identity is recoverable from
     the key alone, so the last 4 chars are the discriminator).
   * ``sk-ant-oat*`` → an OAuth bearer handed in via env (rare; used on
     hosts that deliberately have no credentials.json) → ``sac-env``
     (a label, not the secret — the token is never surfaced).
   * any other / unparseable value → ``sac-env``.

2. **Host OAuth (the common case).** No env override → the agent uses
   the host's ``~/.claude/.credentials.json`` (bind-mounted into every
   container today; there is no per-agent credential-file override yet
   — that is part 1 of the multi-account set, deferred). The identity
   is the ``oauthAccount.emailAddress`` from ``~/.claude.json``. If that
   email matches a saved account from ``sac accounts``, we prefer the
   named-account label (``<name> (<email>)``); else just ``<email>``.

3. **Fallbacks (never crash).**

   * credentials.json present but no resolvable email → ``default``.
   * neither a credentials file nor an env key → ``unknown``.

Design rules
------------
* Never raises. A resolution failure maps to ``unknown``/``default``,
  never an exception that would break ``sac agents list``.
* Token material is never returned. Only an email, a saved-account
  name, a key fingerprint (last 4), or a coarse label string.
* Pure stdlib + the existing ``credentials`` / ``account_store``
  readers. No new dependency, no network call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_SAC_API_KEY_ENV = "SAC_ANTHROPIC_API_KEY"

# Resolution result labels for the non-identity cases.
_LABEL_UNKNOWN = "unknown"
_LABEL_DEFAULT = "default"
_LABEL_SAC_ENV = "sac-env"


def _env_key_label(value: str) -> str:
    """Label for an agent that overrides auth via ``SAC_ANTHROPIC_API_KEY``.

    ``sk-ant-api*`` (pay-per-token) → ``apikey:…<last4>`` so two agents
    on two distinct API keys read as distinct accounts. Anything else
    (OAuth bearer or unparseable) → ``sac-env`` — a coarse "this agent
    brings its own credential" marker. The raw value is never surfaced.
    """
    val = value.strip()
    if val.startswith("sk-ant-api"):
        last4 = val[-4:] if len(val) >= 4 else val
        return f"apikey:…{last4}"
    return _LABEL_SAC_ENV


def _cred_file(home: Path) -> Path:
    """The credentials.json path the SDK would read for the HOST account.

    Mirrors ``_sdk_common._cred_file_path`` *for the resolver's vantage*:
    we always reason about the host operator's ``~/.claude/`` here, not a
    container's ``CLAUDE_CONFIG_DIR`` — the list/status command runs on
    the host and every agent today shares this one file.
    """
    return home / ".claude" / ".credentials.json"


def _match_saved_account(
    email: str,
    home: Path,
    store_dir: Path | None,
) -> str | None:
    """Return the saved-account NAME whose email matches ``email``, or None.

    Best-effort: any failure listing the store maps to ``None`` (we just
    fall back to showing the bare email). Never raises.
    """
    # stx-allow: fallback (reason: account-store read is best-effort enrichment; a missing/corrupt store must degrade to the bare-email label, never break list/status)
    try:
        from .._state.account_store import list_accounts

        for acct in list_accounts(store_dir=store_dir, home=home):
            if acct.get("email_address") == email:
                name = acct.get("name")
                if isinstance(name, str) and name:
                    return name
    except Exception:  # stx-allow: fallback (reason: catch-all — see inline comment)
        return None
    return None


def resolve_agent_account_label(
    env: dict[str, str] | None,
    home: Path | None = None,
    store_dir: Path | None = None,
) -> str:
    """Resolve a short, human-readable account label for one agent.

    Args:
        env: The agent's effective env dict (``AgentConfig.env`` — the v3
            loader promotes ``spec.apptainer.env`` into it). Used to
            detect a ``SAC_ANTHROPIC_API_KEY`` override. ``None`` / empty
            means the agent inherits the host's shared auth.
        home: Override for the user home directory (defaults to
            ``Path.home()``). The host ``~/.claude.json`` /
            ``~/.claude/.credentials.json`` under it are the source of
            truth for the shared-OAuth identity. Used by tests.
        store_dir: Override for the saved-accounts store directory
            (defaults to the SciTeX local-state cascade). Used by tests.

    Returns:
        One of:

        * ``apikey:…<last4>`` — agent brings its own API key.
        * ``sac-env`` — agent brings its own (non-API-key) env credential.
        * ``<name> (<email>)`` — host OAuth, matched to a saved account.
        * ``<email>`` — host OAuth, no matching saved account.
        * ``default`` — credentials.json present but no email resolvable.
        * ``unknown`` — no credentials file and no env override.

    Never raises.
    """
    _home = Path(home) if home is not None else Path.home()

    # 1. Agent-level env override wins (distinct credential source).
    env = env or {}
    override = env.get(_SAC_API_KEY_ENV)
    if isinstance(override, str) and override.strip():
        return _env_key_label(override)

    # 2. Host shared OAuth — resolve identity from the credentials file.
    if not _cred_file(_home).is_file():
        return _LABEL_UNKNOWN

    # stx-allow: fallback (reason: credentials-metadata read is best-effort identity enrichment; a corrupt/partial ~/.claude.json must degrade to the "default" label, never break list/status)
    try:
        from .credentials import read_credentials_metadata

        meta: dict[str, Any] = read_credentials_metadata(home=_home)
    except Exception:  # stx-allow: fallback (reason: catch-all — see inline comment)
        return _LABEL_DEFAULT

    email = meta.get("email_address")
    if not (isinstance(email, str) and email):
        return _LABEL_DEFAULT

    saved_name = _match_saved_account(email, _home, store_dir)
    if saved_name:
        return f"{saved_name} ({email})"
    return email
