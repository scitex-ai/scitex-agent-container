<!-- ---
!-- Timestamp: 2026-05-13 14:23:46
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
  <a href="https://pypi.org/project/scitex-agent-container/"><img src="https://img.shields.io/pypi/v/scitex-agent-container?label=PyPI" alt="PyPI"></a>
  <a href="https://pypi.org/project/scitex-agent-container/"><img src="https://img.shields.io/pypi/pyversions/scitex-agent-container?label=Python" alt="Python"></a>
  <a href="https://github.com/ywatanabe1989/scitex-agent-container/actions/workflows/rtd-sphinx-build-on-ubuntu-latest.yml"><img src="https://img.shields.io/github/actions/workflow/status/ywatanabe1989/scitex-agent-container/rtd-sphinx-build-on-ubuntu-latest.yml?branch=develop&label=RTD" alt="RTD"></a>
  <a href="https://www.gnu.org/licenses/agpl-3.0"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue.svg?label=License" alt="License"></a>
</p>
<p align="center">
  <a href="https://github.com/ywatanabe1989/scitex-agent-container/actions/workflows/pytest-matrix-on-ubuntu-py3-11-3-12-3-13.yml"><img src="https://img.shields.io/github/actions/workflow/status/ywatanabe1989/scitex-agent-container/pytest-matrix-on-ubuntu-py3-11-3-12-3-13.yml?branch=develop&label=Tests" alt="Tests"></a>
  <a href="https://github.com/ywatanabe1989/scitex-agent-container/actions/workflows/import-smoke-on-ubuntu-py3-12.yml"><img src="https://img.shields.io/github/actions/workflow/status/ywatanabe1989/scitex-agent-container/import-smoke-on-ubuntu-py3-12.yml?branch=develop&label=Install-Check" alt="Install-Check"></a>
  <a href="https://github.com/ywatanabe1989/scitex-agent-container/actions/workflows/scitex-dev-quality-audit-on-ubuntu-latest.yml"><img src="https://img.shields.io/github/actions/workflow/status/ywatanabe1989/scitex-agent-container/scitex-dev-quality-audit-on-ubuntu-latest.yml?branch=develop&label=Quality" alt="Quality"></a>
  <a href="https://codecov.io/gh/ywatanabe1989/scitex-agent-container"><img src="https://img.shields.io/codecov/c/github/ywatanabe1989/scitex-agent-container/develop?label=CodeCov" alt="CodeCov"></a>
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

[`examples/`](examples/) walks through the runtime in 15 lessons (image build, sandbox/update/freeze, versioning, run/send/tail, logs/exec, stop/remove, binds, env+user, writing your first spec.yaml, dot_claude/, A2A endpoint, health+restart, multi-host, debugging). Run them read-only with `bash examples/00_run_all.sh`, or `--apply` to execute the mutating ones.

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
                              ├── spec.mounts[]  ← host-path allowlist (ro/rw)
                              ├── state-dir  (~/.scitex/agent-container/runtime/<name>/)
                              └─→ POST /v1/turn  (per-agent A2A inbound)
```

**[Full architecture →](docs/how-sac-works.md)** — launch flow, dot_claude merge rules, A2A inbound, control plane, restart/health.

**[YAML Spec Reference (v3) →](docs/spec-reference.md)** — annotated full example + field table (apiVersion, spec.apptainer.*, spec.claude.*, a2a, health, restart).

**[Talking to a Running Agent →](docs/talking-to-agents.md)** — three transports (A2A `POST /v1/turn`, `sac agents send`, host-level `sac listen`), when to use which, copy-pasteable curl examples.

**[Container Isolation →](docs/isolation.md)** — 10 Apptainer-default leak paths + sac's hardened-by-default countermeasures (`--containall` auto-prepended, opt-out via `spec.apptainer.relaxed: true`). The reference for reproducibility claims.

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

[`scitex-orochi`](https://github.com/ywatanabe1989/scitex-orochi) adds cross-host message routing, a Slack-like chatops UI, and a peer registry on top of `sac`. The dependency is one-way — orochi reads sac's on-disk state; sac never imports orochi. For details, see **[docs/sac-and-orochi.md](docs/sac-and-orochi.md)** — architecture diagram, responsibility split, how to wire `server:orochi-push`.


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