<!-- ---
!-- Timestamp: 2026-05-13 14:18:48
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

**Step 1 — Build the base image (one-time, ~5 min)**

```bash
sac image build base
```

**Step 2 — Create agent directories**

```bash
# Each agent lives in its own directory; the directory name is the agent name.
mkdir -p ~/.scitex/agent-container/agents/hello-agent-{1,2}
```

**Step 3 — Write `spec.yaml`** (copy into each agent directory, adjust `startup_prompts`)

```yaml
# ~/.scitex/agent-container/agents/hello-agent-1/spec.yaml
apiVersion: scitex-agent-container/v3
kind: Agent

spec:
  runtime: apptainer

  apptainer:
    image: ~/.scitex/agent-container/containers/sac-base.sif

  claude:
    model: haiku
    flags:
      - --dangerously-skip-permissions

  startup_prompts:
    - "Reply with the string 'Hello! I am hello-agent-1' and nothing else."

  health:
    enabled: true
    interval: 60
    method: sdk-alive

  restart:
    policy: never
```

> Or copy the bundled example: `cp -r examples/agents/hello-agent ~/.scitex/agent-container/agents/hello-agent-1`

**Step 4 — Run**

```bash
# Start in foreground (waits for completion)
sac agents start hello-agent-1 hello-agent-2 --foreground

# Check status
sac agents list

# Start in background, read output, stop, delete
sac agents start hello-agent-1 hello-agent-2
sac agents tail  hello-agent-1 hello-agent-2 --json
sac agents stop  hello-agent-1 hello-agent-2
sac agents delete hello-agent-1 hello-agent-2 -y
```

### Tutorial

[`examples/tutorial/`](examples/tutorial/) walks through the runtime in 9 lessons (build, sandbox/update/freeze, versioning, run/stop, logs/exec, mounts, env+user). Run them read-only with `bash 00_run_all.sh`, or `--apply` to execute the mutating ones.

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

**[YAML Spec Reference (v3) →](docs/spec-reference.md)** — annotated full example + field table (apiVersion, spec.apptainer.*, spec.claude.*, a2a, health, restart).

## Configuration and Runtime Directories

**[Full directory reference →](docs/directories.md)** — complete tree, configuration cascade (CLI flag → env var → project config → user config).

```
~/.scitex/agent-container/
├── agents/<name>/spec.yaml    ← agent definition (SSoT)
├── containers/sac-base.sif    ← built images (gitignored)
└── runtime/<name>/            ← live state: pid, heartbeat, session.jsonl
```

**[Apptainer images →](docs/images.md)** — `base` vs `scitex` layers, sandbox/freeze workflow, version pinning.

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

## Part of SciTeX

`scitex-agent-container` is part of [**SciTeX**](https://scitex.ai). Install via the umbrella with `pip install scitex[agent-container]` to use as `scitex.agent_container` (Python) or `scitex agent-container ...` (CLI).

[`scitex-orochi`](https://github.com/ywatanabe1989/scitex-orochi) can consume `sac` and allow for cross-host communication across agents and users on a Slack-like web interface (live instance at [https://scitex-orochi.com](https://scitex-orochi.com)).

```
            ┌────────────────────┐                       ┌──────────────────────┐
            │   Human operator   │  chat · DM · channel  │ claude-code-         │
            │   (web UI / CLI)   │ ◄───── alerts ─────── │ telegrammer          │
            └─────────┬──────────┘                       │ Telegram MCP + TUI   │
                      │                                  └──────────▲───────────┘
                      ▼                                             │
        ┌──────────────────────────────────┐                        │
        │   scitex-orochi                  │                        │
        │   WebSocket hub · dashboard      │                        │
        │   MCP channels · presence · A2A  │                        │
        │   peer registry · cross-host     │                        │
        └─────────────────┬────────────────┘                        │
                          │  reads status                           │
                          │  (one-way dep: orochi → sac)            │
                          ▼                                         │
        ┌──────────────────────────────────┐                        │
        │   scitex-agent-container (sac)   │                        │
        │      ← YOU ARE HERE              │                        │
        │   lifecycle · health · restart   │                        │
        │   apptainer runtime · per host   │                        │
        │   (zero knowledge of orochi)     │                        │
        └─────────────────┬────────────────┘                        │
                          │  starts / supervises                    │
                          ▼                                         │
        ┌──────────────────────────────────┐                        │
        │   Claude agents (one per host)   │ ── heartbeat-push ──▶ orochi
        │   session.jsonl · SDK            │ ── alerts ─────────────┘
        └──────────────────────────────────┘
```

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