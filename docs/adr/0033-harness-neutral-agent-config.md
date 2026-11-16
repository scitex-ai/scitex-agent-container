# ADR-0033: Harness-neutral agent config (`.agents/`) rendered per harness

- Status: accepted (Claude Code adapter implemented; other adapters planned)
- Date: 2026-11-16
- Related: ADR-0009, ADR-0018 (layered `to_home`), ADR-0027 (Hermes owns the control plane), ADR-0028 (harness-neutral channels)

## Context

Agent configuration (instructions, skills, hooks, subagents, commands, MCP
servers, settings) was Claude-shaped everywhere: `to_home/.claude/` in sac,
`src/.claude/to_claude/` plus `.claude-settings.json` in dotfiles. Each other
harness translated pieces on its own (Hermes: MCP and skills; Codex: MCP and a
copy of Claude's hooks block). Hosts drifted: on 2026-11-16 three compute
hosts had a `settings.json` naming 46 hook scripts that did not exist there,
because only `settings.json` and skills were ever deployed.

Hermes is sac's primary harness; Claude Code, Codex, Gemini CLI and OpenCode
are peers. The configuration should not belong to any one of them.

## Decision

### One neutral source, two layers

```
~/.scitex/agent-container/.agents/                  user layer (dotfiles, every host)
<git-root>/.scitex/agent-container/.agents/         project layer — wins
```

Each layer holds config only; secrets are referenced as `${VAR}`, never stored.
Note the neighbour: `~/.scitex/agent-container/agents/` holds sac agent specs;
`.agents/` holds the neutral config those agents (and humans) run with.

| Path | Format | Standard? |
|---|---|---|
| `AGENTS.md` | Markdown instructions — the only instruction file | yes (AGENTS.md) |
| `*.md` | extra docs `AGENTS.md` refers to (e.g. `RTK.md`) | — |
| `skills/<name>/SKILL.md` | Agent Skills open standard | yes |
| `subagents/<name>.md` | frontmatter `name`, `description`, `model` + prompt body | common subset |
| `commands/<name>.md` | frontmatter `description`, `argument-hint` + body; `$ARGUMENTS` | common subset |
| `hooks/` + `hooks.yaml` | scripts + registrations (below) | ours |
| `mcp.yaml` | `servers: {name: {transport, command, args, env, url, headers}}` | ours |
| `settings.yaml` | `model`, `env`, `permissions`, `external_skills`, `native: {<harness>: {...}}` | ours |

`hooks.yaml`:

```yaml
disable: [name]            # project layer only: drop a user hook by name
hooks:
  - name: deny_broad_env_dump
    event: pre_tool        # pre_tool | post_tool | stop | session_start | prompt_submit | notification
    matcher: Bash          # tool-name pattern (Claude vocabulary); empty = all
    command: hooks/pre-tool-use/deny_broad_env_dump.sh   # layer-relative, or verbatim
    timeout: 10            # seconds
  - name: worktree_create
    event: claude:WorktreeCreate   # harness-only event; other adapters skip + report
    command: hooks/worktree-create.sh
```

Hook contract (shared by Claude Code, Codex, Gemini CLI and Hermes): JSON on
stdin (`hook_event_name`, `tool_name`, `tool_input`, `session_id`, `cwd`);
exit 2 or `{"decision": "block", "reason": ...}` blocks.

### Precedence (project over user)

Named entries (skills, subagents, commands, docs, MCP servers): same name, the
project wins. Hooks: union, user first; a project removes a user hook only by
naming it in `disable`. Settings: deep merge, the project wins, except
permission lists, which are unioned — a project cannot drop a user deny.

### Adapters render, they do not own

An adapter turns a layer into one harness's native files. A user-scope deploy
renders the user layer into the harness's user dir; a project-scope deploy
renders the project layer into the project's harness dir (the harness itself
then layers the two, as it already does for its native files).

Claude Code (implemented, `sac config deploy --harness claude`):

| Neutral | `~/.claude` (user) / `<root>/.claude` (project) |
|---|---|
| `AGENTS.md` | `CLAUDE.md` → link to `AGENTS.md` |
| `*.md` docs | linked by name |
| `hooks/` | `hooks` → link |
| `subagents/` | `agents` → link |
| `commands/` | `commands` → link |
| `skills/<name>` | `skills/<name>` → link each (the dir stays real; foreign entries kept; names in `external_skills` — owned by another installer, e.g. scitex-dev's `scitex` — are skipped) |
| `hooks.yaml`, `settings.yaml` | `settings.json` written (`hooks/x.sh` → `$HOME/.claude/hooks/x.sh` or `$CLAUDE_PROJECT_DIR/.claude/hooks/x.sh`) |
| `mcp.yaml` | project: `<root>/.mcp.json`; user: not rendered (lives in `~/.claude.json` with live state) |

Measured on Claude Code 2.1.318 (2026-11-16): `AGENTS.md` alone is not loaded
at user or project level, not even with `instructionFiles:
claude-md-and-agents-md`, while `CLAUDE.md` is. Hence the `CLAUDE.md` link:
there is still exactly one instruction file to edit.

Planned adapters: Codex (`~/.codex/`: `AGENTS.md`, `hooks.json`,
`config.toml [mcp_servers]`, `agents/*.toml`; commands as skills), Gemini CLI
(`.gemini/`: `settings.json` hooks with ms timeouts, `commands/*.toml`),
Hermes (`config.yaml`: `hooks:`, `mcp_servers:`, `skills.external_dirs`),
OpenCode (`.opencode/`; hooks need a generated plugin). They plug into the
registry's existing per-harness `prepare_home` seam.

### Verbs

```
sac config show    [--scope user|project]
sac config deploy  --harness claude [--scope user|project] [--dry-run]
sac config check   --harness claude [--scope user|project]     # exit 1 on drift
sac config import  --from claude --to <dir> [--copy-trees]     # migration
```

Deploy never deletes: a real file or directory in the way is moved to
`<target>/.sac-backup/<stamp>/`; a manifest (`.sac-agents-config.json`) records
what deploy manages so entries removed from the source are unlinked next time,
and unmanaged entries are never touched. Live state (`.credentials.json`,
`projects/`, `history.jsonl`, `.claude.json`) is outside every plan.

## Consequences

- dotfiles stops hand-linking `~/.claude`; `deploy.sh` calls
  `sac config deploy --harness claude --scope user`, and the content moves from
  `src/.claude/to_claude/` + `.claude-settings.json` to
  `src/.scitex/agent-container/.agents/` (via `sac config import`).
- Permissions do not translate between harness grammars; anything beyond the
  neutral subset stays in `settings.yaml: native.<harness>`.
- Container `to_home/.claude/` can later be rendered from the same layers,
  closing the host/container drift.
