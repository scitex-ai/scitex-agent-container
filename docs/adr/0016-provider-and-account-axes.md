# ADR: Provider × Account Axes for sac Agent Dispatch (2026-05-28)

## Status

Implemented for the Claude Code harness path. The backend registry now maps
`spec.claude.provider: codex` to the local scitex-genai Anthropic-compatible
gateway; scitex-genai owns Codex OAuth refresh, subscription-account rotation,
usage-aware scheduling, transport, and protocol translation.

## Context

sac (`scitex-agent-container`) currently has ONE knob for picking the LLM backing an agent: `spec.claude.model`. That conflates two orthogonal axes:

1. **Provider axis** — which LLM service the agent talks to (Anthropic / OpenAI / DeepSeek / Google / Groq / Llama / Xiaomi MiMo / etc.)
2. **Account axis** — which identity / subscription / billing entity within that provider (e.g., 3 separate Anthropic Max subscriptions, or distinct OpenAI org keys)

Today's hack: `handyman-deepseek` works by injecting `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic` + `ANTHROPIC_MODEL=deepseek-v4-pro` + `SAC_ANTHROPIC_API_KEY=$DEEPSEEK_API_KEY` via `spec.apptainer.env`, AND omitting `spec.claude.model` (sac's `_VALID_MODEL_RE` rejects `deepseek-*`). This works but is an env-injection hack that hides the structural decision.

Three problems with the current model:

1. **No principled multi-provider support**. Only Anthropic + env-hacked DeepSeek work today. Adding OpenAI/MiMo/Codex via litellm means more env-injection hacks per agent.
2. **No principled multi-account support within a provider**. An operator may
   have several subscription accounts; all agents otherwise bind to whichever
   credential file the host points at. Manual `spec.claude.account` exists but
   isn't wired into cross-provider dispatch / load-balancing.
3. **No quota visibility**. `sac accounts show` and per-agent `quota_5h_used_pct` both fail with "Failed to fetch or parse usage API response". Without quota data, even manual load balancing is blind.

## Decision

Introduce two orthogonal config fields on `spec.claude`:

```yaml
spec:
  claude:
    provider: anthropic   # one of: anthropic (default) / openai / deepseek / google / groq / mimo / ...
    account: alpha     # subscription/identity within provider; semantics depend on provider
    model: claude-opus-4-7[1m]   # always required; format depends on provider
```

Semantics by provider:

| Provider | account field | model field | Auth source |
|---|---|---|---|
| anthropic (default) | one of the stored Max accounts (`alpha-example-com` / `beta-example-com`) — picks `~/.claude/.credentials-<acct>.json` | claude-* model alias (validated by `_VALID_MODEL_RE`) | OAuth credential file (`:rw` bind, refresh-aware per skill 26) |
| openai | OpenAI org key alias (lookup table in sac config) | gpt-* / o*-* | API key from env (`OPENAI_API_KEY` or stored secret) |
| deepseek | "" (single account; no axis) | deepseek-v4-pro / deepseek-r1 | API key from env (`DEEPSEEK_API_KEY`) |
| google | Google org / project alias | gemini-* | API key from env (`GOOGLE_API_KEY`) |
| groq | "" | llama-* / mixtral-* | API key from env (`GROQ_API_KEY`) |
| mimo | "" | mimo-* | API key from env (`XIAOMI_MIMO_API_KEY`) |
| litellm | proxy URL alias | passthrough model name | proxy handles upstream auth |

`provider: anthropic` keeps the current behaviour exactly — no change for existing specs.

For non-Anthropic providers, sac's runtime:
1. Sets `ANTHROPIC_BASE_URL` to the provider's Anthropic-compatible endpoint (or a local litellm proxy)
2. Sets `ANTHROPIC_MODEL` to the provider's native model name
3. Sets `SAC_ANTHROPIC_API_KEY` from the provider's API key env var
4. Relaxes `_VALID_MODEL_RE` when `provider != anthropic`

The `spec.claude.model` field is always required and always self-describing of the actual model being used (no hidden indirection through env).

## Consequences

**What becomes possible**:

- `spec.claude.provider: deepseek` + `model: deepseek-v4-pro` replaces the current env-injection hack for `handyman-deepseek`. Same outcome, declarative.
- Multi-account load balancing on Anthropic via provider-qualified account IDs
  (one account per heavy agent, no quota collision).
- Per-task provider mix in a fleet: research subagents on Anthropic Opus, mechanical work on DeepSeek, vision on Google Gemini. Each declared in the spec.
- Future `sac dispatch --load-balance` helper can pick (provider, account) automatically based on quota_5h headroom (once quota visibility is restored — see Open Questions).

**What becomes harder** (or trade-offs):

- Two fields where there was one. Operators need to learn the axis split. Mitigated by `provider: anthropic` being the default.
- claude-agent-sdk is Anthropic-formatted; running other providers through it incurs prompt-format translation lossiness via litellm. Performance characteristics (tool-call format, JSON handling, system prompt compatibility) not measured — see Open Questions.

**What is ruled out**:

- A single "identity slot" combining (provider, account) into one string. Rejected because it doesn't separate axes — adding a new provider doesn't compose with existing per-account multi-instance dispatch.
- spec.claude.provider taking a full URL. Rejected because it leaks too much detail; the provider name is an enum, the URL is implementation-detail in a lookup table.

## Harness invariant

Claude Code remains the harness for every backend. Its TUI, flags, channels,
hooks, skills, and `CLAUDE.md` behavior are not translated into provider-native
harnesses. Backend switching happens below Claude Code by setting
`ANTHROPIC_BASE_URL`, authentication, and model environment variables through
`spec.claude.provider`.

