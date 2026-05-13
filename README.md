<!-- ---
!-- Timestamp: 2026-05-13 11:03:02
!-- Author: ywatanabe
!-- File: /home/ywatanabe/proj/scitex-agent-container/README.md
!-- --- -->

# SciTeX Agent Container (<code>scitex-agent-container</code>)

<p align="center">
  <a href="https://scitex.ai">
    <img src="docs/scitex-logo-blue-cropped.png" alt="SciTeX" width="400">
  </a>
</p>

<p align="center"><b>Agent in Apptainer</b></p>

<p align="center">
  <a href="https://scitex-agent-container.readthedocs.io/">Full Documentation</a> · <code>uv pip install scitex-agent-container[all]</code>
</p>

<!-- scitex-badges:start -->
<p align="center">
  <a href="https://pypi.org/project/scitex-agent-container/"><img src="https://img.shields.io/pypi/v/scitex-agent-container.svg" alt="PyPI"></a>
  <a href="https://pypi.org/project/scitex-agent-container/"><img src="https://img.shields.io/pypi/pyversions/scitex-agent-container.svg" alt="Python"></a>
  <a href="https://scitex-agent-container.readthedocs.io/en/latest/"><img src="https://readthedocs.org/projects/scitex-agent-container/badge/?version=latest" alt="Read the Docs"></a>
</p>
<p align="center">
  <a href="https://github.com/ywatanabe1989/scitex-agent-container/actions/workflows/test.yml"><img src="https://github.com/ywatanabe1989/scitex-agent-container/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  <a href="https://codecov.io/gh/ywatanabe1989/scitex-agent-container/branch/develop"><img src="https://codecov.io/gh/ywatanabe1989/scitex-agent-container/branch/develop/graph/badge.svg" alt="Coverage (develop)"></a>
</p>
<!-- scitex-badges:end -->

---

## Problem and Solution

