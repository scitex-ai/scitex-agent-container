# ADR-0009: Claude setup delivery — `to_home`-first, definition-as-source-of-truth

**Status:** Accepted (2026-05-21).
**Extends:** ADR-0006 (`to_home/` spec-dir materialization) — that ADR
established the 1:1 `to_home/` → container `$HOME` mirror; this one
records the *delivery + load* model that makes a containerized Claude
agent reproducible from its definition alone.
**Tutorial:** skill `_skills/scitex-agent-container/25_claude-setup-delivery.md`
(the how-to; this ADR is the decision record).

## Context

SAC runs each agent's Claude session **inside** an apptainer container.
The SDK runtime (`runtimes/claude_session.py`, backed by
`runtimes/_sdk_common.py`) does not — and must not — auto-discover the
host operator's `~/.claude`. Without an explicit policy, agents would
silently inherit settings, credentials, MCP config, and CLAUDE.md from
whatever host they happen to run on, so a run would not be reproducible
and "works on my box" would be the norm.

ADR-0006 gave us the `to_home/` mirror but did not pin down (a) what
should live in `to_home` versus `spec.yaml`, (b) how conflicting layers
resolve, or (c) how the materialized tree is actually loaded by the SDK
inside the container.

## Decision

### Definition files are the single source of truth

An agent is **fully described by its `spec.yaml` plus its `to_home/`** —
never by the host machine. To understand or reproduce an agent you read
its definition, nothing else. If you reach for host state to make an
agent behave, that is the bug: put it in the definition and pass it
explicitly.

### `to_home`-first

Default **all** `$HOME` content into `to_home/` — `.env`, `.mcp.json`,
`CLAUDE.md`, `.claude/hooks`, `.bashrc`, etc. `to_home/` mirrors the
container `$HOME` 1:1 (it is a `$HOME` delivery mechanism, **not** a
`.claude`-only one). `spec.yaml` shrinks toward container-wiring-only.

Rationale: ordinary files are easy to handle, adding a file adds a
capability (easy to grow), the agent is self-contained (host-isolated),
and the layout is **isomorphic to a normal `$HOME`** — standard tools
and mental models apply with no special knowledge.

### Exception: read-only bind for secrets only

The **only** exception to `to_home`-first is a read-only (`ro`) `bind`
for **host secrets** (`.ssh`, `.config/gh`, `.claude/.credentials.json`)
— a copy would commit secrets into the git-tracked `to_home`. Secrets
are correctly *outside* the reproducible artifact: the structure is
reproduced and values are injected at run time (not a gap).

Large shared trees such as `~/.claude/skills` are **not** an exception.
They are delivered via an explicit `to_home` symlink that materialize
resolves to real content (see "Symlink resolution" below) — never via a
host bind or a host auto-read.

### Symlink resolution — definition is the sole source of truth

Materialize walks **both** the global `_base/to_home/` and the per-agent
`to_home/` and copies their content into the container `$HOME`
(`/home/agent`). The rule for symlinks:

- **Resolve every symlink to its real target content (dereference-copy).**
  The container `$HOME` ends up holding only real, self-contained files
  (and directory trees, with nested symlinks dereferenced too) — closed
  to apptainer regardless of the host filesystem (no dangling host paths
  under `--containall`).
- **A dangling symlink (unresolvable target) fails loud** with
  `DanglingToHomeSymlinkError`, naming the symlink path, its target, and
  what to fix. A dangling definition symlink is a real defect; it is
  never silently kept or skipped.
- **No host auto-read.** There is no unconditional read of the host
  `~/.claude/skills` (and no `~/.claude/skills` fallback). The only way
  host content enters the container is via an **explicit** symlink the
  operator places under `to_home/` — e.g.
  `_base/to_home/.claude/skills -> ~/.claude/skills` — which the walk
  resolves to real content at deploy time. That is explicit-pass, and it
  is intended.
- **No "keep literal symlink" / warn-and-keep / naming-convention
  behavior.** Everything is resolved. The rule applies uniformly to
  skills, hooks, `.env`, and all `to_home` content. (An in-container
  literal symlink, if ever needed, is created via `startup_commands` —
  out of scope for materialization.)

