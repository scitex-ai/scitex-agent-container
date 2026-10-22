"""Operator secret names passed through into the agent profile secret scope.

The Hermes profile ``.env`` is the agent's secret scope: the values here
reach the agent runtime but never the logs. This module owns the NAME
allowlist; values come from the trusted pool at launch and are never
logged — see :func:`operator_secret_env`.
"""

from __future__ import annotations

from typing import Sequence

import scitex_logging as slogging

from ._secret_pool import read_pool

#: Operator secret names passed through into the agent profile secret scope.
#: PROPOSED set for operator ack (see sac-agent-full-permissions-20261020):
#: generic provider keys + the Cloudflare pair, resolved by NAME from the
#: trusted pool at launch. App-scoped keys (NEWB_*, *_TWITTER_*, ZOTERO_*,
#: SAC_*, SCITEX_SCHOLAR_*, SCITEX_AUDIO_*, SCITEX_HUB_CAMPAIGN_*,
#: SCITEX_GENAI_GATEWAY_*) and bearer tokens (GITHUB/HUGGINGFACE/CODECOV)
#: are deliberately NOT here. Only NAMES live in code and logs -- values
#: come from the pool at runtime and are never logged.
OPERATOR_SECRET_ENV_NAMES: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "BRAVE_API_KEY",
    "COMMANDCODE_API_KEY",
    "DEEPSEEK_API_KEY",
    "ELEVENLABS_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "LLAMA_API_KEY",
    "OPENAI_API_KEY",
    "OPENCODE_GO_API_KEY",
    "OPENCODE_ZEN_API_KEY",
    "PERPLEXITY_API_KEY",
    "SEMANTIC_SCHOLAR_API_KEY",
    "XIAOMI_API_KEY",
    "SCITEX_CLOUDFLARE_API_KEY",
    "SCITEX_CLOUDFLARE_EMAIL",
)

log = slogging.getLogger(__name__)


def scope_allows_full(scope: str) -> bool:
    """True when ``scope`` (a spec ``secret_scope`` value) means full pool.

    Fail-loud on unknown values: silently treating a typo as limited
    would starve a dev agent, and silently treating it as full would
    over-share into a capsule. The loader rejects unknown spellings;
    this is the belt for programmatically built configs.
    """
    normalized = (scope or "").strip().lower()
    if normalized == "full":
        return True
    if normalized == "capsule":
        return False
    raise ValueError(f"unknown secret scope {scope!r}; expected 'full' or 'capsule'")


def operator_secret_env(
    names: Sequence[str] | None = None,
    *,
    full: bool = False,
) -> dict[str, str]:
    """Pass the operator secret set into the agent profile secret scope.

    ``names`` defaults to :data:`OPERATOR_SECRET_ENV_NAMES`; tests pass
    fake names explicitly so no test doubles as a secrets inventory.
    With ``full=True`` every pool-FILE-defined name is passed (the
    development-agent tier); otherwise only the allowlist (the capsule
    tier). Ambient-only names are never passed in either mode: the
    hermetic profile tests pin ``SAC_SECRETS_ENVRC`` to an empty file and
    would break on ambient leakage — full means file-defined, not
    environment-inherited.
    Values are resolved from the trusted pool at launch (:func:`read_pool`).
    Present slots are returned; missing slots are warned about BY NAME and
    an untrusted pool read yields {} (a miss would be inconclusive).
    Values are never logged.
    """
    from ._envrc import pool_defined_names

    if names is None:
        wanted: Sequence[str] = (
            sorted(set(OPERATOR_SECRET_ENV_NAMES) | set(pool_defined_names()))
            if full
            else OPERATOR_SECRET_ENV_NAMES
        )
    else:
        wanted = names
    pool = read_pool()
    if not pool.trusted:
        log.warning(
            "operator secrets: pool read untrusted (%s); passing nothing",
            pool.detail,
        )
        return {}
    out: dict[str, str] = {}
    missing: list[str] = []
    for name in wanted:
        value = pool.env.get(name, "")
        if value:
            out[name] = value
        else:
            missing.append(name)
    if missing:
        log.warning(
            "operator secrets: %d of %d slots missing from the pool: %s",
            len(missing),
            len(wanted),
            ", ".join(missing),
        )
    log.info(
        "operator secrets: passing %d of %d slots into the profile",
        len(out),
        len(wanted),
    )
    return out
