# skills-archive — verbatim snapshot of the sac agent skills

This directory is a **plain-text, uncompressed, verbatim copy** of the
live sac agent skills as they existed immediately before the
"skills de-bloat pilot" (see the PR that introduced this directory).

- Source of truth (live, propagated to fleet agents):
  `src/scitex_agent_container/_skills/scitex-agent-container/`
- This archive: `skills-archive/scitex-agent-container/` — the **old**
  versions, kept so nothing is lost and so the operator can read the
  pre-rewrite text directly.

The live skills remain in place and keep working. The de-bloat pilot
rewrites only a small number of the most API-restatement-heavy skill
files LEAN; every other live file is untouched in this first pass.

If you want to see what a rewritten skill said before, read its twin
here. This directory is not loaded by any agent and is not part of the
skills propagation mechanism — it is a reference snapshot only.
