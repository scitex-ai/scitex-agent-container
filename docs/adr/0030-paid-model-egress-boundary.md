# ADR-0030 — Paid models cross a model-firewall gateway

## Status

Proposed. This change prepares SAC integration; it does not deploy or restart
the gateway or any agent.

## Observation

The DeepSeek Hermes session could select a costly Pro model even when its spec
named Flash. `discover_models: false` did not create an outbound allowlist.
Hermes also registers its built-in DeepSeek provider whenever
`DEEPSEEK_API_KEY` exists in the container, so `/model deepseek-v4-pro` can
bypass a custom provider definition.

## Decision

SAC stays the container and lifecycle boundary. SciTeX GenAI owns a neutral
external-provider relay at the network egress boundary.

- The canonical SAC provider name is `external-gateway`.
- The legacy `deepseek` registry name resolves to the same gateway so existing
  declarative specs do not retain the unsafe direct route.
- The default endpoint is `http://scitex-compute-04:18775`; deployments may
  override it with `SAC_EXTERNAL_GATEWAY_URL`.
- Containers receive `SCITEX_GENAI_GATEWAY_API_KEY`, not the vendor API key.
- Every harness pointed at the resolved external gateway receives an explicit
  empty `DEEPSEEK_API_KEY` at the final Apptainer environment boundary,
  independent of model spelling or gateway-token variable name. SAC removes
  values contributed by `spec.env` and `raw_args` before secret-file creation,
  then places the empty `--env` override after all environment files. This
  closes host inheritance, spec/raw injection, and Hermes built-in-provider
  discovery for Claude Code, Codex, and Hermes paths alike.
- The scitex-genai egress gateway holds the vendor credential, normalizes the
  accepted Flash alias to the canonical model, blocks Pro before outbound
  HTTP, and owns usage and spending controls.

The readable spec remains small:

```yaml
spec:
  harness: hermes
  engines:
    deepseek-flash:
      model: deepseek-flash
      provider: external-gateway
      reasoning_effort: low
```

## Consequences

An agent cannot reach DeepSeek directly using configuration that names the
registered `deepseek` provider. An inline provider dictionary that resolves to
the same gateway endpoint receives the same final credential denial. Inline
direct-vendor endpoints remain a general operator escape hatch, so network
isolation remains the ultimate authority for untrusted specs.

The gateway budget is incarnation-scoped initially. Persisting calendar-period
spend belongs in the shared SciTeX Postgres store, not a SAC or Hermes SQLite
database.
