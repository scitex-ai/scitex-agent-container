---
description: |
  [TOPIC] scitex-agent-container — how sac passes Claude setup explicitly into apptainer agents for reproducibility
  [DETAILS] The to_home → container $HOME 1:1 mirror (general, not just .claude), overlay/--home delivery for relaxed specs, explicit --settings hook load, setting_sources=[] for machine-independence (no host ~/.claude auto-discovery), and the credentials/MCP/hooks loading model (credentials via auth layer, MCP via --mcp-config, hooks via --settings).
tags: [scitex-agent-container-claude-setup-delivery]
---

# Claude setup delivery into apptainer agents

## Core doctrine — the definition files are the single source of truth

**To understand or reproduce an agent you should only ever need to read its
definition: the `spec.yaml` plus its `to_home/`. Never the host machine.**
Nothing about an agent is implicit or inherited from whatever box it happens
to run on. This is *why* everything below exists — it is not a set of
mechanisms first, it is this principle, enforced by mechanisms.

SAC runs each agent's Claude session **inside** an apptainer container and
never auto-discovers the host operator's `~/.claude` (the `claude --bare`
philosophy). Every piece of Claude setup — settings, hooks, MCP config,
credentials, CLAUDE.md, `.bashrc`, `.env` — is passed **explicitly**, declared
in the agent's definition, so a run is identically reproducible on any host
(machine-independence). If you find yourself reaching for host state to make
an agent behave, that is the bug: put it in the definition (`to_home`/`spec`)
and pass it explicitly.

## `to_home`-first: the default placement, with a `ro`-bind exception

**Default everything to `to_home`.** The whole agent `$HOME` — `.env`,
`.mcp.json`, `CLAUDE.md`, `.claude/hooks`, instructions, `.bashrc` — should be
declared as `to_home` files and materialized into the container. `spec.yaml`
should shrink toward *container wiring only* (image, overlay, runtime flags);
the agent's *contents* live in `to_home`.

Why `to_home`-first beats stuffing config into `raw_args`/`startup_prompts`:

1. **Easier to handle** — ordinary files, edited with ordinary tools.
2. **Easier to grow** — add a capability by adding a file, no spec schema churn.
3. **Host-isolated** — the tree is self-contained; nothing leaks in from the host.
4. **Isomorphic to a normal `$HOME`** — it *is* just a home dir (`.bashrc`,
   `.env`, `.ssh`, `.claude`), so standard conventions, tools, and mental
   models apply directly. No special knowledge is needed to read, reason about,
   or reproduce an agent. (Migration target: today's
`raw_args: --env GIT_AUTHOR_*` move to `to_home/.env`; `startup_prompts` move
to `to_home/CLAUDE.md`.)

The **only** exception is read-only `bind`s, used for two narrow cases where a
materialized copy is wrong:

- **Host secrets** — `.ssh`, `.config/gh`, `.claude/.credentials.json`. A copy
  would land secrets in the git-tracked `to_home` source; a `ro`-bind keeps
  them out of git and always current.
- **Large shared, always-current trees** — `~/.claude/skills`. A copy would
  duplicate and go stale; a `ro`-bind shares the live host copy.

Both are acceptable mechanisms, but `to_home` is the first choice; reach for a
`ro`-bind only for secrets or shared-live trees.

### Placement default vs override precedence (two different axes)

Two principles that sound similar but govern different things — they coexist:

- **Placement default (`to_home`-first):** where you *put* a setup. Default to
  `to_home`.
- **Override precedence (when the same key is declared in several layers, which
  wins):**

  ```
  direct command arguments  >  spec.yaml fields  >  to_home/<files>
  ```

  `to_home` is the **base** (declared defaults); `spec.yaml` fields override it;
  explicit command arguments / `raw_args` are the final override. This mirrors
  Claude's own settings precedence (user < project < flag). So: declare the
  baseline in `to_home`, and use `spec`/args only as escape hatches to override
  a specific value without editing the `to_home` base. (Implementing this
  cleanly requires the runtime to merge the layers and resolve overrides —
  today `spec.raw_args` and `to_home` are still separate surfaces.)

### Scope / non-goals

SAC guarantees exactly this model: per-agent `to_home` + the `_base` shared
baseline, with `ro`-binds for secrets and shared skills. That is the line.

Bolder convenience patterns are *possible* through the raw escape hatches
(`spec.apptainer.binds` / `raw_args`) but are explicitly **out of scope** — SAC
neither designs for nor supports them, and they are the operator's own risk:

- pooling a single **shared `to_home`** across many agents, or
- binding the **host `$HOME` directly** into the container.

Keeping these out of scope is deliberate — it avoids over-engineering and keeps
the reproducibility guarantee crisp.

### The `ro`-bind reproducibility trade-off

A `ro`-bind trades strict reproducibility for currency/sharing — worth being
honest about:

- **Secrets** (`.ssh`, `.config/gh`, `.credentials.json`): correctly *outside*
  the reproducible artifact. You reproduce the *structure*; secret values are
  injected at run time, never committed. This is standard and not a gap.
- **Skills**: a real trade-off. The live `ro`-bind is always-current and shared
  but **not pinned** — if the host's `~/.claude/skills` changes, the agent
  changes, so the run is not byte-reproducible. For *strict* reproducibility you
  would "chain everything": pin the skills to a version and materialize that
  pinned snapshot into `to_home` (bringing it into the definition chain).

So there are two postures: **default = `ro`-bind** (current, shared) and
**strict-reproducible = pin skills into `to_home`**. Pick per agent; the default
is currency.

## The `to_home` 1:1 mirror (general, not just `.claude`)

`<spec_dir>/to_home/` mirrors the container `$HOME` **1:1**. Every path under
`to_home/` lands at the same relative path under the container `$HOME`:

