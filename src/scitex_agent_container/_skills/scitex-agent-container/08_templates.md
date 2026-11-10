---
description: |
  [TOPIC] Agent templates and scaffolding
  [DETAILS] Discover installed inline presets and local directory templates, create a spec, and distinguish scaffolding/schema checks from launch readiness.
tags: [scitex-agent-container-templates]
---

# Agent templates and scaffolding

## Installed scaffolding command

```bash
sac agents create my-agent --template minimal --dry-run
sac agents create my-agent --template minimal
sac agents create --base-dir /path/to/agents --help
```

`scitex-agent-container` is the long entry-point spelling. `create` writes a
spec and home skeleton; it does not start an agent. Inline creation may also
attempt managed-home adoption. Review the target and emitted warnings before
using its output for a launch.

`--dry-run` reports arguments only and hides `--set` values. It does not read
templates or credentials, resolve the default root, check destination state
or qualify a launch. MCP `agent_create(dry_run=True)` also prevents `start=True`.
Cold CLI startup may initialize shared logging; preview does not write agent files.
The public Python entry point is
`scitex_agent_container.agent.create(..., dry_run=True)`, the same callable as MCP.

The built-in `minimal` and `full` definitions live in
`cli_pkg/_create_templates.py`. They are available through the installed CLI.
Their model and endpoint fields follow the selected engine. The full preset
uses `SCITEX_CARDS_AGENT_ID` for card identity. Credential, image, engine and
host readiness still need separate qualification; schema acceptance alone
does not establish that the agent can start.

## Directory templates

The command discovers `_template_<kind>/` directories under the selected
agents root. `--base-dir` selects that root explicitly; help scans it at
invocation time. Use the displayed list for that environment rather than a
fixed catalog. Built-in `minimal` and `full` names retain their inline meaning.

Directory templates are copied and their `SAC_PLACEHOLDER_*` tokens filled
using `--project`, `--agent-id` and repeated `--set KEY=VALUE`. Unresolved
tokens fail and partial directory-template output is removed. Existing inline
specs are not overwritten; force replacement is unsupported. CLI name and
reserved-slot checks precede writes. GUI callers use the shared
`cli_pkg/_create.py:scaffold_agent` backend and own their name validation.

The loader derives an agent's name from its spec's parent directory. Built-in
scaffolding creates a `to_home/` sibling for optional home assets; directory
templates can supply their own tree. Review those assets and the resulting
spec before starting the agent.

## Repository examples and maintenance

In a source checkout, `examples/agents/*/spec.yaml` contains copy-and-adapt
examples. These files are separate from the inline presets and locally
discovered directory templates; this guide does not promise that checkout
examples exist beside every installed wheel.

Use the owning implementation and its existing controls when changing them:

- `cli_pkg/_create.py` and `cli_pkg/_create_templates.py`;
  `tests/scitex_agent_container/cli_pkg/test__create.py` and
  `tests/scitex_agent_container/cli_pkg/test__create_templates.py` cover
  scaffolding, template fields, refusals, placeholders and live help discovery.
- `tests/integration/test_templates_v3_valid.py` loads the current repository
  examples and checks their names/runtime and selected home assets. Those
  checks do not launch an agent, provision an image or admit a live provider.

Keep source, installed CLI help and the relevant tests aligned. Generated or
served documentation and actual runtime readiness require separate evidence.
