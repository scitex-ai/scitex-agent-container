---
description: |
  [TOPIC] Neutral configuration CLI
  [DETAILS] Inspect one .agents layer, preview Claude rendering, check drift, and import with explicit write approval. Distinct from spec.yaml and fleet config.yaml; other neutral renderers remain planned.
tags: [scitex-agent-container-neutral-config-cli]
---

# Neutral configuration CLI

`sac config` operates on harness-neutral `.agents/` content. It does not edit
agent `spec.yaml` or fleet `config.yaml`. The current CLI renderer accepts only
`--harness claude` (`--from claude` for import). Lifecycle support for another
harness does not establish a neutral renderer for it.

## Resolve the selected layer

`--scope user` is the default. It selects
`~/.scitex/agent-container/.agents`; native output defaults to `~/.claude`, or
`CLAUDE_CONFIG_DIR` when set. Project scope selects
`<git-root>/.scitex/agent-container/.agents` and `<git-root>/.claude`.
These commands inspect or render the selected layer; `show` is not a report of
an automatically merged user/project configuration.

```bash
sac config show --scope user --json
sac config deploy --harness claude --scope user --dry-run
sac config check --harness claude --scope user
```

`show` supports `--source` for the layer directory. `deploy` and `check` also
support `--target`. Project scope needs a Git repository unless both source
and target are explicit; `show` has no `--target` option. A missing source layer
is a usage error, not an empty configuration.

## Preview, drift and writes

`deploy` previews by default. Applying its plan requires `--yes`/`-y`;
`--dry-run` still previews when combined with approval. Preview reports drift
but is not the drift exit-code gate: use `check`, which exits 1 on drift and
0 when in sync. Neither a preview nor a successful check launches an agent or
qualifies an Apptainer runtime.

`import` reads native Claude files and requires a destination via `--to`.
It previews unless approved; `--dry-run` takes precedence over `--yes`.
`--claude-dir` selects the native source and `--copy-trees` includes the supported
hooks/agents/commands/skills trees. Inspect the plan before requesting writes:

```bash
sac config import --from claude --claude-dir /owned/claude --to /owned/layer --dry-run
```

Use owned temporary paths for qualification, not live credentials or agent
homes. Source commands and flags are defined by the
[CLI implementation](../../cli_pkg/config_group.py). Owning checkout tests are
`tests/scitex_agent_container/cli_pkg/test_config_group.py` and
`tests/scitex_agent_container/_neutral_config/`: they cover preview/apply,
drift, migration and preservation behavior. They do not establish native
launch, hosted delivery or another harness's renderer.
