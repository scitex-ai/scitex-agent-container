<!-- ---
!-- Timestamp: 2026-05-13 06:41:17
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
  <a href="https://github.com/ywatanabe1989/scitex-agent-container/actions/workflows/install-test.yml"><img src="https://github.com/ywatanabe1989/scitex-agent-container/actions/workflows/install-test.yml/badge.svg" alt="Install Test"></a>
  <a href="https://codecov.io/gh/ywatanabe1989/scitex-agent-container/branch/develop"><img src="https://codecov.io/gh/ywatanabe1989/scitex-agent-container/branch/develop/graph/badge.svg" alt="Coverage (develop)"></a>
</p>
<!-- scitex-badges:end -->

---

## Problem and Solution

| # | Problem                                                     | Solution                                                                                                                                      |
|---|-------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------|
| 1 | Scripting an agentic workflow is hard.                      | `scitex-agent-container` (`sac`) declares the agent as a **single YAML file** ([`spec.yaml`](## YAML Spec Reference (v3))).                                                  |
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
# 1. Build the layered images (one-time)
sac image build base -y     # ~15-25 min — OS + dev tools
sac image build scitex -y   # ~10-20 min with uv — FROM :base + scitex[all] (numpy / pandas /
                            #              scipy / torch / etc.). Walk away.

# 2. Define an agent
mkdir -p ~/.scitex/agent-container/agents/hello/
cat > ~/.scitex/agent-container/agents/hello/spec.yaml <<'YAML'
apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
  workdir: /tmp/hello
  model: claude-haiku-4-5
  startup_commands:
    - command: "Reply with the string 'hello-ok' and nothing else."
YAML

# 3. Start an agent
sac agent start hello --foreground   # streams stdout, exits when done

# 4. Start multiple agents
DIR="~/.scitex/agent-container/agents/"
cp -r "$DIR"/hello/ "$DIR"/hello2/
cp -r "$DIR"/hello/ "$DIR"/hello3/
sac agent start hello,hello2,hellow3 --foreground   # streams stdout, exits when done
```

## How it works

`scitex-agent-container` (`sac`) materializes a `spec.yaml` into a long-lived, externally addressable Claude agent:

```
  spec.yaml   ─┐
  dot_claude/ ─┴─→ sac agent start ──→ apptainer instance
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

| Section | Key Fields | Description |
|---|---|---|
| `apiVersion` | `scitex-agent-container/v3` | Config format version |
| `metadata` | `name` (auto-derived from dir), `labels` | Agent identity |
| `spec.runtime` | `apptainer` (default) / `docker` / `claude-session` (host-local) | Container backend |
| `spec.image` | path or tag | Default: `~/.scitex/agent-container/containers/sac-scitex.sif` (apptainer); `scitex-agent-container:scitex` for docker |
| `spec.workdir` | path | Workspace mounted at `/work` inside the container |
| `spec.model` | `sonnet`, `opus[1m]`, `haiku-4-5`, ... | Claude model |
| `spec.user` | `""` / `"host"` / `"<uid>:<gid>"` | Run-as user; `"host"` matches the operator |
| `spec.mounts[]` | `src`, `dst`, `mode` (ro/rw) | Bind mounts (`${VAR}` expanded at start) |
| `spec.env` | key-value pairs | Container env (`${VAR}` expanded) |
| `spec.a2a` | `port` | Bind `POST /v1/turn` on this localhost port |
| `spec.remote` | `host`, `user`, `timeout` | SSH-as-transport for cross-machine deploy |
| `spec.startup_commands[]` | `command` | One-shot turns to send to the SDK before going idle |
| `spec.health` | `enabled`, `interval`, `method: sdk-alive` | Health probe config |
| `spec.skills` | `required[]`, `available[]` | Skill auto-injection into CLAUDE.md |
| `spec.dot_claude` | path | Default: auto-discover `./dot_claude` next to `spec.yaml`. Absolute or relative; the directory is materialized into the workspace at start with `${metadata.name}` and `${ENV_VAR}` interpolation. |

`<dot_claude>/CLAUDE.md`, `state.md`, `.mcp.json`, `.env` are materialized at the workdir root; `<dot_claude>/commands/`, `skills/`, `hooks/`, etc. mirror into `<workdir>/.claude/`.

Example YAML file is seen at [`./examples/agent-templates`](./examples/agent-templates)

</details>

## Configuration Directory

Configuration directories are separated into user-scope (`~/.scitex/agent-container/`) and project-scope (`<proj-root>/.scitex/agent-container/`; prioritized when present). They include `config.yaml`, `agents`, `accounts`, `tokens`, `conatiners`, and `runtime` as described below.

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
    └── cache/                   snapshot cache for the dashboard / `sac agent diff`
        └── <agent>.{latest,prev,diff}.json
```

<details>
<summary><strong>Configuration Cascade</strong></summary>

Configurations can be overriden by CLI flags and environmental variables with the following precedence:

1. **CLI flag** — `sac agent start hello --workdir /tmp/x`
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
sac agent start <name> [--foreground]    # daemon by default; --foreground streams stdio
sac agent stop  <name>                    # graceful SIGTERM, escalate to SIGKILL after 5 s
sac agent restart <name>
sac agent status [<name>] [--snapshot] [--priority]
sac agent health <name>
sac agent tail   <name>                   # render session.jsonl (structured transcript)
sac agent recall <name>                   # human-readable session summary
sac agent check  <name>                   # preflight (validates yaml + probes runtime deps)
sac agent find   <capability>

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