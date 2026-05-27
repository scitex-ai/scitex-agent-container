# ADR-0011 — DeepSeek / multi-provider auth-token location + resolution via scitex-config

## Status

Accepted (lands with PR #208 — `feat/deepseek-provider-override`).

## Context

sac agents need to run on Anthropic-SDK-compatible alternate providers
(DeepSeek first, gateway/self-hosted shapes later) for bulk fleet work
where Anthropic Max-plan quota / OAuth token refresh would otherwise be
the bottleneck. The provider override requires:

1. **One canonical on-disk home for the auth token** — operators must not
   have to set the same secret in multiple places, and sac must not
   surprise them with cwd-relative or hidden-dotfile lookups.
2. **A documented precedence cascade** — so a temporary `export` in the
   launch shell predictably overrides the on-disk value, and silent
   fallback to Anthropic on a missing key is impossible.
3. **Consistency with the SciTeX ecosystem** — packages across the
   ecosystem already use scitex-config's `direct → config → env → default`
   cascade; sac should follow the same convention, not invent a fourth
   one.

Earlier iterations of PR #208 hand-rolled a `to_home/.env` parser inside
`_apptainer_provider.py`. The operator's correction (msg 5325): "resolve
the provider auth token THROUGH scitex-config so precedence is consistent
with the ecosystem" — replace the hand-rolled parser with the canonical
ecosystem primitive.

## Decision

1. **Key location.** The DeepSeek API key (and any future provider key
   declared via `spec.claude.provider.auth_token_env`) lives in the agent's
   `to_home/.env`, which `runtimes/_to_home.py` materializes into the
   container's `$HOME/.env` at start (env-injection port 3 — the
   `to_home → $HOME` mirror; see ADR-0006 / ADR-0009). The host-side
   equivalent for the launch shell is `$HOME/.env` on the host running
   `sac agents start`.

2. **Resolution via scitex-config (public API).** `_apptainer_provider.py`
   resolves the token through the scitex-config public surface — no
   bespoke parser:

   ```python
   from pathlib import Path
   from scitex_config import PriorityConfig, load_dotenv

   load_dotenv(dotenv_path=str(Path.home() / ".env"))
   resolver = PriorityConfig(auto_uppercase=False)
   api_key = resolver.resolve(key=auth_token_env, default="")
   ```

   - `load_dotenv(dotenv_path=...)` merges the named file into
     `os.environ` **without overriding any already-set var**. The path
     is pinned to `$HOME/.env` on purpose: the library's default search
     order also reads `cwd/.env` first, which would be a surprise for
     an operator running `sac agents start` from an unrelated project
     dir.
   - `PriorityConfig(auto_uppercase=False).resolve(key=…)` reads from
     `os.environ` and applies the standard SciTeX cascade. We pass
     `auto_uppercase=False` because `auth_token_env` is already the
     literal env-var name declared by the spec.
   - `PriorityConfig` auto-masks `API_KEY` / `TOKEN` / `SECRET` style
     keys in its resolution log — sac never logs the key value.

