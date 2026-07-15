<!-- ---
!-- Timestamp: 2026-07-16
!-- Author: ywatanabe
!-- File: /home/ywatanabe/proj/scitex-agent-container/README.md
!-- --- -->

# SciTeX Agent Container (<code>scitex-agent-container</code>)

<p align="center">
  <a href="https://scitex.ai">
    <img src="docs/scitex-logo-blue-cropped.png" alt="SciTeX" width="400">
  </a>
</p>

<p align="center"><b>Declarative, on-prem-first lifecycle manager for a fleet of Claude Code agents — one master, many hosts.</b></p>

<p align="center">
  One YAML spec → one reproducible, sandboxed, fleet-addressable agent.<br/>
  Define every agent on one master node; place any of them on a laptop, an HPC login node, or a compute node — and drive the whole fleet from one place.
</p>

<p align="center">
  <a href="https://scitex-agent-container.readthedocs.io/">Full Documentation</a> · <a href="docs/README.md">Docs Index</a> · <code>uv pip install scitex-agent-container[all]</code>
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
  <a href="https://github.com/ywatanabe1989/scitex-agent-container/actions/workflows/quality-audit-on-ubuntu-latest.yml"><img src="https://img.shields.io/github/actions/workflow/status/ywatanabe1989/scitex-agent-container/quality-audit-on-ubuntu-latest.yml?branch=develop&label=Quality" alt="Quality"></a>
  <a href="https://codecov.io/gh/ywatanabe1989/scitex-agent-container"><img src="https://img.shields.io/codecov/c/github/ywatanabe1989/scitex-agent-container/develop?label=CodeCov" alt="CodeCov"></a>
</p>
<!-- scitex-badges:end -->

---

## What `sac` is

`sac` (scitex-agent-container) is a **container-based, multi-agent fleet orchestrator** for the SciTeX ecosystem. Each agent is a long-lived Claude Code session, fully described by a single `spec.yaml`, and launched into a rootless Apptainer sandbox (or a plain tmux/TUI session). One master node holds every agent's definition and the authoritative registry; a one-line `host:` field in a spec places that agent on any reachable host — a laptop, an HPC login node, or (via multi-hop SSH) a compute node — while `sac agents list / attach / restart` reach across hosts to drive the whole fleet from one terminal.

## Why `sac`

