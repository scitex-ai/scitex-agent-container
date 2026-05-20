---
name: example-skill
description: |
  [WHAT] Project-specific context and reusable knowledge for this agent — coding conventions, tool usage patterns, domain vocabulary.
  [WHEN] Always loaded at agent startup; provides background the agent needs to work effectively in this codebase.
  [HOW] Place one `.md` file per topic in this directory; each file is loaded into the agent's context via the skills system.
tags: [example]
primary_interface: skills
interfaces:
  python: 0
  cli: 0
  mcp: 0
  skills: 3
  http: 0
---

# example-skill

Project-specific knowledge for the agent.

## Sub-skills

- [example-skill.md](example-skill.md) — Starter skill file; replace with real project context