| # | Problem                                                     | Solution                                                                                                                                      |
|---|-------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------|
| 1 | Scripting an agentic workflow is hard.                      | `scitex-agent-container` (`sac`) declares the agent as a **single YAML file** ([`spec.yaml`](#yaml-spec-reference-v3)).                                                  |
| 2 | Subagents don't scale across hosts, projects, and contexts. | `sac` lets agents spawn **full agents** on local AND **remote hosts**.                                                                        |
| 3 | Controlling agent permissions is difficult.                 | `sac` runs every agent **inside Apptainer** — full mount/env/security options exposed in `spec.yaml`.                                         |
| 4 | Supporting the A2A protocol by hand is time-consuming.      | `sac` needs just one YAML field (`spec.a2a.port`).                                                                                            |
| 5 | Version-controlling Apptainer recipes is laborious.         | `sac` enables layered Apptainer images with a sandbox/update/freeze workflow via [`scitex-container`](https://github.com/ywatanabe1989/scitex-container). |

## Installation

```bash
uv pip install "scitex-agent-container[all]"
```

## Quickstart

```bash
# 1. Build built-in Apptainer image (one-time)
sac image build base # base image; ~5 min
# built /home/ywatanabe/.scitex/agent-container/containers/sac-base/sac-base.sif

# 2. Define agents by writing YAML files under ~/.scitex/agent-container/agents/<agent-name>
define_hello_agents() {
    for agent_id in 1 2; do
        agent_dir=~/.scitex/agent-container/agents/"hello-agent-$agent_id"

        mkdir -p "$agent_dir" >/dev/null
        # Unquoted heredoc tag — shell expands $agent_name before write.
        # Escape any literal `$` that should reach YAML verbatim (none here).
        cat > "$agent_dir/spec.yaml" <<YAML
apiVersion: scitex-agent-container/v3
kind: Agent

spec:
  runtime: apptainer
  # workdir is optional — defaults to runtime/agents/<name>/.

  apptainer:
    image: ~/.scitex/agent-container/containers/sac-base.sif

  claude:
    model: haiku
    flags:
      - --dangerously-skip-permissions

  startup_prompts:
    - "Reply with the string 'Hello! I am hello-agent-$agent_id' and nothing else."
YAML

    done
}
define_hello_agents

# 3. Start an agent in foreground
sac agents start hello-agent-1 hello-agent-2 --foreground
# INFO: starting hello-agent-1 → ywata-note-win@/home/ywatanabe/.scitex/agent-container/runtime/agents/hello-agent-1:/work
# INFO: CLAUDE.md updated for agent hello-agent-1 at /home/ywatanabe/.scitex/agent-container/runtime/agents/hello-agent-1/.claude/CLAUDE.md
# SUCC: hello-agent-1 started (ywata-note-win@/home/ywatanabe/.scitex/agent-container/runtime/agents/hello-agent-1:/work)
#  
# INFO: starting hello-agent-2 → ywata-note-win@/home/ywatanabe/.scitex/agent-container/runtime/agents/hello-agent-2:/work
# INFO: CLAUDE.md updated for agent hello-agent-2 at /home/ywatanabe/.scitex/agent-container/runtime/agents/hello-agent-2/.claude/CLAUDE.md
# SUCC: hello-agent-2 started (ywata-note-win@/home/ywatanabe/.scitex/agent-container/runtime/agents/hello-agent-2:/work)

# [hello-agent-1] (stopped)
# [hello-agent-2] (stopped)

# 4. Check agents
sac agents list
#                                                            Agents                                                            
# ┏━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┓
# ┃ Name          ┃ Status  ┃ YAML ┃ Host  ┃ Path                                                                   ┃ Started ┃
# ┡━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━┩
# │ hello-agent-1 │ defined │ ✓    │ local │ /home/ywatanabe/.scitex/agent-container/agents/hello-agent-1/spec.yaml │ —       │
# │ hello-agent-2 │ defined │ ✓    │ local │ /home/ywatanabe/.scitex/agent-container/agents/hello-agent-2/spec.yaml │ —       │
# └───────────────┴─────────┴──────┴───────┴────────────────────────────────────────────────────────────────────────┴─────────┘

# 5. Start multiple agents in background (default)
sac agents start hello-agent-1 hello-agent-2

# 6. Read the outputs in JSON format
sac agents tail hello-agent-1 hello-agent-2 --json

# 7. Stop agents
sac agents stop hello-agent-1 hello-agent-2

# 8. Delete agents
sac agents delete hello-agent-1 hello-agent-2 -y
```

## How it works

`scitex-agent-container` (`sac`) materializes a `spec.yaml` into a long-lived, externally addressable Claude agent:

```
  spec.yaml   ─┐
  dot_claude/ ─┴─→ sac agents start ──→ apptainer instance
                                          │
                                          ▼
                              long-lived Claude SDK session
                              │
                              ├── <workdir>  (= spec.workdir, mounted rw)
                              │     CLAUDE.md / .mcp.json / .env / state.md     ← from dot_claude/
                              │     .claude/{commands,skills,hooks,...}         ← mirrored
                              │
                              ├── spec.mounts[]  ← explicit host-path allowlist (ro/rw)
                              │
                              ├── state-dir  (host: ~/.scitex/agent-container/runtime/<name>/)
                              │     pid, heartbeat.json,
                              │     session.jsonl, session_id, quota.json
                              │
                              ├─→ POST /v1/turn                 (per-agent A2A inbound)
                              │       ▲
                              │       │  live-runner route
                              │       │
  sac listen :7878 ───────────┼───────┘
  bearer-auth /v1/sac/{                 \
    health, agents,                      ─→ claude --resume <sid> -p
    agents/<n>/{status,send,card},                          (re-launch fallback when
    ...                                                      no live runner)
  }
                                                            ▲
  sac channel send TO MSG ─────────────────────────────────┤
  sac peer  post-turn  AGENT TEXT  ────────────────────────┘
```


<details>
<summary><strong>YAML Spec Reference (v3)</strong></summary>

Container + session knobs nest under the engine that interprets them
(`spec.apptainer.*`, `spec.claude.*`). Cross-cutting knobs (workdir,
a2a, health, restart) stay at the top level. Every curated block has
a `raw_*` escape hatch — full underlying surface is always reachable.

The agent name is the parent directory of `spec.yaml` (dir-as-SSoT);
there's no `metadata.name` field. Renaming an agent = `mv` the directory.

| Section                       | Key Fields                                                               | Description                                                                                                                                                                  |
|-------------------------------|--------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `apiVersion`                  | `scitex-agent-container/v3`                                              | Config format version                                                                                                                                                        |
| `metadata.labels`             | string→string map                                                        | Used by `sac fleet ...` filters                                                                                                                                              |
| `spec.runtime`                | `apptainer` (the only supported runtime)                                 | Container backend                                                                                                                                                            |
| `spec.workdir`                | path                                                                     | Workspace mounted at `/work` inside the container                                                                                                                            |
| `spec.apptainer.image`        | path to `.sif`                                                           | Default: `~/.scitex/agent-container/containers/sac-scitex.sif`                                                                                                               |
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

**Lifetime / session selection:** no `mode` field. Default is
long-lived + new session. CLI flips it: `sac agents start <name>
--one-shot` (exits after `startup_prompts`), `--resume <sid>` /
`--continue` (resumes / continues the prior session).

Example:

``` yaml

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

Example YAMLs: [`./examples/agent-templates`](./examples/agent-templates)

</details>

## Configuration Directory

Configuration directories are separated into user-scope (`~/.scitex/agent-container/`) and project-scope (`<proj-root>/.scitex/agent-container/`; prioritized when present). They include `config.yaml`, `agents`, `accounts`, `tokens`, `containers`, and `runtime` as described below.

```
~/.scitex/agent-container/ or <project>/.scitex/agent-container/
├── config.yaml                ← host identity, host.aliases, peers (F-CS12)
├── agents/<name>/             ← per-agent declarations (you write these)
│   ├── spec.yaml              ← v3 Agent definition (the SSoT)
│   └── dot_claude/            ← optional: materialized into <workdir> at start
│       ├── CLAUDE.md           (→ <workdir>/CLAUDE.md, marker-protected)
│       ├── .mcp.json           (→ <workdir>/.mcp.json, per-server merge)
│       ├── .env                (→ <workdir>/.env, mode 0600)
│       ├── state.md            (→ <workdir>/state.md, full overwrite)
│       ├── commands/           (→ <workdir>/.claude/commands/)
│       ├── skills/             (→ <workdir>/.claude/skills/)
│       └── hooks/              (→ <workdir>/.claude/hooks/)
├── accounts/                  ← saved Claude Code accounts (sac account save)
│   ├── <name>/
│   │   ├── account.json        (safe metadata; no tokens)
│   │   └── .credentials.json   (copied into ~/.claude/ on `sac account use`)
│   └── _rotations/
│       └── <email>.ndjson      (OAuth-rotation log, one append per observed rotation)
├── tokens/
│   └── listen-<host>.token    ← `sac listen` bearer tokens (0600)
├── containers/                ← built Apptainer images (see "Apptainer images" below)
│   ├── sac-base.sif    -> sac-base/sac-base.sif        (top-level symlink)
│   ├── sac-scitex.sif  -> sac-scitex/sac-scitex.sif    (top-level symlink)
│   ├── sac-{base,scitex}/                              (dir-per-image)
│   │   ├── sac-{base,scitex}.sif                       (the image; gitignored)
│   │   ├── sac-{base,scitex}.def                       (recipe snapshot)
│   │   ├── sac-{base,scitex}.build-YYYY-MMDD-HHMMSS.log (full build log; gitignored)
│   │   └── .def-hash                                   (skip-rebuild cache)
│   └── {dpkg,node,requirements}-lock.txt               (auto-freeze lock files)
└── runtime/                   ← regenerable per-host state; gitignored
    ├── <agent-name>/           per-agent runner state
    │   ├── pid                  (runner PID)
    │   ├── heartbeat.json       ({ts, pid, state}; refreshed every tick)
    │   ├── session_id           (persisted SDK session id, resume marker)
    │   ├── session.jsonl        (one JSON object per turn event)
    │   └── quota.json           (accumulated per-turn token totals)
    ├── events/                  Claude Code hook event ring-buffer
    │   └── <agent>.jsonl
    └── cache/                   snapshot cache for the dashboard / `sac agents diff`
        └── <agent>.{latest,prev,diff}.json
```

<details>
<summary><strong>Configuration Cascade</strong></summary>

Configurations can be overriden by CLI flags and environmental variables with the following precedence:

1. **CLI flag** — `sac agents start hello --workdir /tmp/x`
2. **Env var** — `SAC_<X>` or the long `SCITEX_AGENT_CONTAINER_<X>` form
   (setting both with different values raises `SacEnvConflict`). Copy
   [`.env.example`](.env.example) to `.env` and uncomment what you need.
3. **Project config** — `<proj>/.scitex/agent-container/config.yaml`,
   when you're inside a git repo that ships one.
4. **User config** — `~/.scitex/agent-container/config.yaml`
   (relocatable via `$SCITEX_DIR`).

</details>

<details>
<summary><strong>Builtin Apptainer Images (`base` and `scitex`)</strong></summary>

Two `.def` recipes, layered:

| Tag       | What's inside                                                                                               | When                                   |
|-----------|-------------------------------------------------------------------------------------------------------------|----------------------------------------|
| `:base`   | Ubuntu 24.04 + dev tools (git, gh, rust CLIs, mermaid, prettier, eslint, jsonlint, uv, pipx, tree, node 20) | Foundation                             |
| `:scitex` | `FROM :base` + ffmpeg + portaudio + `scitex[all]` + claude-agent-sdk + sac itself                           | **Default** when `spec.image` is unset |

```
<site-packages>/scitex_agent_container/containers/    ← recipes (ship in pip wheel)
  apptainer-{base,scitex}.def                          ← canonical SSoT
  Dockerfile.{base,scitex}                             ← docker mirrors
```

Recipes ship in the pip wheel — no need to clone the repo to run `sac image build`. Built artifacts live under `~/.scitex/agent-container/containers/`, never in git.

### "scitex updates often, do we rebuild?"

No — sandbox once, refresh when you want, freeze when stable:

```bash
sac image build scitex --sandbox        # one-time: writable sandbox
sac image update sandbox/                # any time: pip install --upgrade scitex[all]
sac image freeze sandbox/ scitex-2.28.15.sif   # bake to immutable SIF
sac image switch 2.28.15                 # atomic flip (previous remembered)
sac image rollback                       # restore previous version
sac image snapshot -o env.json           # full reproducibility capsule
```

The build / sandbox / version / rollback verbs all delegate to [`scitex-container`](https://github.com/ywatanabe1989/scitex-container).

</details> 

## 1 Interfaces

<details open>
<summary><strong>CLI ⭐⭐⭐ (primary)</strong></summary>

<br>

```bash
# Agent lifecycle
sac agents start  <name> [--foreground]   # daemon by default; --foreground streams stdio
sac agents stop   <name>                  # graceful SIGTERM, escalate to SIGKILL after 5 s
sac agents restart <name>
sac agents send   <name> "<prompt>"       # send a follow-up turn to a running session
sac agents send   <name> --key ESC        # interrupt current turn
sac agents list [<name>] [--snapshot] [--priority]
sac agents health <name>
sac agents tail   <name>                  # render session.jsonl (structured transcript)
sac agents recall <name>                  # human-readable session summary
sac agents check  <name>                  # preflight (validates yaml + probes runtime deps)
sac agents find   <capability>

# Control plane (HTTP/JSON, loopback-only)
sac listen [--bind 127.0.0.1:7878]       # boot per-host REST API (bearer-auth)
sac channel send <to> "<msg>"            # local agent-to-agent message via sac listen

# Image lifecycle (delegates to scitex-container)
sac image build [base|scitex] [--sandbox] [--runtime apptainer|docker]
sac image sandbox SOURCE                  # SIF → writable sandbox
sac image update  SANDBOX [-p PKG]        # pip install --upgrade
sac image freeze  SANDBOX OUT.sif         # sandbox → SIF
sac image list                            # installed versions
sac image switch  VERSION                 # atomic flip
sac image rollback                        # restore previous
sac image status                          # unified dashboard
sac image snapshot [-o env.json]          # reproducibility capsule

# Account / quota
sac account list / save / delete / switch / watch-quota

# Network / peers
sac host show / list / probe / exec / validate
sac peer post-turn AGENT TEXT             # A2A outbound
sac a2a serve <yamls...>                  # A2A inbound for non-SDK runtimes

# Misc
sac event ingest                          # Claude Code hook event ingestor
sac db   query / show / clean / migrate   # state.db inspection
sac registry reconcile                    # singleton placement reconcile across fleet
sac --help-recursive                      # full subcommand tree
```

</details>

<details>
<summary><strong>Examples</strong></summary>

### Agent Templates

`examples/agent-templates/` ships minimal pattern templates — copy and adapt:

| Template | Pattern | When to use |
|---|---|---|
| `apptainer.yaml` | claude-session inside Apptainer SIF | **Default**: HPC + reproducibility |
| `ssh.yaml` | remote agent via SSH | Cross-machine fleet member |

MCP tool wiring is no longer a separate template — drop a `.mcp.json`
into the agent's [`dot_claude/`](#user-state-layout-scitexagent-container)
directory and it'll be merged into `<workdir>/.mcp.json` at start.

### Tutorial

`examples/apptainer_and_sac/` walks through the runtime in 9 lessons (build, sandbox/update/freeze, versioning, run/stop, logs/exec, mounts, env+user). Run them read-only with `bash 00_run_all.sh`, or `--apply` to execute the mutating ones.

</details>

## Part of SciTeX

`scitex-agent-container` is part of [**SciTeX**](https://scitex.ai). Install via the umbrella with `pip install scitex[agent-container]` to use as `scitex.agent_container` (Python) or `scitex agent-container ...` (CLI).

[`scitex-orochi`](https://github.com/ywatanabe1989/scitex-orochi) can consume `sac` and allow Slack-like interface and cross-host communication across agents and users on a web interface (live instance at [https://scitex-orochi.com](https://scitex-orochi.com)).

| Concern                                                   | Owner                                      |
|-----------------------------------------------------------|--------------------------------------------|
| Agent process (SDK + session.jsonl)                       | **sac**                                    |
| Per-host control plane (start/stop/send/tail/list)        | **sac**                                    |
| Container runtime (apptainer only; docker/podman dropped) | **sac**                                    |
| Cross-host message routing                                | **orochi**                                 |
| Human chatops UI (Slack-like)                             | **orochi**                                 |
| In-session push (MCP channel server)                      | **orochi** ships MCP; sac runs agent       |
| SSH mesh / tunnel layer (cloudflared + autossh)           | **orochi**                                 |
| Peer registry                                             | **orochi** (`~/.scitex/orochi/peers.yaml`) |

Rule: **sac knows containers + sessions on one host; orochi knows messages + people across hosts.** sac never imports orochi.


>Four Freedoms for Research
>
>0. The freedom to **run** your research anywhere — your machine, your terms.
>1. The freedom to **study** how every step works — from raw data to final manuscript.
>2. The freedom to **redistribute** your workflows, not just your papers.
>3. The freedom to **modify** any module and share improvements with the community.
>
>AGPL-3.0 — because we believe research infrastructure deserves the same freedoms as the software it runs on.

---

<p align="center">
  <a href="https://scitex.ai" target="_blank"><img src="docs/scitex-icon-navy-inverted.png" alt="SciTeX" width="40"/></a>
</p>

<!-- EOF -->