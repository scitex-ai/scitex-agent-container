# ADR-0024 — Named engines in one spec, switched at start

Status: accepted (2026-09-03)

## Context

A sac spec can declare exactly ONE backend. It is spread over three keys that
already exist and already work:

* `spec.harness` — WHICH AGENT PROGRAM runs the loop (`anthropic` = the
  claude-agent-sdk, `openai` = the openai-agents SDK). See
  `config/_harness_types.py`, which retired the `spec.provider` naming
  collision and is the design precedent for everything below.
* `spec.claude.provider` — WHICH INFERENCE ENDPOINT answers: an inline
  `{base_url, auth_token_env}` dict, or a NAME resolved from the registry in
  `config/_provider_registry.py`.
* `spec.claude.model` — the model id passed to that endpoint.

They compose, and a live agent already spans backends this way:
`handyman-01` runs model `qwen38-27b` through an inline provider on the
`anthropic` harness.

What none of it can do is write down TWO backends and pick one. Switching an
agent between Claude and a local Qwen means editing the spec, which is a
different action from starting the agent, needs a different set of hands, and
leaves no record of what the agent was actually started on.

The operator asked for the switch to be an argument (Telegram 2026-09-02):

```
sac agents restart <agent> --engine qwen-3.8-27b
```

## The four operator answers (2026-09-03 06:34–06:38Z)

These were answers to four direct questions and are settled; they are recorded
here because the implementation is only legible against them.

**Q1 — vocabulary.** The words are **ENGINE** and **HARNESS**. `claude:` was
already the wrong name the moment it held Qwen — a vendor name is a claim
about scope. So the multi-backend surface is `engines:`, and one entry is an
ENGINE: a named (harness, model, provider, parameters) tuple.

**Q2 — switching granularity.** **START TIME ONLY** —「起動時だけで大丈夫です」.
No per-turn escape hatch, no mid-session rebinding.

**Q3 — an engine that cannot be honoured.** **REFUSE TO START**, naming what
could not be honoured —「勝手なフォールバックはしないと言うルールなので」. The
spec value is the default, a CLI argument may override it, and nothing
silently falls back.

**Q4 — per-engine parameters.** **YES**, parameters per model,
`reasoning_effort` above all; Qwen is expected to run at low reasoning effort
permanently.

## Decision

```yaml
spec:
  engines:
    claude:
      harness: anthropic
      model: fable[1m]
      provider: anthropic
      default: true
    qwen38-27b:
      harness: anthropic
      model: qwen38-27b
      provider:
        base_url: http://127.0.0.1:18772
        auth_token_env: QWEN_GATEWAY_API_KEY
      reasoning_effort: low
      max_context_tokens: 393216
```

**One vocabulary, not two.** An engine entry carries the SAME fields the
single-backend surface carries. `harness` resolves through the existing
`_harness_registry` (not a second list of harnesses); `provider` folds through
the same parser `spec.claude.provider` uses (`config/_provider_parse.py`, now
shared by both) and is checked by the same `validate_provider`; `model` is the
same model id. Only `default:`, `reasoning_effort`, `max_context_tokens` and
`env:` are new words, because they name things the old surface could not say.

**Exactly one default.** With a single entry it is the default implicitly. Two
entries marked `default: true` is a hard error naming both. Two entries with
NO default is also a hard error: taking the first would make the default
depend on YAML ordering, which is a guess wearing a convention.

**Selection.** `sac agents start|restart <name> --engine <key>` selects a
different entry for THAT start. An unknown key is a hard error listing the
declared keys — it never degrades to the default, because starting on a
different backend than the one named is exactly the silent fallback Q3 rules
out.

