#!/usr/bin/env bash
# Lesson 11 — The dot_claude/ sibling directory.
#
# What problem does this solve?
#   Claude Code (the CLI sac runs inside the container) reads its
#   personality and tool surface from a `.claude/` directory:
#     - CLAUDE.md      project-scoped system prompt
#     - .mcp.json      MCP server definitions (what tools Claude has)
#     - .env           environment variables Claude can read
#     - commands/      slash commands
#     - skills/        loadable skills (the things you `Skill`-invoke)
#     - hooks/         pre/post-tool-use shell hooks
#
#   Inside a container that workspace must be SHIPPED, not hand-crafted
#   on every start. spec.dot_claude points to a host-side template that
#   sac copies/merges into the agent's workspace before claude boots.
#
# Failure mode if you skip this:
#   - You start the agent, then realise it has no MCP servers. Each
#     boot starts naked. Solution: ship them in dot_claude/.
#   - You bake secrets into CLAUDE.md instead of .env, then commit the
#     spec to git. .env in dot_claude is the right place.
#
# Layout convention (dir-as-SSoT for dot_claude too):
#
#   ~/.scitex/agent-container/agents/my-agent/
#     spec.yaml
#     dot_claude/                    ← sibling, auto-discovered
#       CLAUDE.md
#       .mcp.json
#       .env                         ← gitignored; secrets live here
#       commands/
#       skills/
#       hooks/
#
# spec.yaml field:
#   spec:
#     dot_claude: ./dot_claude       # default: ./dot_claude next to spec.yaml
#   # → omit the field and sac auto-discovers ./dot_claude/ if it exists
#   # → set explicitly to point to a shared template:
#     dot_claude: ~/agent-templates/researcher/dot_claude
#
# What lands where at start:
#   Host: <agent_dir>/dot_claude/                    (template, read-only intent)
#   Container: <workdir>/.claude/                    (Claude reads this)
#
# Merge rules (see docs/how-sac-works.md):
#   - Files copied verbatim (CLAUDE.md, .mcp.json, .env, *.json).
#   - Subdirectories (commands/, skills/, hooks/) merged file-by-file.
#   - Existing files in <workdir>/.claude/ from a previous run are
#     preserved unless overridden by the template — so user state
#     (e.g. session history) survives a restart.
#
# Pure-apptainer equivalent:
#   apptainer instance start \
#     --bind ./dot_claude:/work/.claude:ro \
#     my.sif my-agent
#   # but you'd have to manage .env separately, handle the writable
#   # subset (commands/, skills/) by hand, and copy files at start.
#   # sac's merge logic exists to centralise that.
set -euo pipefail
APPLY="${1:-}"

DEMO_NAME="${SAC_DEMO_AGENT:-tutorial-demo}"
AGENT_HOME="$HOME/.scitex/agent-container/agents/$DEMO_NAME"
DOT_CLAUDE="$AGENT_HOME/dot_claude"

echo "── Current dot_claude (if any) ──"
if [[ -d "$DOT_CLAUDE" ]]; then
    # shellcheck disable=SC2012
    ls -la "$DOT_CLAUDE/" | head -20
else
    echo "(no dot_claude at $DOT_CLAUDE)"
fi

echo
echo "── Reference example (bundled) ──"
echo '$ ls examples/agents/full-agent/dot_claude/'
echo '  # → CLAUDE.md  .env.example  .mcp.json  commands/  hooks/  skills/'

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
    echo "── Materialising $DOT_CLAUDE (real) ──"
    mkdir -p "$DOT_CLAUDE/commands" "$DOT_CLAUDE/skills" "$DOT_CLAUDE/hooks"
    cat >"$DOT_CLAUDE/CLAUDE.md" <<'MD'
# Agent Role
You are tutorial-demo, a sandbox agent.
- Reply concisely.
- Do not call shell tools unless explicitly asked.
MD
    cat >"$DOT_CLAUDE/.mcp.json" <<'JSON'
{"mcpServers": {}}
JSON
    cat >"$DOT_CLAUDE/.env.example" <<'ENV'
# Copy to .env and fill in. .env is gitignored by convention.
# ANTHROPIC_API_KEY=
ENV
    echo "  wrote: $DOT_CLAUDE/{CLAUDE.md,.mcp.json,.env.example}"
fi

# EOF
