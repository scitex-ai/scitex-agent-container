<!-- ---
!-- Timestamp: 2026-06-07 03:00:00
!-- Author: ywatanabe
!-- File: /home/ywatanabe/proj/scitex-agent-container/README.md
!-- --- -->

# SciTeX Agent Container (<code>scitex-agent-container</code>)

<p align="center">
  <a href="https://scitex.ai">
    <img src="docs/scitex-logo-blue-cropped.png" alt="SciTeX" width="400">
  </a>
</p>

<p align="center"><b>Declarative, on-prem-first lifecycle manager for autonomous agent processes.</b></p>

<p align="center">
  One YAML spec → one reproducible, sandboxed, fleet-addressable agent.<br/>
  Runs anywhere Apptainer runs — laptop, HPC node, air-gapped server.
</p>

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
  <a href="https://codecov.io/gh/ywatanabe1989/scitex-agent-container"><img src="https://img.shields.io/codecov/c/github/ywatanabe1989/scitex-agent-container/develop?label=CodeCov" alt="CodeCov"></a>
</p>
<!-- scitex-badges:end -->

---

## Why `sac`

| # | What sac gives you                                                                                                                                                                                                                                                                                                                                                                                  |
|---|---|
| 1 | **Declarative agents.** One `spec.yaml` per agent — the file IS the agent (dir-as-SSoT, no hidden state). Reproducible across hosts, version-controlled, diff-reviewable. [`spec-reference.md`](docs/spec-reference.md). |
| 2 | **Rootless Apptainer isolation.** Runs where cloud sandboxes (E2B, Modal, etc.) can't — HPC login nodes, on-prem clusters, fully air-gapped boxes. No root, no daemon, no Docker socket. Hardened by default with `--containall` ([`isolation.md`](docs/isolation.md)). |
| 3 | **Two independent axes, one spec.** sac owns the agent PROCESS; a *harness* owns only the TURN. **Which harness drives the turn** is `spec.harness:` — `anthropic` (default), `openai`, or `codex`. **Which endpoint answers it** is the separate `spec.claude.provider:` knob — Anthropic OAuth (default), Codex/ChatGPT via scitex-genai, DeepSeek, MiMo/Xiaomi, or any Anthropic-compatible `base_url`. Swapping the endpoint does not change the harness, and vice versa. The clearest proof the axes are different: `codex` is a legal value of **both**, and it means different things — as a *harness* the Codex agent program runs the loop; as a *provider* Claude Code still drives and Codex only answers. **[What actually starts today →](#which-harnesses-actually-start)** — the registry has four entries and only the `anthropic` ones can be started. [`spec-reference.md`](docs/spec-reference.md). |
| 4 | **Fleet ops out of the box.** A2A push (`POST /v1/turn` per agent, native), health & heartbeat, restart policies, multi-account credential rotation with auto-quota-watch, MCP + CLI + Python surface, cross-host orchestration via `sac fleet`. |
| 5 | **AGPL-3.0.** Research-freedom license — infrastructure stays open, modifications stay shareable. The [Four Freedoms for Research](#four-freedoms-for-research) below. |

## Installation

```bash
uv pip install "scitex-agent-container[all]"
```

Or via the SciTeX umbrella: `uv pip install "scitex[agent-container]"` → use as `scitex agent-container ...` (CLI) or `import scitex.agent_container` (Python).

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

# Check status (fleet view)
sac agents status

# Start in background, send a follow-up turn, tail, stop, delete
sac agents start  hello-agent-1 hello-agent-2
sac agents send   hello-agent-1 "What is 2+2? Reply with just the number."
sac agents tail   hello-agent-1 hello-agent-2 --json
sac agents stop   hello-agent-1 hello-agent-2
sac agents delete hello-agent-1 hello-agent-2 -y
```

### Tutorial

[`examples/`](examples/) walks through the runtime in 15 lessons (image build, sandbox/update/freeze, versioning, run/send/tail, logs/exec, stop/remove, binds, env+user, writing your first spec.yaml, to_home/, A2A endpoint, health+restart, multi-host, debugging). Run them read-only with `bash examples/00_run_all.sh`, or `--apply` to execute the mutating ones. Pre-baked agent specs live in [`examples/agents/`](examples/agents/) (`hello-agent`, `minimal-agent`, `full-agent`, `codex-agent`, `deepseek-agent`, `proxy-agent`).

### Which harnesses actually start

The harness registry has **four** entries, and `spec.harness` accepts **three**
values. Only the `anthropic` ones can be started today — a registry entry is a
declaration, not a working launch path, and this table says which is which
rather than letting the count imply support that is not there:

| `spec.harness` | Registry entry     | Selected by                                       | `sac agents start`? |
|----------------|--------------------|---------------------------------------------------|---------------------|
| `anthropic` *(default)* | `claude-code-tui`  | `spec.runtime: tui`, or unset                     | **yes** |
| `anthropic`    | `claude-agent-sdk` | `spec.runtime: claude-agent-sdk` (legacy alias `apptainer`) | **yes** |
| `openai`       | `openai-agents`    | the harness axis alone                            | **no** — refused |
| `codex`        | `codex-sdk`        | the harness axis alone                            | **no** — refused |

A non-`anthropic` harness loads, validates and resolves to its registry entry,
but every lifecycle launch path refuses it *loudly* rather than silently
starting a Claude runner under a spec that asked for something else. The one
working alternative today is `spec.a2a.handler: openai_session` for the OpenAI
SDK; there is no equivalent A2A executor for `codex` yet.

`spec.runtime` only discriminates *within* the `anthropic` family — the `openai`
and `codex` families have a single entry each, so the runtime axis selects
nothing for them.

### Models

`spec.claude.model` is the per-agent model knob. Be aware that sac resolves it
to `sonnet` when a spec leaves it empty — for **every** agent, whichever harness
the spec selects. That Anthropic-shaped default is real and not yet unwound by
the harness/runtime/inference layering work.

Anthropic aliases:

| Alias    | Model (current)             | Use for                         |
|----------|-----------------------------|---------------------------------|
| `opus`   | Claude Opus 4.7             | Hardest reasoning; slowest      |
| `sonnet` | Claude Sonnet 4.6 *(default)* | Balanced capability and speed |
| `haiku`  | Claude Haiku 4.5            | Fast, cheap, light tasks        |

Aliases auto-track the latest version of each family; append `[1m]` for the 1M-token context window (`opus[1m]`, `sonnet[1m]`). Pin an exact build with a full ID like `claude-opus-4-7` or `claude-haiku-4-5-20251001`.

**A non-Anthropic inference endpoint?** `spec.claude.provider` swaps the
ENDPOINT, not the harness — the Claude-family harness keeps running, pointed
somewhere else. Set `spec.claude.provider: codex` with a GPT model to use the
local scitex-genai ChatGPT-subscription gateway. Other bundled entries are
`deepseek`, `mimo`, and `xiaomi`; a dict `{ base_url: "...", auth_token_env:
"..." }` accepts any Anthropic-compatible endpoint. Under an override the model
id is the endpoint's own (e.g. `deepseek-chat`), so the `claude-*` alias check
relaxes and the table above no longer applies. Codex setup and multi-account
configuration are documented in [`docs/credentials.md`](docs/credentials.md).
See [`examples/agents/deepseek-agent/`](examples/agents/deepseek-agent/) for the
generic provider shape. **[Full harness + model + provider reference →](docs/spec-reference.md)**

## How it works

`sac` materializes a `spec.yaml` into a long-lived, externally addressable agent process — whichever harness drives its turns:

```
  spec.yaml   ─┐
  to_home/    ─┴─→ sac agents start ──→ apptainer instance
                                          │
                                          ▼
                              long-lived harness session (TUI or SDK)
                              │
                              ├── <workdir>  (= spec.workdir, mounted rw)
                              ├── spec.mounts[]  ← host-path allowlist (ro/rw)
                              ├── state-dir  (~/.scitex/agent-container/runtime/<name>/)
                              └─→ POST /v1/turn  (per-agent A2A inbound)
```

**SAC-from-SAC (in-SIF spawn).** An agent running INSIDE an apptainer SIF can spawn a child agent on the **bare host** by calling `sac agents start <child>` as normal — the CLI auto-detects the in-SIF condition (`APPTAINER_CONTAINER`) and POSTs the spawn RPC to the host's `sac listen` instead of trying nested apptainer (which the supported HPC shape forbids). The host re-runs ACL gating, records the parent → child lineage, and shells the real start against the bare host's apptainer. Wiring is automatic: `SAC_LISTEN_BASE_URL` + `SAC_LISTEN_BEARER` are injected at container launch.

**[Full architecture →](docs/how-sac-works.md)** — launch flow, to_home merge rules, A2A inbound, control plane, restart/health.

**[YAML Spec Reference (v3) →](docs/spec-reference.md)** — annotated full example + field table (apiVersion, spec.harness, spec.runtime, spec.apptainer.*, spec.claude.*, a2a, health, restart, provider).

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

### Host listen as a persistent service

`sac listen` is the host's HTTP/JSON control plane (push hub, spawn broker, lead inbox). For long-running deployments install the bundled systemd-user unit so it auto-starts on boot and auto-restarts on crash:

```bash
install -m 0644 scripts/systemd/sac-listen.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sac-listen.service
journalctl --user -u sac-listen.service -n 50          # logs
curl -s http://127.0.0.1:7878/v1/health                # healthcheck
```

See [`scripts/systemd/README.md`](scripts/systemd/README.md) for the full recipe + the federated-jobs vs hand-maintained-services split.

## Three Interfaces

<details open>
<summary><strong>CLI ⭐⭐⭐ (primary)</strong></summary>

<br>

```bash
# Agent lifecycle
sac agents start  <name> [--foreground]   # daemon by default; --foreground streams stdio
                                           # inside a SIF: auto-brokers to host listen
                                           # (no apptainer-in-apptainer needed)
sac agents stop   <name>                  # graceful SIGTERM, escalate to SIGKILL after 5 s
                                           # --force tolerates an unreachable bound host
sac agents restart <name>
sac agents delete <name>                  # stop + remove spec dir + runtime dir + registry
sac agents forget <name> [--force]        # local-only state.db cleanup for the
                                           # "agent is gone, only stale rows persist" case
                                           # (no ssh, no signal)
sac agents send   <name> "<prompt>"       # send a follow-up turn to a running session
sac agents send   <name> --key ESC        # interrupt current turn
sac agents status [<name>] [--snapshot] [--priority]   # FLEET-WIDE view if no name;
                                                       # per-agent JSON payload otherwise
sac agents list   [<name>]                # alias of `status` (same renderer)
sac agents list   --host <hostname>       # one host; repeatable, exact match.
                                           # `localhost` is resolved at parse time and
                                           # the header echoes the resolution.
                                           # The fleet view ALWAYS prints a header saying
                                           # which hosts answered and which did not, with
                                           # the reason ("5/6 hosts responded — spartan:
                                           # ssh timed out after 8s"). A host that could
                                           # not be reached is REPORTED, never dropped:
                                           # `agents: []` means an EMPTY fleet only when
                                           # `hosts.responded == hosts.total` in --json.
sac agents health <name>
sac agents tail   <name>                  # render session.jsonl (structured transcript)
sac agents recall <name>                  # human-readable session summary
sac agents check  <name>                  # preflight (validates yaml + probes runtime deps)
sac agents find   <capability>            # search by metadata.labels.capabilities

# Control plane (HTTP/JSON, loopback-only)
sac listen [--bind 127.0.0.1:7878]        # boot per-host REST API (bearer-auth)
                                           # single-instance flock guard fails loud
                                           # on a duplicate launch (PID + lockfile shown)
sac listen restart                        # atomic stop-clean-relaunch
sac peer post-turn <to> "<msg>"           # local agent-to-agent message via sac listen
sac peer resolve-url <to>                 # print URL post-turn would target

# A2A protocol (generic, no fleet deps)
sac a2a serve <yamls...>                  # inbound HTTP for non-SDK runtimes
                                           # (apptainer-runtime agents host /v1/turn themselves)
sac a2a doctor <agent>                    # probe AgentCard endpoint
sac a2a grant / revoke / block / unblock / grants

# Image lifecycle (delegates to scitex-container)
sac image build [base|scitex] [--sandbox]
sac image sandbox SOURCE                  # SIF → writable sandbox
sac image update  SANDBOX [-p PKG]        # pip install --upgrade
sac image freeze  SANDBOX OUT.sif         # sandbox → SIF
sac image list                            # installed versions
sac image switch  VERSION                 # atomic flip
sac image rollback                        # restore previous
sac image status                          # unified dashboard
sac image snapshot [-o env.json]          # reproducibility capsule

# Accounts / quota (multi-account rotation)
sac accounts list / save / delete / switch        # stored-credential rotation
sac accounts status                       # one-shot quota snapshot (5h%, 7d%, tier)
sac accounts quota                        # this agent's own live quota
sac accounts refresh                      # mint fresh access_token from refresh_token
sac accounts sync-live / watch-live       # auto-snapshot live cred on `claude /login`
sac accounts watch-quota                  # auto-rotate when quota threshold hit

# Network / peers
sac host list / add / remove / set / probe / exec / validate
sac host ssh-opts                         # print sac's ssh ControlMaster flags (shell-quoted)
sac host add-peer / list-peers / remove-peer      # cross-host listen-bearer registry
sac host probe-hub                        # WSL → fleet-hub layered connectivity probe

# Fleet (peer-aware multi-agent orchestration)
sac fleet launch  PEER <name>...          # rsync specs to PEER, start each remotely
sac fleet notify  done|blocker|status --summary "..."   # agent→lead push (ADR-0013)
sac fleet sync                            # cross-host spec audit (fails loud on drift)

# Diagnostics / introspection
sac doctor [--fleet]                      # diagnose agent-spec source drift
sac doctor --pollers                      # >1 live Telegram poller per bot token?
sac subagent get-state                    # Claude Code Agent-tool subagent state
sac mcp list-tools                        # MCP introspection
sac skills list / get                     # bundled agent-facing skills

# Federated scheduled jobs (delegates to scitex-dev ecosystem)
sac dev systemd list / install / uninstall    # kind=timer|service -> ~/.config/systemd/user/sac.*
sac dev cron    list / install / uninstall    # kind=cron -> crontab entries

# State db / registry / events
sac db query / show / clean / export / import / migrate / tick   # state.db inspection
sac registry sync / reconcile             # cross-host comms_nodes anti-entropy
sac event ingest                          # Claude Code hook event ingestor

# Misc
sac installation boot                     # first-time host bootstrap (venv, PATH, cron)
sac list-python-apis                      # enumerate public Python API
sac --help-recursive                      # full subcommand tree
```

</details>

<details>
<summary><strong>Python ⭐⭐</strong></summary>

<br>

```python
# Direct import
import scitex_agent_container as sac

cfg = sac.load_config("~/.scitex/agent-container/agents/hello-agent-1/spec.yaml")
sac.validate_config(cfg)
sac.agent.start("hello-agent-1")           # daemon
sac.agent.status("hello-agent-1")          # dict matching `sac agents status --json`
sac.peer.post_turn("hello-agent-1", "What is 2+2?")
```

```python
# Or via the umbrella
import scitex
scitex.agent_container.agent.start("hello-agent-1")
```

See [`docs/spec-reference.md`](docs/spec-reference.md) for `AgentConfig` fields.

</details>

<details>
<summary><strong>MCP ⭐</strong> (no server bundled — agents spawn their own)</summary>

<br>

sac itself does not ship an MCP server. Each agent declares its own MCP servers in `spec.mcp_servers` (which is mirrored into `$HOME/.mcp.json` at start via `to_home/`), so per-agent MCP surface is part of the YAML spec rather than a sac-global service.

```yaml
spec:
  mcp_servers:
    filesystem:
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/work"]
```

</details>

## Part of SciTeX

`scitex-agent-container` is part of [**SciTeX**](https://scitex.ai). Install via the umbrella with `pip install scitex[agent-container]` to use as `scitex.agent_container` (Python) or `scitex agent-container ...` (CLI).

An external fleet hub can add cross-host message routing, a chatops UI, and a peer registry on top of `sac`. The dependency is one-way — the hub reads sac's on-disk state and published HTTP endpoints; sac never imports it. See **[docs/adr/0008-sac-node-transport-boundary.md](docs/adr/0008-sac-node-transport-boundary.md)** for the transport boundary sac guarantees.

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