**Refusal.** When the selected engine cannot be honoured — the provider name
is not in the registry, the inline dict is incomplete, the `auth_token_env`
variable is unset on this host, the harness is unknown — the start FAILS with
a message naming the engine key, HOW it was selected (explicit `--engine`, or
the spec's default, so the operator knows which line to edit), what was
unhonourable, and the fix.

## Reachability: static always, live probe opt-in

This is a deliberate choice and it is stated in the `--engine` help, in
`config/_engine_honour.py`, and here, because an operator has to be able to
predict which failures refuse a start.

There are THREE states and they stay distinguishable:

| verdict | meaning | effect on the start |
|---|---|---|
| `honourable` | every declared piece resolves | starts |
| `not-honourable` | a piece is DEFINITELY wrong | **REFUSES** |
| `could-not-tell` | the probe reached no verdict | **starts, with a LOUD warning** |

* **Static resolution runs on EVERY start** and is the whole refusal surface by
  default. It reads the spec and the host environment — no sockets — so
  everything it rejects is a fact about the declaration and cannot flap.
* **The live probe is opt-in** (`--probe-engine`, or `SAC_ENGINE_PROBE=1`), on
  a short bounded timeout. Only an ACTIVE connection refusal — a closed port,
  which is a definite answer — may refuse a start. A timeout or DNS failure is
  `could-not-tell`: the start proceeds, and says so loudly.

The reason for the asymmetry is on the record already:
`hub-cards-dsn-unreachable-should-refuse-to-boot-20260815` notes that refusing
on an unreachable dependency can ground the fleet. If every start dialled a
possibly-remote endpoint, one ten-second network blip would refuse every agent
that restarted in that window. But `could-not-tell` is never silently read as
`honourable` — the warning is the difference between "proceeding despite not
knowing" and "pretending to know".

## Migration, not rename

123 agent specs on compute-04 alone are written with the existing single-backend
block. A hard rename boot-reds every one of them, so this follows the rule
`_harness_types.py` already implements for `harness`/`provider`, applied to a
BLOCK instead of a key:

* **legacy block alone** → works unchanged, and SILENTLY. No deprecation line:
  at ~123 specs a nudge-per-start is noise.
* **`engines:` alone** → the new path.
* **BOTH, agreeing** → accepted, silently. The author has migrated and kept the
  legacy block for an older sac; nothing is ambiguous.
* **BOTH, disagreeing** → HARD ERROR naming both values and both YAML paths.
  Picking one silently would let a spec claim one backend and run another.

The comparison targets the DEFAULT engine, because that is the backend the
spec runs on with no `--engine` — the one the legacy block claims to describe.
A NON-default engine naturally differs; that is the entire point of declaring
several, so it is not compared. Written-but-empty (`model: ""`, `harness: ~`)
states no opinion and never manufactures a conflict, and a provider spelled as
a registry NAME compares equal to a dict that copy-pasted that registry entry.

**`engines:` is OPTIONAL in the explicit-fields map**, deliberately: the ~123
deployed specs predate the axis, and requiring it would red-start every one of
them for declaring nothing new — the posture `residency` and `to_home_layers`
already took. The per-engine parameters live on `AgentConfig`, not on
`ClaudeSpec`, for the same reason: `_explicit_fields._claude_fields()` derives
EVERY `ClaudeSpec` field as a required YAML key.

### The migration has an END

It closes when **every deployed spec declares `engines:`**. At that point the
legacy single-backend READING is deleted — `spec.harness` +
`spec.claude.model` + `spec.claude.provider` stop being read as a backend
declaration — and `legacy_conflict_messages()` goes with them. The condition is
recorded in code as `_engine_types.MIGRATION_END_CONDITION` so it is a
condition and not an open-ended "someday". Rolling the specs over is a
separate, dry-run-first sweep; this ADR's PR ships the mechanism only and
changes no live spec.

## Where each diagnostic fires

| decided by | example | fires at |
|---|---|---|
| the spec TEXT | two defaults, no default, unknown harness, malformed provider, legacy disagreement | LOAD (`validate_raw`) |
| the CLI argument | unknown `--engine` key | START |
| the HOST | `$API_KEY` unset, endpoint does not answer | START |

Nothing host-dependent runs in the loader. `sac agents list` loads every spec
on the machine: a loader that resolved keys per spec, or dialled each declared
endpoint, would answer a question nobody asked once per spec and would
contaminate `--json`. This is the same ruling that placed
`warn_if_legacy_harness_key` and `warn_if_legacy_apptainer_runtime` on the
start path.

## What per-engine parameters actually do

`reasoning_effort` and `max_context_tokens` are delivered into the container as
`SAC_ENGINE_REASONING_EFFORT` / `SAC_ENGINE_MAX_CONTEXT_TOKENS`, alongside
`SAC_ENGINE=<key>` for provenance (see
`runtimes/_apptainer_provider.engine_env_flags`, emitted on EVERY auth branch
so an engine without a provider override does not lose them).

sac's contract stops at delivering the declaration under a name that says
where it came from. Whether the in-container harness ACTS on it is the
harness's business, and sac does not claim otherwise — which is why these are
`SAC_`-prefixed rather than dressed up as a vendor env var
(`MAX_THINKING_TOKENS`, say) that would imply a mapping nobody has measured.
Saying so is the point: a field that VALIDATES is not a field that RUNS, and a
green test on `engine_env_flags` proves delivery, not effect. Wiring a measured
mapping to a specific harness knob is a separate, testable change.

An engine's own `env:` map is the escape hatch in the meantime: it merges over
`spec.apptainer.env` and reaches the container through the normal env path, so
an operator can spell a gateway's real knob today.

## Consequences

**What becomes possible**

* One spec declares Claude and a local Qwen; `--engine` picks per start.
* The engine an agent was started on is recorded — in the container env, and in
  the birth certificate's compiled-spec snapshot via `AgentConfig.engine_key`.
* Per-model parameters are declared beside the model they belong to instead of
  being duplicated into `spec.apptainer.env` per agent.

**What is harder**

* A spec can now be wrong in a new way (two defaults, none, a legacy block that
  disagrees). All three are LOAD errors with messages naming both offending
  values, which is the trade the explicit-spec ruling already accepts.

**What is NOT covered by this PR** — each fails loud rather than dropping the
engine silently:

* `--engine` from INSIDE a container. `agent_start` brokers to the host's
  `sac listen`, whose POST body threads `force`/`foreground`/`one_shot`/
  `assume_yes` explicitly and drops anything else. Threading an engine through
  the body, the `/agents` handler and its argv builder is the follow-up; until
  then an in-SIF `--engine` refuses, naming the host command.
* `--engine` for an agent that lives on a PEER. The cross-host restart re-runs
  `sac agents restart` over ssh through an argv this code does not build.
* `--engine` with directory / multi-agent targets. Engine keys are per-spec, so
  one key does not name the same backend across agents; `_start_parallel` also
  does not re-append it to each child argv.

**What is ruled out**

* A per-turn or mid-session engine switch (Q2).
* Any fallback: to the default when an explicit `--engine` was given, to
  another engine when one cannot be honoured, or to plain Anthropic (Q3).
* Reading `could-not-tell` as honourable.