| # | What sac gives you |
|---|---|
| 1 | **Declarative agents.** One `spec.yaml` per agent — the file IS the agent (dir-as-SSoT, no hidden state). Reproducible across hosts, version-controlled, diff-reviewable. [`02_agent-spec.md`](docs/02_agent-spec.md). |
| 2 | **Master-authoritative cross-host control plane.** Definitions and the registry live only on the master; `spec.host:` routes an agent to a peer over SSH (multi-hop `ProxyJump`-aware). `sac agents list / attach / restart` live-probe and drive remote agents from the master. [`03_cross-host-control-plane.md`](docs/03_cross-host-control-plane.md). |
| 3 | **Rootless Apptainer isolation.** Runs where cloud sandboxes (E2B, Modal, …) can't — HPC login nodes, on-prem clusters, air-gapped boxes. No root, no daemon, no Docker socket. Hardened by default with `--containall`. [`isolation.md`](docs/isolation.md). |
| 4 | **A2A mesh, out of the box.** Each host runs a `sac listen` control plane; peers forward to each other over SSH so any agent can reach any other by name (`POST /v1/turn`). Health, heartbeat, restart policies, and multi-account credential rotation with auto-quota-watch are built in. [`04_listen-and-a2a.md`](docs/04_listen-and-a2a.md). |
| 5 | **LLM-agnostic & on-prem capable.** Default: Anthropic OAuth with a rotating multi-account pool. Alternative: any Anthropic-API-compatible endpoint (DeepSeek, MiMo/Xiaomi, a self-hosted LiteLLM / vLLM-with-Anthropic-shim gateway) via a one-line `spec.claude.provider:` knob. Data, code, and inference can stay entirely on your network. |
| 6 | **AGPL-3.0.** Research-freedom license — infrastructure stays open, modifications stay shareable ([Four Freedoms for Research](#four-freedoms-for-research)). |

## Installation

```bash
uv pip install "scitex-agent-container[all]"
```

Or via the SciTeX umbrella: `uv pip install "scitex[agent-container]"` → use as `scitex agent-container ...` (CLI) or `import scitex.agent_container` (Python).

## Quickstart

**1 — Build the runtime image (one-time, ~5 min)**

```bash
sac image build base
```

**2 — Scaffold an agent spec**

```bash
sac agents create hello-agent          # writes ~/.scitex/agent-container/agents/hello-agent/spec.yaml
```

A minimal `spec.yaml` looks like this (edit the scaffold, or copy a bundled example from [`examples/agents/`](examples/agents/)):

```yaml
# ~/.scitex/agent-container/agents/hello-agent/spec.yaml
apiVersion: scitex-agent-container/v3
kind: Agent

spec:
  runtime: apptainer                      # or `tui` (tmux + Claude Code CLI)
  # host: spartan                         # ← omit to run locally; name a peer to place it there
  apptainer:
    image: ~/.scitex/agent-container/containers/sac-base.sif
  claude:
    model: haiku
    flags: [--dangerously-skip-permissions]
  startup_prompts:
    - "Reply with the string 'Hello! I am hello-agent' and nothing else."
  health:   { enabled: true, interval: 60 }
  restart:  { policy: never }
```

**3 — Run it and drive the fleet**

```bash
sac agents start   hello-agent            # daemon by default; --foreground streams stdio
sac agents list                           # fleet view — one row per running agent (local + remote)
sac agents attach  hello-agent            # attach your terminal to its TUI (Ctrl-b d to detach)
sac agents send    hello-agent "What is 2+2? Reply with just the number."
sac agents tail    hello-agent --json     # structured session transcript
sac agents restart hello-agent -y
sac agents stop    hello-agent
sac agents delete  hello-agent -y
```

**4 — (optional) Boot the host control plane** so agents can reach each other:

```bash
sac listen start                          # HTTP/JSON control plane on 127.0.0.1:7878 (bearer-auth)
sac listen status                         # health report; exit 1 if down
```

> **Placing an agent on another host** is a one-line change: set `host: spartan` in the spec and run `sac agents start <name>` from the master. sac rsyncs the spec to the peer, starts it there over SSH, and records it in the master's registry so `sac agents list` shows it as running-on-peer. See [Cross-host control plane](#master-authoritative-cross-host-control-plane).

### Tutorial

[`examples/`](examples/) walks through the runtime in ~15 lessons (image build, sandbox/update/freeze, versioning, run/send/tail, logs/exec, stop/remove, binds, env+user, writing your first `spec.yaml`, `to_home/`, the A2A endpoint, health+restart, multi-host, debugging). Run them read-only with `bash examples/00_run_all.sh`, or `--apply` to execute the mutating ones. Pre-baked specs live in [`examples/agents/`](examples/agents/) (`hello-agent`, `minimal-agent`, `full-agent`, `deepseek-agent`, `proxy-agent`).

## How it works

`sac` materializes a `spec.yaml` (plus an optional `to_home/` overlay) into a long-lived, externally addressable Claude agent:

```
  spec.yaml   ─┐
  to_home/    ─┴─→ sac agents start ──→ apptainer instance (or tmux/TUI)
                                          │
                                          ▼
                              long-lived Claude session
                              │
                              ├── $HOME   (runtime/<name>/home/, bind-mounted; CLAUDE.md/.mcp.json/.env ← to_home/)
                              ├── <workdir>       (bound rw)
                              ├── spec.mounts[]   ← host-path allowlist (ro/rw)
                              ├── state-dir       (~/.scitex/agent-container/runtime/<name>/: pid, heartbeat, session.jsonl)
                              └─→ POST /v1/turn   (per-agent A2A inbound)
```

**[Full single-agent launch flow →](docs/how-sac-works.md)** — to_home merge rules, A2A inbound, restart/health.

### Master-authoritative cross-host control plane

Every agent is **defined once, on the master** (`ywata-note-win`), which also holds the **authoritative registry** (`state.db`). A spec's `host:` field is the only thing that decides *where* the agent runs — the master dispatches it there over SSH and keeps driving it remotely. Peers hold no definitions; they hold only a runtime SIF, host-local comms config, credentials, and the running process.

```
                        ┌──────────────────────────────────────────────────────┐
                        │                  MASTER · ywata-note-win               │
   agent specs (SSoT) ─▶│  ~/.scitex/agent-container/agents/<name>/spec.yaml     │
   one dir = one agent  │        │                                              │
                        │        │  spec.host: <peer>           ┌─────────────┐ │
   authoritative     ──▶│        ▼                              │  state.db   │ │
   registry             │  sac agents start ──────────────────▶│  instances  │ │
                        │  sac agents list / attach / restart   │ comms_nodes │ │
                        │        (cross-host, node-aware)       └─────────────┘ │
                        │        │        ▲ live-probe                          │
                        │  sac listen :7878 ───────────  a2a mesh  ───────────┐ │
                        └────────┼────────┼─────────────────────────────────┼─┘
              ssh: rsync spec +  │        │ ssh: remote probe        ssh-curl │  peer /v1/turn
              `start --no-redispatch`     │      / tmux attach       forward  │  (agent → agent, by name)
                                 ▼        │                                   ▼
              ┌───────────────────────────────┐        ┌──────────────────────────────┐
              │   PEER · spartan (login node)  │◀───────│   PEER · mba / nas / …        │
              │   sac listen (own bearer)      │        │   sac listen (own bearer)     │
              │   SIF / TUI runtime            │        │   … each peer runs its own    │
              │   agent: spartan-dev  a2a:19002│        │      listen, peered back      │
              └───────────────┬────────────────┘        └──────────────────────────────┘
                              │  ssh -J spartan   (ProxyJump multi-hop)
                              ▼
              ┌───────────────────────────────┐
              │  spartan-bmNNN (compute node)  │   spec.host: spartan-bmNNN
              │  SIF runtime (GPU/CPU lease)   │   glob peer — inherits Spartan's registry
              │  agent: <remote agent>         │   root via its `via:` ProxyJump chain
              └───────────────────────────────┘
```

1. **Host-field routing.** `spec.host: spartan` makes `sac agents start <name>` (run on the master) rsync the spec dir to the peer, invoke `sac agents start <name> --no-redispatch --json` there over SSH, and write a master-side `state.db` `instances` row so the fleet view sees the remote agent immediately. An empty/absent `host:` (or one naming the master) runs locally — an unroutable host is a loud error, never a silent local start on the wrong machine.
2. **Multi-hop placement.** A compute-node target (`host: spartan-bmNNN`) is reached with OpenSSH `ProxyJump` (`-J login,…`) from the peer's `via:` chain — no bespoke tunnelling. Glob peers that are not registry rows inherit their login node's pinned state root.
3. **Cross-host liveness & attach.** `sac agents list` live-probes each remote agent *on its own host* (a non-login-shell tmux probe, so a healthy session never reads DEAD); `sac agents attach` / `restart` reach the agent over SSH. You drive the entire fleet from the master.
4. **A2A mesh.** Each host runs its own `sac listen` with its own bearer token; listens forward to each other as peers over SSH-`curl`, so an agent on one host reaches an agent on another **by name** (resolved through the federated `comms_nodes` registry), not by hard-wired URL.

**[Full cross-host runbook →](docs/03_cross-host-control-plane.md)** · **[ADR-0020 →](docs/adr/0020-cross-host-spartan-agent-placement.md)**

## Key concepts

**Agent spec (`spec.yaml`) + host routing.** One directory per agent; the directory name is the agent name (no `name:` field). The spec fully describes the agent: `runtime`, `apptainer`/image + binds, `claude` model/provider/credentials/channels, `a2a` port, `health`, `restart`, and startup prompts/commands. The `host:` field routes placement; `to_home/` next to the spec seeds the agent's `$HOME` (CLAUDE.md, `.mcp.json`, `.env`, `.claude/`). See [`02_agent-spec.md`](docs/02_agent-spec.md).

**`sac listen` + A2A.** `sac listen` is the per-host HTTP/JSON control plane (default `127.0.0.1:7878`, bearer-authenticated, loopback-only) — the plane every agent reaches the host through, and the broker for cross-host spawns and pushes. Agent-to-agent messaging is `POST /v1/turn`; `sac peer post-turn <agent> "<msg>"` and `sac a2a` cover the generic surface. See [`04_listen-and-a2a.md`](docs/04_listen-and-a2a.md) and [`talking-to-agents.md`](docs/talking-to-agents.md).

**Credential / account pool.** `sac accounts` manages a pool of stored Claude credentials for rotation: `save`, `switch`, `refresh` (mint a fresh access token), `status`/`quota` (5h% / 7d% / tier), `sync-live` / `watch-live` (auto-snapshot the live login), and `watch-quota` (auto-rotate when a quota threshold is hit). A spec lists `claude.credentials_files: [...]`; the runtime picks a healthy one at launch. See [`05_credentials-and-accounts.md`](docs/05_credentials-and-accounts.md).

**Apptainer + TUI runtimes.** `spec.runtime` selects the launch mode. `apptainer` runs the agent as a Claude SDK session inside a rootless SIF, hardened with `--containall` (opt out per-agent with `apptainer.relaxed: true`); `tui` runs Claude Code in a tmux session you can `sac agents attach` to. `docker` / `podman` are also accepted. See [`isolation.md`](docs/isolation.md) and [`images.md`](docs/images.md).

## Three Interfaces

<details open>
<summary><strong>CLI ⭐⭐⭐ (primary)</strong></summary>

<br>

Top-level command groups (run `sac <group> --help`, or `sac --help-recursive` for the full tree):

| Group | What it does |
|-------|--------------|
| `sac agents`   | Agent lifecycle: `create`, `start`, `stop`, `restart`, `delete`, `rename`, `twin`; interact (`send`, `attach`); inspect (`list`/`status`, `health`, `tail`, `recall`); `find`, `check`. |
| `sac listen`   | Per-host HTTP/JSON control plane: `start` / `stop` / `restart` / `status` (bare `sac listen` is deprecated). |
| `sac peer`     | Outbound A2A: `post-turn` into another agent's `/v1/turn`; `resolve-url`. |
| `sac a2a`      | Generic A2A protocol surface: `serve`, `doctor`, `grant` / `revoke` / `block` / `grants`. |
| `sac host`     | Local host identity + peer routing: `add`/`remove`/`set`, `add-peer`/`list-peers`, `probe`, `exec`, `ssh-opts`, `sync`, `validate`. |
| `sac fleet`    | Peer-aware orchestration: `launch` (rsync + start on a peer), `notify` (agent→lead event), `sync` (cross-host spec audit). |
| `sac accounts` | Multi-account credential rotation: `save`/`switch`/`delete`, `refresh`, `status`/`quota`, `sync-live`/`watch-live`, `watch-quota`. |
| `sac image`    | Apptainer image lifecycle (delegates to scitex-container): `build`, `sandbox`, `update`, `freeze`, `list`, `switch`, `rollback`, `status`, `snapshot`. |
| `sac db`       | Inspect/maintain `state.db`: `show`, `query`, `clean`, `export`/`import`, `migrate`, `tick`. |
| `sac doctor`   | Diagnose agent-spec source drift (`--fleet` across hosts). |
| `sac installation` · `sac dev` · `sac mcp` · `sac skills` · `sac provenance` · `sac ports` · `sac pytest` | Host bootstrap, maintainer plumbing, MCP/skills introspection, provenance/ports diagnostics, remote (SLURM) pytest. |

> **[Full CLI reference →](docs/06_cli-reference.md)** · run `sac --help-recursive` for the live subcommand tree.

</details>

<details>
<summary><strong>Python ⭐⭐</strong></summary>

<br>

```python
import scitex_agent_container as sac

cfg = sac.load_config("~/.scitex/agent-container/agents/hello-agent/spec.yaml")
sac.validate_config(cfg)
sac.agent.start("hello-agent")             # daemon
sac.agent.status("hello-agent")            # dict matching `sac agents status --json`
sac.peer.post_turn("hello-agent", "What is 2+2?")
```

Or via the umbrella: `import scitex; scitex.agent_container.agent.start("hello-agent")`. See [`02_agent-spec.md`](docs/02_agent-spec.md) for `AgentConfig` fields.

</details>

<details>
<summary><strong>MCP ⭐</strong> (no server bundled — agents spawn their own)</summary>

<br>

sac itself does not ship an MCP server. Each agent declares its own MCP servers under `spec.mcp_servers`, mirrored into `$HOME/.mcp.json` at start via `to_home/`, so the per-agent MCP surface is part of the YAML spec rather than a sac-global service.

```yaml
spec:
  mcp_servers:
    filesystem:
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/work"]
```

</details>

## Documentation

Start at the **[Docs Index](docs/README.md)**. Highlights:

- [`01_architecture.md`](docs/01_architecture.md) — the big picture: spec → runtime → control plane.
- [`02_agent-spec.md`](docs/02_agent-spec.md) — the `spec.yaml` schema (v3), field by field.
- [`03_cross-host-control-plane.md`](docs/03_cross-host-control-plane.md) — master-authoritative placement, host-field routing, multi-hop.
- [`04_listen-and-a2a.md`](docs/04_listen-and-a2a.md) — the listen server and agent-to-agent mesh.
- [`05_credentials-and-accounts.md`](docs/05_credentials-and-accounts.md) — the credential/account pool and quota rotation.
- [`06_cli-reference.md`](docs/06_cli-reference.md) — every command group and flag.

Deep-dives: [`how-sac-works.md`](docs/how-sac-works.md) · [`spec-reference.md`](docs/spec-reference.md) · [`isolation.md`](docs/isolation.md) · [`images.md`](docs/images.md) · [`directories.md`](docs/directories.md) · [`talking-to-agents.md`](docs/talking-to-agents.md) · [`credentials.md`](docs/credentials.md) · [`sac-and-orochi.md`](docs/sac-and-orochi.md) · [ADRs](docs/adr/).

### Host `sac listen` as a persistent service

For long-running deployments, install the bundled systemd-user unit so the control plane auto-starts on boot and auto-restarts on crash:

```bash
install -m 0644 scripts/systemd/sac-listen.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sac-listen.service
curl -s http://127.0.0.1:7878/v1/health           # healthcheck
```

See [`scripts/systemd/README.md`](scripts/systemd/README.md) for the full recipe.

## Part of SciTeX

`scitex-agent-container` is part of [**SciTeX**](https://scitex.ai). Install via the umbrella with `pip install scitex[agent-container]` to use as `scitex.agent_container` (Python) or `scitex agent-container ...` (CLI).

[`scitex-orochi`](https://github.com/ywatanabe1989/scitex-orochi) adds cross-host message routing, a Slack-like chatops UI, and a peer registry on top of `sac`. The dependency is one-way — orochi reads sac's on-disk state; sac never imports orochi. See **[docs/sac-and-orochi.md](docs/sac-and-orochi.md)**.

## Four Freedoms for Research

> Four Freedoms for Research
>
> 0. The freedom to **run** your research anywhere — your machine, your terms.
> 1. The freedom to **study** how every step works — from raw data to final manuscript.
> 2. The freedom to **redistribute** your workflows, not just your papers.
> 3. The freedom to **modify** any module and share improvements with the community.
>
> AGPL-3.0 — because we believe research infrastructure deserves the same freedoms as the software it runs on.

---

<p align="center">
  <a href="https://scitex.ai" target="_blank"><img src="docs/scitex-icon-navy-inverted.png" alt="SciTeX" width="40"/></a>
</p>

<!-- EOF -->