The top-level `spec.provider` field is a different, unfinished SDK-family axis.
Selecting `spec.provider: openai` would replace the Claude harness and therefore
does not implement this decision.

The implemented Codex shape is:

```yaml
spec:
  claude:
    provider: codex
    model: gpt-5.6-sol
```

SAC resolves this to `http://127.0.0.1:18765` and reads the local-hop API key
from `SCITEX_GENAI_GATEWAY_API_KEY`. The gateway discovers provider-qualified
subscription accounts from `~/.scitex/agent-container/accounts/openai/`;
`SCITEX_GENAI_CODEX_HOMES` is an explicit override. Account selection is not
duplicated in each agent spec, and the normal rotation selector is invoked even
when the eligible account list contains one value.

## Existing adapter inventory

The following reusable components were confirmed in the local projects on
2026-07-19:

- sac already parses `spec.claude.provider` and injects an
  Anthropic-compatible endpoint into both SDK and TUI runtimes.
- OpenClaw implements `openai-codex` browser OAuth, refresh, provider-qualified
  profiles, usage retrieval, and the ChatGPT Codex Responses transport.
- Claude Relay Service manages OpenAI Responses/Codex accounts and converts
  OpenAI Chat Completions requests and responses to and from Codex Responses.
- Claude Relay Service's CCR account type forwards Anthropic Messages to an
  Anthropic-compatible upstream, but it does not convert Anthropic Messages to
  Codex Responses itself.
- scitex-genai implements the authenticated Anthropic Messages to Codex
  Responses gateway, including streaming/tool translation, OAuth refresh,
  usage polling, sticky least-loaded selection, and rate-limit failover.

The previously missing protocol link is therefore resolved. SAC selects the
registered `codex` backend and points Claude Code at scitex-genai; it does not
duplicate transport, credential refresh, or account-scheduling logic.

## Open questions

1. **Quota visibility regression**. `account_show` and `agent_status` both fail with "Failed to fetch or parse usage API response" (2026-05-28). Root cause unknown. **Blocks** automatic load-balancing dispatch. Task #16 tracks this.
2. **Performance/quality across providers**. claude-agent-sdk + litellm shim + DeepSeek/OpenAI/MiMo: tool-call format translation, system prompt compatibility, JSON-shape preservation are all untested. **Decision (operator 2026-05-28 msg 6735)**: skip the benchmark — cost-prohibitive. Rely on real-use signal: if handyman-deepseek works satisfactorily in practice we keep it; if not we replace.
3. **Account naming**. `alpha` vs `alpha@example.com` vs `alpha-example-com` — three slugs in flight. Standardize on dash-form (`alpha-example-com`) matching the existing `.credentials-<acct>.json` filenames.
4. **scitex-genai is the abstraction home** (operator decision 2026-05-28 msg 6734). Use the existing `src/scitex_genai/llm/_BaseGenAI.py` + per-provider modules as the unified abstraction layer. sac's `spec.claude.provider` enum maps to scitex-genai provider modules; sac is the consumer, scitex-genai is the producer. Avoids spinning up a new scitex-llm-proxy package.
5. **Failover and retries**. When account A hits 429, should the runtime auto-failover to account B same provider? Or just fail-fast and let the lead re-dispatch? Conservative default: fail-fast with structured error including current quota state. Operator can opt in to auto-failover per spec.
6. **earendil-works/pi (MIT)** (operator pointer 2026-05-28 msg 6734). Lightweight TUI for LLM use. Worth investigating as a reference implementation when building the scitex-genai consumer side — particularly how it handles subscription-account-vs-API-key dispatch (the same axis split this ADR captures).

## Implementation outline (not in this ADR)

Four PRs, in order:

1. **PR-1** — restore quota visibility (`sac accounts show` + `agent_status.quota_5h_used_pct`). Likely a small parser fix; the OAuth token is fresh, just the API response shape changed. Task #16.
2. **PR-2** (scitex-genai) — add `BaseGenAI.serve()` method that exposes a provider as an Anthropic-compatible local HTTP endpoint (litellm-backed under the hood). `scitex-genai serve --provider deepseek --port 11434` style.
3. **PR-3** (sac) — introduce `spec.claude.provider` field with `anthropic` default. Backwards-compatible. Validate provider enum. Update `_VALID_MODEL_RE` to skip when `provider != anthropic`. Reference scitex-genai's provider list as the canonical enum.
4. **PR-4** (sac) — `sac dispatch --load-balance` CLI helper that picks the freshest-quota agent across the fleet. Optional / future.

Each PR independently mergeable. PR-3 unlocks declarative `handyman-deepseek` (migrate that spec away from env-injection in a follow-up).

## Cross-references

- [skill 26 SAC OAuth credentials](https://github.com/ywatanabe1989/scitex-agent-container/blob/develop/src/scitex_agent_container/_skills/scitex-agent-container/26_credentials-rotation.md) — per-account credentials store + COPY caveat
- [skill 27 credentials re-login](https://github.com/ywatanabe1989/scitex-agent-container/blob/develop/src/scitex_agent_container/_skills/scitex-agent-container/27_credentials-relogin.md) — operator-in-loop flow
- [scitex-genai llm submodule](https://github.com/ywatanabe1989/scitex-genai/tree/develop/src/scitex_genai/llm) — existing provider abstractions (Anthropic / OpenAI / DeepSeek / Google / Groq / Llama / Perplexity) — candidate home for the litellm proxy if we go that route
- Telegram conversation 2026-05-28 msg 6713 → 6729 → 6731 — operator's exploratory framing of the 2-axis model
