# YAML Spec Reference (v3)

Container + session knobs nest under the engine that interprets them
(`spec.apptainer.*`, `spec.claude.*`). Cross-cutting knobs (workdir,
a2a, health, restart) stay at the top level. Every curated block has
a `raw_*` escape hatch — full underlying surface is always reachable.

The agent name is the parent directory of `spec.yaml`.

## Full annotated example

```yaml
apiVersion: scitex-agent-container/v3
kind: Agent

metadata:
  labels:                              # arbitrary string→string, used by sac fleet ...
    role: researcher
    team: lab-a

spec:
  runtime: apptainer                   # the only accepted value (post 2026-05-13 ripout)
  workdir: ~/proj/example              # mounted rw at /work inside the container

  apptainer:
    image: ./sac-base.sif              # full path or relative path to this spec.yaml
    overlay: ./overlay.img             # writable overlay (rw layer above the SIF)
    nv: false                          # forward host NVIDIA libs (--nv)
    rocm: false                        # forward host AMD ROCm libs (--rocm)
    binds:                             # bind mounts (host:container[:mode])
      - /data/gpfs:/data/gpfs:ro
    env:                               # env vars exported into the container
      FOO: bar
    raw_args: []                       # escape hatch → appended to apptainer exec argv

  dot_claude: ./dot_claude             # relative to spec.yaml (preferred) or absolute
    # merged into workspace/.claude/ at agent-start.
    # may contain: CLAUDE.md, .mcp.json, .env, state.md,
    #              commands/, skills/, hooks/, settings.local.json

  startup_commands:                    # shell commands, run BEFORE claude starts
    - "uv venv /opt/venv-agent --python python3"

  startup_prompts:                     # fed to claude as first user message(s)
    - "Apply the SciTeX quality playbook."

  claude:
    model: claude-opus-4-5
    session: new-session               # or 'continue', or 'resume <sid>' (mirrors claude CLI)
    channels:                          # push-based; passed as claude --channels
      - server:orochi-push
      - server:a2a
    flags:
      - --dangerously-skip-permissions
    raw_options: {}                    # escape hatch → ClaudeAgentOptions(**raw_options)

  a2a:
    port: 7901                         # bind POST /v1/turn on this localhost port (per-agent)

  health:
    enabled: true
    interval: 60                       # seconds between probes
    method: sdk-alive                  # only currently supported method

  restart:
    policy: on-failure                 # never | on-failure | always
    max_retries: 3
    backoff_initial: 30
    backoff_max: 300
    backoff_multiplier: 2
```

## Field reference

| Section                       | Key Fields                                                               | Description                                                                                                                                                                  |
|-------------------------------|--------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `apiVersion`                  | `scitex-agent-container/v3`                                              | Config format version                                                                                                                                                        |
| `metadata.labels`             | string→string map                                                        | Used by `sac fleet ...` filters                                                                                                                                              |
| `spec.runtime`                | `apptainer` (the only supported runtime)                                 | Container backend                                                                                                                                                            |
| `spec.workdir`                | path                                                                     | Workspace mounted at `/work` inside the container                                                                                                                            |
| `spec.apptainer.image`        | path to `.sif`                                                           | Default: `~/.scitex/agent-container/containers/sac-base.sif`                                                                                                                 |
| `spec.apptainer.overlay`      | path                                                                     | Writable overlay (rw layer above the SIF)                                                                                                                                    |
| `spec.apptainer.nv` / `.rocm` | bool                                                                     | Forward host NVIDIA / AMD ROCm libs                                                                                                                                          |
| `spec.apptainer.binds[]`      | `host:container[:mode]`                                                  | Bind mounts (`${VAR}` expanded at start)                                                                                                                                     |
| `spec.apptainer.env`          | key-value pairs                                                          | Env vars exported into the container (`${VAR}` expanded)                                                                                                                     |
| `spec.apptainer.raw_args[]`   | list of strings                                                          | **Escape hatch** — appended verbatim to the `apptainer exec` argv                                                                                                            |
| `spec.dot_claude`             | path                                                                     | Default: auto-discover `./dot_claude` next to `spec.yaml`. Materialized into the workspace at start (CLAUDE.md / .mcp.json / .env / state.md / commands/ / skills/ / hooks/) |
| `spec.startup_commands[]`     | shell commands                                                           | Run **before** Claude starts (e.g. `uv venv ...`)                                                                                                                            |
| `spec.startup_prompts[]`      | strings                                                                  | Fed to Claude as the first user message(s)                                                                                                                                   |
| `spec.claude.model`           | `sonnet`, `opus[1m]`, `haiku-4-5`, ...                                   | Claude model                                                                                                                                                                 |
| `spec.claude.session`         | `new-session` / `continue` / `resume <sid>`                              | Mirrors `claude --resume`/`--continue`                                                                                                                                       |
| `spec.claude.channels[]`      | `server:orochi-push`, `plugin:foo@bar`                                   | Push channels; passed as `claude --channels`                                                                                                                                 |
| `spec.claude.flags[]`         | strings                                                                  | Extra flags appended to the `claude` invocation                                                                                                                              |
| `spec.claude.raw_options`     | dict                                                                     | **Escape hatch** — splatted into `ClaudeAgentOptions(**raw_options)`                                                                                                         |
| `spec.a2a.port`               | int                                                                      | Bind `POST /v1/turn` on this localhost port (per-agent)                                                                                                                      |
| `spec.health`                 | `enabled`, `interval`, `method: sdk-alive`                               | Health probe config                                                                                                                                                          |
| `spec.restart`                | `policy` (`never` / `on-failure` / `always`), `max_retries`, `backoff_*` | Supervisor restart policy                                                                                                                                                    |

## Lifetime / session selection

No `mode` field. Default is long-lived + new session. CLI flips it:

```bash
sac agents start <name> --one-shot        # exits after startup_prompts
sac agents start <name> --resume <sid>    # resume a prior session
sac agents start <name> --continue        # continue the last session
```

## Examples

Copy from [`examples/agents/`](../examples/agents/):

- `full-agent/` — annotated spec with all fields + full `dot_claude/` layout
- `minimal-agent/` — bare-minimum spec, no `dot_claude`
- `hello-agent/` — quickstart example with `startup_prompts`
