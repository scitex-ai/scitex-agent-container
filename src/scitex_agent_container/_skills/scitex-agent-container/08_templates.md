---
name: templates
description: Templates and Examples — see file body for details.
tags: [scitex-agent-container, scitex-package]
---

# Templates and Examples

Two directories under `config/` ship YAML you can copy:

- `config/templates/` — six minimal **pattern** templates (one per deployment pattern)
- `config/examples/` — concrete real-world configs

Both are validated by `tests/test_templates_v3_valid.py`. The SLURM template additionally renders an sbatch script in CI to catch YAML/dataclass drift.

## Pattern matrix

| Template | Runtime | Distinguishing key |
|---|---|---|
| `local.yaml` | `claude-code` | no container, no remote |
| `docker.yaml` | `claude-code` | `container.runtime: docker` |
| `apptainer.yaml` | `claude-code` | `container.runtime: apptainer`, `image: *.sif` |
| `ssh.yaml` | `claude-code` | `remote.host: ...` |
| `ssh-slurm.yaml` | `slurm` | `slurm.{partition,time_limit,hooks}` |
| `mcp.yaml` | `claude-code` | `mcp_servers: {...}` |

Patterns are orthogonal — a real agent can combine them (e.g. `ssh` + `mcp`, `apptainer` + `ssh`). The templates demonstrate the *minimum* fields each pattern requires; mix-and-match by copying YAML keys.

## Instantiating (dir-as-SSoT)

The v3 loader derives the agent name from the parent directory, not from `metadata.name`. To instantiate:

```bash
mkdir -p ~/.scitex/orochi/agents/my-agent
cp config/templates/local.yaml ~/.scitex/orochi/agents/my-agent/my-agent.yaml
sac start my-agent
```

For SSH-deployed agents, drop sibling `src_CLAUDE.md` and `src_mcp.json` into the same directory — they're copied to `/tmp/` on the remote and materialized at start.

## When to add a new template

Add a new pattern template only when the new YAML shape isn't expressible by combining existing templates. If you find yourself documenting a specific operator decision, write to `config/examples/` instead — examples are real configs frozen in time, templates are minimal patterns.

When adding a template:

1. Drop the YAML in `config/templates/<name>.yaml`.
2. Add `<name>.yaml` to the `expected` set in `test_minimal_templates_cover_expected_patterns`.
3. Add a runtime-specific assertion to `test_templates_v3_valid.py` that exercises the field shape unique to your pattern (e.g. for slurm, render the sbatch script and check for hardener strings).

## Examples

`config/examples/` holds concrete configs that aren't patterns:

- `newbie-docker.yaml` — Hawthorne-effect-free naive-user simulation; documents the 2026-04-12 contamination incident lesson.
- `researcher-opus.yaml` — Opus-powered researcher with on-failure restart + backoff tuned for long sessions.

Examples are loaded by `test_example_loads` to guard against schema drift, but they are not part of the pattern guarantee — feel free to add new ones for any non-trivial fleet member you ship.
