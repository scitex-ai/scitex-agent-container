# 02 — Agent spec (`spec.yaml`)

> **Stub.** Scope and outline below; to be fully written in a follow-on.
> Consolidates [`spec-reference.md`](spec-reference.md) (the current
> authoritative field reference) into the product-doc series.

## Scope

The complete `apiVersion: scitex-agent-container/v3` `spec.yaml` schema — the
single file that fully defines an agent. One directory per agent under
`~/.scitex/agent-container/agents/<name>/`; the directory name IS the agent name
(there is no `name:` field). This page documents every field, its default, and
which runtime/loader reads it.

## TODO — this page will contain

- [ ] Top-level keys: `apiVersion`, `kind`, `metadata.labels` (`groups`, `tags`, `capabilities`, `cardinality`), `spec`.
- [ ] `spec.runtime` — the launch-mode selector (`tui` default, `apptainer`, `docker`, `podman`) and what each dispatches to.
- [ ] `spec.host` — placement / cross-host routing (→ [03](03_cross-host-control-plane.md)).
- [ ] `spec.workdir` and `spec.mounts[]` / `spec.apptainer.binds[]` — the host-path allowlist.
- [ ] `spec.apptainer` — `image`, `relaxed`, `raw_args`, overlays, `--containall` defaults (→ [isolation.md](isolation.md)).
- [ ] `spec.claude` — `model` (aliases + `[1m]` + exact pins), `provider` (Anthropic / DeepSeek / MiMo / custom endpoint), `credentials_files`, `channels`, `flags`, `session`.
- [ ] `spec.a2a` (`port: auto`), `spec.health`, `spec.restart` (`policy`, `max_retries`).
- [ ] `spec.startup_prompts` / `spec.startup_commands`, `spec.mcp_servers`.
- [ ] `to_home/` overlay next to the spec — merge rules for `CLAUDE.md`, `.mcp.json`, `.env`, `.claude/` (→ [how-sac-works.md](how-sac-works.md)).
- [ ] Scaffolding a spec with `sac agents create`, and validating it with `sac agents check` / `sac agents explain`.
- [ ] The `AgentConfig` Python object and how loader defaults are applied.

<!-- EOF -->