3. **Precedence cascade (canonical).** From highest priority to lowest:

   1. **direct shell export** — `export DEEPSEEK_API_KEY=...` in the
      shell that runs `sac agents start`. Wins over everything.
   2. **`$HOME/.env`** — the operator's standard SciTeX dotenv location,
      loaded via `scitex_config.load_dotenv`.
   3. **default** — empty string, which trips the fail-loud branch.

   Note: scitex-config's full cascade signature is `direct → config →
   env → default`. In sac's call site `config` (YAML) is intentionally
   not used — API-key secrets do not belong in YAML — so the live
   layers reduce to `env (process env, populated by load_dotenv) →
   default`. The `direct` slot is reserved for a future caller-supplied
   override (e.g. a literal `auth_token` in the spec).

4. **Fail-loud, never silent.** If the cascade yields an empty string,
   `_apptainer_provider.provider_env_flags()` raises `ProviderEnvError`
   with a message that names BOTH sources the operator can set. There
   is no silent fallback to Anthropic — a missing key on a
   provider-active agent must crash the start, not 401 every turn with
   a fresh-looking heartbeat.

5. **Spec surface.** `spec.claude.provider: { base_url, auth_token_env }`
   declares the provider; `spec.claude.model` accepts the provider's
   own model name (e.g. `deepseek-v4-pro`, lowercase) once the
   provider block is present (the claude-* model alias check is
   relaxed under a provider override). `spec.claude.account` and
   `spec.claude.provider` remain mutually exclusive — an API-key
   backend uses no Anthropic OAuth.

## Consequences

### Positive

- **Single source of truth.** The operator sets the key once in
  `$HOME/.env` (or `to_home/.env` for in-container parity); sac switches
  every agent that names the right `auth_token_env`.
- **Ecosystem-consistent precedence.** scitex-config owns the cascade.
  No more re-implementation drift across packages — anyone reading
  another scitex package's code knows what to expect here.
- **Sensitive masking comes for free.** `PriorityConfig` recognises
  `*API*`, `*KEY*`, `*TOKEN*`, `*SECRET*`, etc., and masks the value in
  its resolution log without any sac-side bookkeeping.
- **Smaller surface to maintain.** Removed the hand-rolled
  `_parse_dotenv` / `_to_home_env` helpers from
  `_apptainer_provider.py` (−145 lines / +94 lines net delta in
  PR #208's third commit `6ddd2c9`).

### Negative / things to watch

- **Process-wide `os.environ` mutation.** `load_dotenv()` writes to
  the live process env (only keys not already set). For a sac process
  running multiple agents with DIFFERENT provider keys, all those keys
  must coexist in `$HOME/.env` with distinct env-var names (e.g.
  `DEEPSEEK_API_KEY_PROD`, `DEEPSEEK_API_KEY_DEV`). Per-agent secret
  isolation via separate `to_home/.env` files no longer applies to
  host-side provider resolution.
- **Secret hygiene is the operator's job.** The key is a real secret
  in a 0600 file. A leaked `$HOME/.env` is a leaked key — must be
  rotated. sac does not encrypt at rest.
- **scitex-config is now in `[project] dependencies`.** It was
  effectively a runtime dependency through `_ecosystem.local_state`
  already, but the public `PriorityConfig` + `load_dotenv` surface is
  used directly now. `pyproject.toml` declares `scitex-config>=0.3.0`
  (the release that split public from ecosystem-internal and exposed
  both surfaces).

## Alternatives considered

- **Hand-rolled `to_home/.env` parser inside `_apptainer_provider.py`**
  — was implemented in commit `33505eb` (the interim commit on PR #208),
  then superseded by the scitex-config refactor in `6ddd2c9` on operator
  guidance. Rejected because (a) two parsers reading two `.env` files
  for the same purpose is churn, (b) precedence rules would drift from
  the ecosystem standard, (c) sensitive-key masking would have to be
  re-implemented.

- **Bare `os.environ.get(auth_token_env)`** with no `.env` loading —
  the original v1 of `provider_env_flags()`. Rejected because it
  forces every operator to manage shell exports / dotfile sourcing
  manually, with no canonical on-disk home.

- **YAML config (`~/.scitex/sac.yaml` with provider keys)** via
  `scitex_config.ScitexConfig` — declined. Secrets in YAML files are
  a worse default than secrets in `chmod 0600 .env`. The `config`
  layer in scitex-config is left intentionally unused for this lookup.

- **Per-package mini-resolver** that re-exports the cascade as a sac
  helper — declined. Adds sac-specific API surface for no gain over
  calling scitex-config directly.

## Related work

- ADR-0006 — `to_home` materialization layout (the in-container side
  of the `.env` story).
- ADR-0009 — Claude setup delivery: `to_home` first.
- PR #208 — `feat/deepseek-provider-override` — implements this ADR.

## Sequencing notes

- WI-A (audit-failure fix, 1b) is in flight on its own branch and will
  land before #208's CI can go fully green.
- This ADR ships in PR #208 itself, not a standalone PR — the
  decisions described here ARE PR #208's decisions; they should land
  together so that a future reader hitting `git log` finds the rationale
  next to the implementation.