Implementation: `runtimes/_symlink_resolve.py::deref_copy_symlink`,
called from the materialize walk in `runtimes/_to_home.py`.

### Override precedence

When the same setting is declared in more than one layer, the winner is:

```
direct command args  >  spec.yaml fields  >  to_home/<files>
```

`to_home` is the base; `spec` fields and direct args are escape-hatch
overrides. This mirrors Claude's own `user < project < flag` layering.
(Placement and precedence are distinct axes: `to_home`-first is *where*
you put a setting; precedence is *which layer wins* on conflict.)

### Delivery into the container `$HOME`

Where the materialized tree must physically land depends on how the
container gets its `$HOME`:

- **Hardened mode** — the runtime binds the host workspace home at the
  container `$HOME` (`--bind runtime/<name>/home/:/home/agent`);
  `deploy_to_home` materializes the tree there.
- **Relaxed mode** (`--home`/`--overlay` specs) — the operator-declared
  `--home` is satisfied by the overlay's upper layer, which shadows the
  workspace-home bind. `runtimes/_to_home_overlay.py::deploy_to_home_overlay`
  therefore materializes the **same** tree into the overlay upper home
  (`<overlay>/upper/<container_home>/`) so it reaches `$HOME`. This
  applies only to directory overlays; `.img` loopback overlays are a
  no-op.

### Load model

The SDK runner is built with `setting_sources=[]`
(`runtimes/_sdk_common.py::build_sdk_options`). This is **intentional
and must not change**: the default would load the host's `~/.claude`
state files and treat "no state" as not-logged-in even when credentials
are mounted. Empty sources mean the explicitly-delivered setup is the
entire context — no host auto-discovery (machine-independence).

Each surface is then loaded explicitly:

- **Settings/hooks** — `ClaudeAgentOptions.settings` → `--settings`
  pointed at the in-container `$HOME/.claude/settings.local.json`. This
  is the SDK's "flag settings" layer (highest-priority user-controlled
  layer), loaded **independently** of `setting_sources`; without it the
  empty `setting_sources` would never load the delivered settings.
- **MCP** — `--mcp-config`, parsed from the workspace `.mcp.json` that
  sac materializes from `spec.mcp_servers`.
- **Credentials** — the auth layer
  (`provision_anthropic_auth`): `~/.claude/.credentials.json` (Pro/Max
  OAuth, flat-rate) takes precedence, else `SAC_ANTHROPIC_API_KEY`
  mirrored into `ANTHROPIC_API_KEY`. A bare host `ANTHROPIC_API_KEY` is
  never honoured (it is popped when `SAC_ANTHROPIC_API_KEY` is unset).

## Consequences

**Positive:**

- An agent is reproducible from its definition on any host; "works on my
  box" is structurally prevented.
- Config grows by adding ordinary files under `to_home/`; no special
  per-surface plumbing.
- The same delivered tree reaches `$HOME` under both hardened and
  relaxed/overlay specs, via one materialization contract.

**Negative:**

- Skills delivered via a resolved `to_home` symlink are current as of
  deploy time but not version-pinned, so a re-deploy can pick up changed
  host skill content; the materialized tree is self-contained for that
  run, but byte-for-byte reproducibility across deploys requires pinning
  the source the symlink points at.
- A dangling `to_home` symlink now hard-aborts the deploy instead of
  being tolerated — correct, but operators must keep definition symlinks
  resolvable on the deploy host.
- `spec.raw_args` and `to_home` remain separate runtime surfaces today;
  full precedence merging (args > spec > to_home) still relies on the
  runtime resolving them rather than a single merged layer.

## Related work

- ADR-0006 — `to_home/` spec-dir materialization (the 1:1 mirror this
  ADR builds on).
- ADR-0003 — runtime home directory layout (`runtime/<name>/home/`).
- ADR-0001 — isolation hardening (the `--bare`/`setting_sources=[]`
  posture).
- Skill `25_claude-setup-delivery.md` — the operator/maintainer tutorial.
