#!/usr/bin/env bash
# Lesson 11 — The to_home/ sibling directory.
#
# What problem does this solve?
#   Claude Code (the CLI sac runs inside the container) reads its
#   personality and tool surface from the agent's $HOME:
#     - CLAUDE.md          system prompt        ($HOME/CLAUDE.md)
#     - .mcp.json          MCP server definitions (what tools Claude has)
#     - .env               environment variables Claude can read
#     - .claude/commands/  slash commands
#     - .claude/skills/    loadable skills (the things you `Skill`-invoke)
#     - .claude/hooks/     pre/post-tool-use shell hooks
#
#   Inside a container that $HOME must be SHIPPED, not hand-crafted on
#   every start. spec.to_home points to a host-side template whose
#   contents sac mirrors into the agent's $HOME before claude boots.
#   Every path under to_home/ lands at the same relative path in $HOME.
#
# Failure mode if you skip this:
#   - You start the agent, then realise it has no MCP servers. Each
#     boot starts naked. Solution: ship them in to_home/.mcp.json.
#   - You bake secrets into CLAUDE.md instead of .env, then commit the
#     spec to git. to_home/.env is the right place (chmod 0600 at deploy).
#
# Layout convention (dir-as-SSoT for to_home too):
#
#   ~/.scitex/agent-container/agents/my-agent/
#     spec.yaml
#     to_home/                       ← sibling, auto-discovered; mirrors $HOME
#       CLAUDE.md
#       .mcp.json
#       .env                         ← gitignored; secrets live here
#       .claude/
#         commands/
#         skills/
#         hooks/
#
# spec.yaml field:
#   spec:
#     to_home: ./to_home             # default: ./to_home next to spec.yaml
#   # → omit the field and sac auto-discovers ./to_home/ if it exists
#   # → set explicitly to point to a shared template:
#     to_home: ~/agent-templates/researcher/to_home
#
# What lands where at start:
#   Host: <agent_dir>/to_home/                       (template, read-only intent)
#   Container: $HOME/  (= runtime/<name>/home/)      (Claude reads this)
#
# Merge rules (see docs/how-sac-works.md):
#   - CLAUDE.md / state.md: marker-protected merge (user tail preserved).
#   - .env: full overwrite, chmod 0600.
#   - Everything else: full overwrite at the mirrored path.
#   - A shared baseline to_home/ (<agents_dir>/_base/to_home) is applied
#     first; the per-agent to_home/ overlays on top (per-agent wins).
#
# Pure-apptainer equivalent:
#   apptainer instance start \
#     --bind ./to_home:/home/agent:ro \
#     my.sif my-agent
#   # but you'd have to manage .env perms separately, handle the writable
#   # subset by hand, and copy files at start. sac's merge logic exists
#   # to centralise that.
set -euo pipefail
APPLY="${1:-}"

DEMO_NAME="${SAC_DEMO_AGENT:-tutorial-demo}"
AGENT_HOME="$HOME/.scitex/agent-container/agents/$DEMO_NAME"
TO_HOME="$AGENT_HOME/to_home"

echo "── Current to_home (if any) ──"
if [[ -d "$TO_HOME" ]]; then
    # shellcheck disable=SC2012
    ls -la "$TO_HOME/" | head -20
else
    echo "(no to_home at $TO_HOME)"
fi

echo
echo "── Reference example (bundled) ──"
echo '$ ls examples/agents/full-agent/to_home/'
echo '  # → CLAUDE.md  .env.example  .mcp.json  .claude/{commands,hooks,skills}/'

echo
echo "── Minimal CLAUDE.md to ship ──"
cat <<'MD'
# Agent Role
You are tutorial-demo, a sandbox agent.
- Reply concisely.
- Do not call shell tools unless explicitly asked.
MD

echo
echo "── Minimal .mcp.json (no servers; safe default) ──"
echo '{"mcpServers": {}}'

if [[ "$APPLY" == "--apply" ]]; then
    echo
    echo "── Materialising $TO_HOME (real) ──"
    mkdir -p "$TO_HOME/.claude/commands" "$TO_HOME/.claude/skills" "$TO_HOME/.claude/hooks"
    cat >"$TO_HOME/CLAUDE.md" <<'MD'
# Agent Role
You are tutorial-demo, a sandbox agent.
- Reply concisely.
- Do not call shell tools unless explicitly asked.
MD
    cat >"$TO_HOME/.mcp.json" <<'JSON'
{"mcpServers": {}}
JSON
    cat >"$TO_HOME/.env.example" <<'ENV'
# Copy to .env and fill in. .env is gitignored by convention.
# ANTHROPIC_API_KEY=
ENV
    echo "  wrote: $TO_HOME/{CLAUDE.md,.mcp.json,.env.example}"
fi

# EOF
