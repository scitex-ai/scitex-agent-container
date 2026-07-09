"""``spec.claude`` block validation.

Extracted from ``_validation.py`` (which stays the orchestrator and
re-exports :data:`_VALID_MODEL_RE` for back-compat) to keep that module
under the 512-line cap — same pattern as the sibling
``_provider_validation`` / ``_acl_validation`` modules.

Covers the SDK-facing ``spec.claude`` fields the loader / runtime read:

  * ``model``       — accepted alias / versioned form (SDK silently
    rejects unknown values; see :data:`_VALID_MODEL_RE`).
  * ``provider``    — vendor-agnostic backend override (delegated to
    ``_provider_validation``).
  * ``account``     — mutually exclusive with ``provider``.
  * ``resume_id``   — when set, MUST be a well-formed UUID. A typo would
    otherwise silently degrade to a fresh session on the ``session:
    resume`` path (the SDK discards an unknown resume id), so we fail
    loud at validate time naming the bad value.
"""

from __future__ import annotations

import re
import uuid

from ._provider_validation import provider_is_active, validate_provider

# Accepted shapes for ``spec.claude.model`` (F-CS7).
#
# claude-agent-sdk silently rejects unknown aliases — the runner stays
# alive, the heartbeat is fresh, but every turn returns 0 input tokens
# and 0 output tokens because the SDK never makes the API call. Pin the
# validation here so the failure surfaces at yaml-validate time instead
# of as a hung-looking agent.
#
# Two acceptable shapes:
#   1. Bare alias: ``opus`` / ``sonnet`` / ``haiku`` / ``inherit`` /
#      ``default``, optionally with a context-suffix (``[1m]``).
#   2. Full versioned form: ``claude-<family>-N-M`` with optional date
#      tail (``-20251001``) and optional context-suffix.
#
# Reproduction (2026-05-05): ``claude-opus[1m]`` (abbreviated, missing
# the version digits) was accepted by the YAML loader but silently
# rejected by the SDK — every turn returned ``input_tokens=0``,
# ``output_tokens=0``, ``iterations=[]``. Other peers using
# ``claude-opus-4-7[1m]`` worked fine.
_VALID_MODEL_RE = re.compile(
    r"""
    ^(?:
        (?:opus|sonnet|haiku|fable|inherit|default)
        |
        # opus/sonnet/haiku ship as ``family-N-M`` (e.g. ``claude-opus-4-7``).
        # Fable is published as a single-digit family version
        # (``claude-fable-5``); see lead-confirmed 2026-06-12 (msg
        # 6172e53d ruling 1). The ``[1m]`` context-suffix is CLI-native
        # (proven empirically — msg 6f7e2f56) and bolted on below.
        claude-(?:
            (?:opus|sonnet|haiku)-\d+-\d+
            |
            fable-\d+
        )(?:-[a-z0-9]+)*
    )
    (?:\[[a-zA-Z0-9_]+\])?
    $
    """,
    re.VERBOSE,
)


def validate_claude(spec: dict) -> list[str]:
    """Return validation errors for the ``spec.claude`` block (empty = valid)."""
    errors: list[str] = []

    claude_block = spec.get("claude", {}) or {}
    if not isinstance(claude_block, dict):
        claude_block = {}

    # spec.claude.provider — vendor-agnostic backend override
    # (ProviderSpec). When present, the SDK session runs against an
    # Anthropic-SDK-compatible backend on an API key, so the model id is
    # the provider's own (e.g. 'deepseek-chat') and the claude-* regex
    # below is skipped. Absent → behaviour unchanged.
    provider_block = claude_block.get("provider")
    has_provider = provider_is_active(provider_block)
    errors.extend(validate_provider(provider_block))

    # spec.claude.model — F-CS7 (v3: moved from top-level spec.model).
    model = claude_block.get("model")
    if model is not None:
        if not isinstance(model, str):
            errors.append(
                f"spec.claude.model must be a string, got {type(model).__name__}"
            )
        elif model and not has_provider and not _VALID_MODEL_RE.match(model):
            errors.append(
                f"spec.claude.model '{model}' is not an accepted alias. "
                "Use a bare alias ('opus', 'sonnet', 'haiku', 'inherit', "
                "'default'), optionally with a context suffix like "
                "'opus[1m]'; OR the full versioned form "
                "'claude-<family>-N-M[-<tail>]' (e.g. 'claude-opus-4-7', "
                "'claude-opus-4-7[1m]', 'claude-haiku-4-5-20251001'). "
                "Abbreviated forms like 'claude-opus[1m]' are rejected "
                "by the SDK without raising — every turn returns 0 "
                "tokens. (When spec.claude.provider is set, the model "
                "field accepts the provider's own model id instead.)"
            )

    # spec.claude.provider + spec.claude.account are mutually exclusive —
    # an API-key backend needs no OAuth. Declaring both is a config error
    # (the runtime would otherwise have to guess which auth path wins).
    if has_provider and (claude_block.get("account") or ""):
        errors.append(
            "spec.claude.provider and spec.claude.account are mutually "
            "exclusive — a provider backend uses an API key, not "
            "Anthropic OAuth. Set exactly one."
        )

    # spec.claude.credentials_files — the account POOL (plural). Must be a
    # list of non-empty strings (host paths to ``.credentials.json``). The
    # start pre-flight picks ONE quota-aware; a malformed value would
    # silently degrade the pool to empty, so we fail loud here.
    cred_files = claude_block.get("credentials_files")
    if cred_files is not None:
        if not isinstance(cred_files, list):
            errors.append(
                "spec.claude.credentials_files must be a list of host paths "
                f"to .credentials.json files, got {type(cred_files).__name__}"
            )
        else:
            for i, entry in enumerate(cred_files):
                if not isinstance(entry, str) or not entry.strip():
                    errors.append(
                        "spec.claude.credentials_files[%d] must be a "
                        "non-empty string path, got %r" % (i, entry)
                    )
        # Mirror the account/provider exclusivity — a provider backend uses
        # an API key, not an OAuth credentials pool.
        if has_provider and cred_files:
            errors.append(
                "spec.claude.provider and spec.claude.credentials_files are "
                "mutually exclusive — a provider backend uses an API key, "
                "not an Anthropic OAuth credentials pool. Set exactly one."
            )

    # spec.claude.resume_id — the explicit session id passed to
    # ``claude --resume <id>`` (TUI) / ``ClaudeAgentOptions(resume=<id>)``
    # (SDK) when ``spec.claude.session: resume``. When set it MUST be a
    # well-formed UUID: the SDK/CLI resume mechanism keys off the on-disk
    # transcript uuid, and an unknown/malformed id is silently DISCARDED
    # to a fresh session (no error). A typo would therefore degrade the
    # pin invisibly, so we reject it loudly here naming the bad value.
    resume_id = claude_block.get("resume_id")
    if resume_id is not None:
        if not isinstance(resume_id, str):
            errors.append(
                "spec.claude.resume_id must be a string, got "
                f"{type(resume_id).__name__}"
            )
        elif resume_id.strip():
            try:
                uuid.UUID(resume_id.strip())
            except (ValueError, AttributeError, TypeError):
                errors.append(
                    f"spec.claude.resume_id '{resume_id}' is not a valid UUID. "
                    "It must be the session transcript's UUID (e.g. "
                    "'123e4567-e89b-12d3-a456-426614174000'); an unknown or "
                    "malformed id is silently discarded to a fresh session by "
                    "the SDK/CLI, so the pin would be lost without this check."
                )

    return errors


__all__ = ["_VALID_MODEL_RE", "validate_claude"]