```
agents/<name>/to_home/
  .bashrc            → $HOME/.bashrc
  .env               → $HOME/.env            (chmod 0600)
  .mcp.json          → $HOME/.mcp.json
  CLAUDE.md          → $HOME/CLAUDE.md        (marker-protected)
  .claude/
    CLAUDE.md        → $HOME/.claude/CLAUDE.md (marker-protected)
    settings.local.json
    hooks/
    skills/          (usually a separate read-only bind, see below)
```

A shared baseline (`<agents_dir>/_base/to_home/`, or `$SAC_TO_HOME_BASELINE`)
is applied first; the per-agent `to_home/` overlays on top (per-agent wins).
See `runtimes/_to_home.py` and ADR-0006 for the per-entry semantics. **This
is general** — `to_home` is not a `.claude` delivery mechanism, it is a `$HOME`
delivery mechanism.

## Two delivery paths into the container `$HOME`

Where the materialized tree must physically land depends on how the container
gets its `$HOME`.

### Hardened mode — workspace-home bind

By default the apptainer runtime binds the host workspace home at the
container `$HOME`:

```
--bind <runtime/<name>/home/>:/home/agent
```

`deploy_to_home(config, <workspace_home>)` materializes the tree into
`runtime/<name>/home/`, and the bind makes it visible at `/home/agent`. Done.

### Relaxed mode — overlay upper home

Relaxed specs (`apptainer.relaxed: true`) opt out of the hardened auto-flags
and declare their own `raw_args`, typically:

```yaml
raw_args:
  - --containall
  - --home
  - /home/agent
  - --overlay
  - .../containers/overlays/<name>/      # a DIRECTORY overlay
```

Under this combo the operator-declared `--home /home/agent` is satisfied by
the **overlay's upper layer**, not by the earlier workspace-home bind — so the
workspace-home delivery is shadowed and the `to_home` tree never reaches the
container `$HOME`.

Fix (`runtimes/_to_home_overlay.py`): before launch, `deploy_to_home_overlay`
materializes the **same** tree into the overlay's upper home —

```
<overlay>/upper/<container_home>/      e.g. <overlay>/upper/home/agent/
```

— so the whole tree is part of the container filesystem. The container `$HOME`
is resolved from the spec's `--home` (defaulting to `/home/agent`), **never**
from the host operator's environment. This applies only to **directory**
overlays; `.img` loopback overlays are a no-op (they can't host an upper layer
writable from the host), and such specs don't use the `--home`-override
pattern anyway.

### Why the skills bind is not shadowed

Specs commonly bind skills read-only on top of `$HOME`:

```yaml
binds:
  - /home/ywatanabe/.claude/skills:/home/agent/.claude/skills:ro
```

Writing `.claude/` (settings, hooks) into the overlay upper home is safe:
apptainer bind mounts always win at their **exact** mount point, so the
`/home/agent/.claude/skills` bind layers cleanly on top of the overlay's
`.claude/` without being shadowed. This is precisely why overlay-write (not
per-entry binds) is the chosen mechanism — a single `.claude` bind would
shadow the skills sub-mount.

## Loading model — credentials, MCP, hooks

The SDK runner is started with `setting_sources=[]` (see
`runtimes/_sdk_common.py::build_sdk_options`). This is **intentional and must
not change**: the default would load the host's `~/.claude` state files
(`state.json`, `projects/`, `settings.json`) and treat "no state" as
not-logged-in even when credentials are mounted. Empty `setting_sources` means
the explicitly-passed setup is the entire context — no host auto-discovery.

Each surface is therefore loaded explicitly:

| Surface       | Mechanism                                                              |
|---------------|------------------------------------------------------------------------|
| Credentials   | The auth layer (`provision_anthropic_auth`): `~/.claude/.credentials.json` is bind-mounted at `/tmp/sac-claude/`, `CLAUDE_CONFIG_DIR` points the SDK there; or `SAC_ANTHROPIC_API_KEY` bridged into `ANTHROPIC_API_KEY`. A bare host `ANTHROPIC_API_KEY` is never honoured. |
| MCP servers   | `--mcp-config` — parsed from the workspace `.mcp.json` (materialized by sac from `spec.mcp_servers`) into `ClaudeAgentOptions.mcp_servers`. The `sac` channel sidecar is auto-registered for `channels: [server:sac]`. |
| Hooks/settings| `--settings <path>` — `ClaudeAgentOptions.settings` is set to the in-container `$HOME/.claude/settings.local.json`. This is the SDK's "flag settings" layer, the highest-priority user-controlled layer, loaded **independently** of `setting_sources`. Without it, `setting_sources=[]` would never load the delivered settings. |

### `--settings` and hook paths

`build_sdk_options` resolves the settings path from the in-container `$HOME`
(`$HOME/.claude/settings.local.json`) and sets `ClaudeAgentOptions.settings`
only when the file is present (so a spec without one doesn't aim `--settings`
at a missing file). The hook `command`s inside `settings.local.json` use
`$HOME/.claude/hooks/...`, so they resolve in-container regardless of what
`$HOME` is — both the workspace-home bind and the overlay upper home put the
hook scripts at exactly that path.

## Summary

1. `to_home/` = a general 1:1 `$HOME` mirror.
2. Hardened mode: delivered via the workspace-home bind. Relaxed
   `--home`/`--overlay` mode: delivered into `<overlay>/upper/<home>/` so the
   tree is part of the container filesystem and the skills bind layers on top.
3. `setting_sources=[]` (machine-independence) + explicit `--settings`,
   `--mcp-config`, and the auth-layer credentials bind. No host `~/.claude`
   auto-discovery, ever.
