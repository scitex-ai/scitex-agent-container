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
- A DeepSeek model behind this gateway explicitly receives an empty
  `DEEPSEEK_API_KEY` in its final Apptainer environment. This closes the
  inherited-host-environment route by which Hermes creates its built-in
  provider.
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
registered `deepseek` provider. Explicit inline provider dictionaries remain
supported as the general escape hatch, so container/network isolation remains
the ultimate authority for untrusted specs. Deployment must therefore avoid
placing vendor credentials in `to_home`, raw environment entries, or generic
agent secrets.

The gateway budget is incarnation-scoped initially. Persisting calendar-period
spend belongs in the shared SciTeX Postgres store, not a SAC or Hermes SQLite
database.
