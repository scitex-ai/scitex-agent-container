---
description: |
  [TOPIC] Templates and Examples
  [DETAILS] Templates and Examples — see file body for details..
tags: [scitex-agent-container-templates]
---

# Templates and Examples

Two directories under `config/` ship YAML you can copy:

- `examples/agent-templates/` — six minimal **pattern** templates (one per deployment pattern)
- `examples/agents/` — concrete real-world configs

Both are validated by `tests/test_templates_v3_valid.py`. The SLURM template additionally renders an sbatch script in CI to catch YAML/dataclass drift.

## Pattern matrix

| Template | Pattern | Distinguishing key |
|---|---|---|
| `apptainer.yaml` | claude-session inside Apptainer SIF | `spec.runtime: apptainer`, `spec.image: *.sif` (default) |
| `ssh.yaml` | remote agent via SSH | dispatched cross-host via `sac --on <peer>` (F-CS12) |

MCP wiring is no longer a separate template — drop a `.mcp.json` into the agent's `dot_claude/` directory and it's merged into `<workdir>/.mcp.json` at start (F-DC1). Docker / podman / local-bare-metal patterns deleted after F-CS17 made sac apptainer-only.

## Instantiating (dir-as-SSoT)

The v3 loader derives the agent name from the parent directory, not from `metadata.name`. To instantiate:

```bash
mkdir -p ~/.scitex/agent-container/agents/my-agent
cp examples/agent-templates/apptainer.yaml ~/.scitex/agent-container/agents/my-agent/spec.yaml
# optional: add a dot_claude/ sibling for CLAUDE.md / .mcp.json / .env / commands / skills / hooks
sac agent start my-agent
```

For SSH-deployed agents, drop a sibling `dot_claude/` directory next to `spec.yaml` — the whole directory rsyncs to the remote and is materialized into the workspace at start.

## When to add a new template

Add a new pattern template only when the new YAML shape isn't expressible by combining existing templates. If you find yourself documenting a specific operator decision, write to `examples/agents/` instead — examples are real configs frozen in time, templates are minimal patterns.

When adding a template:

1. Drop the YAML in `examples/agent-templates/<name>.yaml`.
2. Add `<name>.yaml` to the `expected` set in `test_minimal_templates_cover_expected_patterns`.
3. Add a runtime-specific assertion to `test_templates_v3_valid.py` that exercises the field shape unique to your pattern (e.g. for slurm, render the sbatch script and check for hardener strings).

## Examples

`examples/agents/` holds concrete configs that aren't patterns:

- `newbie-docker.yaml` — Hawthorne-effect-free naive-user simulation; documents the 2026-04-12 contamination incident lesson.
- `researcher-opus.yaml` — Opus-powered researcher with on-failure restart + backoff tuned for long sessions.

Examples are loaded by `test_example_loads` to guard against schema drift, but they are not part of the pattern guarantee — feel free to add new ones for any non-trivial fleet member you ship.
