# Provider credential and account files

`sac accounts list` treats the provider as part of account identity. For
example, `claude-code:person-example-com` and
`openai:person-example-com` are distinct accounts even when their email-derived
slugs match.

## OpenAI Codex

Run `sac accounts sync-openai` to collect the active Codex login into
`~/.scitex/agent-container/accounts/openai/<account-slug>/auth.json`. The
provider directory is part of storage identity, so `openai:<account-slug>`
cannot collide with the legacy Claude store at `accounts/<account-slug>/`.
The source login is read from `$CODEX_HOME/auth.json`, falling back to
`~/.codex/auth.json`. For ChatGPT login mode, SAC decodes display-only claims
from the local ID token: email, display name, ChatGPT account ID, plan,
organization, subscription dates, and last refresh time. This decoding is for
status display only and is never used as an authorization decision.

The extractor returns an explicit allowlist. It never returns the ID, access,
or refresh token, and never returns `OPENAI_API_KEY`. API-key login mode shows
only that the mode is configured because no user identity claims are available.

OpenAI contributes every collected Codex identity to the combined account
view. `SCITEX_GENAI_CODEX_HOMES` remains an explicit override. Rotation of
OpenAI accounts is performed by the gateway; Claude Code OAuth rotation
remains SAC-managed. The gateway always invokes its rotation selector,
including when the candidate list contains exactly one account.

### Claude Code harness with Codex subscriptions

SAC uses the nested `spec.claude.provider` backend axis for this mode. Do not
set the top-level `spec.provider: openai`: that selects the OpenAI Agents SDK
and replaces the Claude Code harness.

Start the scitex-genai gateway with its account homes and a local gateway key:

```bash
pip install 'scitex-agent-container[codex]'
sac accounts sync-openai
export SCITEX_GENAI_GATEWAY_API_KEY="$(openssl rand -hex 32)"
scitex-genai-gateway --host 127.0.0.1 --port 18765
```

Then declare the backend in an agent spec:

```yaml
spec:
  # Omit top-level provider, or keep its default `anthropic` value.
  claude:
    provider: codex
    model: gpt-5.6-sol
    flags:
      - --dangerously-skip-permissions
      - --effort=medium
```

The gateway discovers the collected Codex homes, keeps sessions sticky, ranks
accounts by available usage-window headroom, spreads concurrent sessions, and
rotates away from temporary rate limits. Therefore Codex account files are
configured on the gateway, not repeated in each agent's
`claude.credentials_files`. The gateway key authenticates only the local SAC
to gateway hop; Codex OAuth tokens remain in each `auth.json`.

This document describes the on-disk files Claude Code manages for a user
and which fields `scitex-agent-container` is allowed to read and surface.

## Claude Code files

### `~/.claude.json`

The main per-user settings JSON managed by Claude Code itself. Top-level
keys relevant to agent orchestration:

- `oauthAccount` (subdict): `accountUuid`, `emailAddress`, `organizationUuid`,
  `organizationName`, `billingType`, `accountCreatedAt`,
  `subscriptionCreatedAt`, `hasExtraUsageEnabled`, `displayName`,
  `organizationRole`.
- `hasAvailableSubscription` (bool)
- `cachedExtraUsageDisabledReason` (str, e.g. `"out_of_credits"`)
- `overageCreditGrantCache` (obj)
- `numStartups` (int)
- `installMethod` (str)
- `claudeCodeFirstTokenDate` (str)
- `firstStartTime` (str)
- `hasCompletedOnboarding` (bool)
- `passesEligibilityCache` (obj)
- `changelogLastFetched` (str)
- `lastReleaseNotesSeen` (str)
- `skillUsage` (obj)

Any other keys (model caches, feature flags, editor state, MCP server
definitions, per-project history) are considered opaque and MUST NOT be
surfaced by our tooling.

### `~/.claude/.credentials.json`

OAuth tokens for Claude.ai. Contains (inside a `claudeAiOauth` subdict):
`accessToken`, `refreshToken`, `expiresAt`, `scopes`, `subscriptionType`,
`rateLimitTier`.

**RULE: this file MUST NEVER be read or emitted by scitex-agent-container
tooling except for the non-secret strings `subscriptionType` and
`rateLimitTier`.** The extractor must not load, log, cache, or transmit
any other field from this file. Tokens are the highest-sensitivity
material on the host.

### `~/.claude/settings.json`

Per-user Claude Code settings. Common keys: `permissions`, `statusLine`
(command used to render the bottom bar, often claude-hud),
`enabledPlugins`. Contains no secrets but may reveal which plugins /
skills are enabled.

## Fleet hosts

Each fleet host runs exactly one Claude Code OAuth identity shared by
all tmux-managed agents on that host:

| Host              | Domain role            | Credential home |
|-------------------|------------------------|-----------------|
| MBA               | fleet hub              | `~/.claude/`    |
| NAS               | scitex.ai              | `~/.claude/`    |
| spartan           | GPU worker             | `~/.claude/`    |
| ywata-note-win    | Windows/WSL            | `~/.claude/`    |

All tmux panes on a host inherit the same `~/.claude.json` +
`~/.credentials.json`, so any head-agent view of "Claude account" is
per-host, not per-agent.

## What NOT to emit

The extraction layer MUST strip any field whose key or stringified value
contains any of these substrings (case-insensitive):

- `accessToken`
- `refreshToken`
- `sk-ant-`
- `Bearer ` (with trailing space)
- `secret`
- `apiKey`
- `claudeAiOauth`

A post-extraction guard asserts the returned dict contains none of the
above in either keys or values, and raises if violated.

## Safe metadata fields (whitelist)

`read_credentials_metadata()` returns a flat dict with exactly these
keys. Fields unavailable on disk are returned as `None`.

From `~/.claude.json` `oauthAccount`:

- `account_uuid`
- `email_address`
- `organization_uuid`
- `organization_name`
- `billing_type`
- `account_created_at`
- `subscription_created_at`
- `has_extra_usage_enabled`
- `display_name`
- `organization_role`

From `~/.claude.json` top level:

- `has_available_subscription`
- `cached_extra_usage_disabled_reason`
- `num_startups`
- `install_method`
- `claude_code_first_token_date`
- `first_start_time`
- `has_completed_onboarding`

From `~/.claude/.credentials.json` `claudeAiOauth` (only these two):

- `subscription_type`
- `rate_limit_tier`

From `~/.claude/settings.json`:

- `status_line_command`
- `enabled_plugins`

Any addition to this list requires updating both this doc and the
whitelist in `src/scitex_agent_container/credentials.py`.
