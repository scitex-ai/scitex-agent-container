# ADR-0018 — Layered to_home config cascade: deep-merge with raise-on-conflict (2026-06-24)

## Status

Accepted (operator decisions, 2026-06-24). Implementation in progress on
`develop`:

* `runtimes/_layer_merge.py` — `deep_merge_layers()` deterministic
  cascade deep-merge + provenance (DONE, unit-tested).
* `runtimes/_to_home_errors.py` — `LayerMergeConflict` (DONE).
* `runtimes/_to_home.py` — deploy `settings.json` / `.mcp.json` through
  the cascade (TODO).
* baseline rename `_shared/to_home/.claude/settings.local.json` →
  `settings.json` (TODO — must land WITH the deploy change).
* `cli_pkg/_explain.py` — per-key provenance in `sac agents explain`
  (TODO).

Tracked: scitex-todo `sac-config-cascade-merge`.

## Context

An agent's effective `$HOME/.claude/` config is assembled from several
`to_home` sources that can each ship the same file (`settings.json`,
`.mcp.json`, `.envrc`). The sources form a precedence stack, lowest
first:

```
1. user    ~/.scitex/agent-container/agents/_shared/to_home   (fleet-wide)
2. project <proj>/.scitex/agent-container/agents/_shared/to_home (per-repo)
3. per-agent <spec_dir>/to_home                                 (this agent)
        ↓ materialize at start
   container $HOME/.claude/...   ← DERIVED, re-built every start
```

The container `$HOME` is **output**, not a source — re-materialized on
every start, so it self-heals. The real drift risk is *between the host
source layers* (and across hosts, which stays git's job — the existing
`sac-drift` unpushed-commits warning).

The three mergeable files did **not** compose the same way:

* `.envrc` — already cascades all layers and folds into `.env`.
* `.mcp.json` — deep-merged (two-pass), raises `McpMergeConflict` on a
  same-name server defined two ways.
* `settings.json` — **plain full overwrite** in `deploy_to_home`
  (`_deploy_plain_file`). The baseline was deliberately named
  `settings.local.json` precisely to dodge that overwrite, then
  `settings_json.setup_settings_json` folded `.local` → `.json` with a
  per-event hooks deep-merge. Indirect and surprising.

Two consequences the operator flagged:

1. The baseline lands at `$HOME/.claude/` = **USER scope**, where only
   `settings.json` is discovered; `settings.local.json` is project-scope
   only. So the baseline source must be `settings.json`.
2. Renaming it to `settings.json` without teaching `deploy_to_home` to
   deep-merge would turn it into a silent full overwrite — a regression.
   Rename and deep-merge must land together.

## Decision

One deterministic merge model for every mergeable `to_home` config file
(`settings.json`, `.mcp.json`; `.envrc` keeps its env-fold but the same
cascade order):

* **Cascade order**: user `_shared` → project `_shared` → per-agent
  (lowest → highest precedence), de-duping identical resolved paths.
* **Deep-merge**: `dict`+`dict` recurse; the `hooks` block merges
  per-event (concatenate + dedupe identical matcher-groups); `list`+`list`
  append uniques (order-preserving); an identical scalar from two layers
  is idempotent.
* **Raise on conflict (SSOT)**: two layers assigning the same key two
  **different** scalar values raises `LayerMergeConflict` at deploy. No
  silent winner — each key is owned by exactly ONE layer. The message
  names the key path, both layers, both values, and the fix.
* **Provenance**: the merge returns a dotted-key-path → owning-layer map
  so `sac agents explain` shows WHERE each effective setting came from
  (drift made visible before launch).
* **Baseline rename**: `_shared/to_home/.claude/settings.local.json` →
  `settings.json` (user scope). The legacy-name fold in
  `setup_settings_json` stays as a back-compat shim for un-renamed hosts.

### Why raise instead of "higher layer wins"

The operator chose fail-loud over silent precedence (2026-06-24). Silent
override is exactly how drift hides: a project `_shared` quietly shadows a
fleet default and nobody notices until behaviour diverges across agents.
Forcing single-layer ownership per key keeps the cascade auditable and
makes every override an explicit, visible act. Matches the existing
`.mcp.json` `McpMergeConflict` contract.

## Consequences

* Adding the same scalar key to two layers is now a hard deploy error —
  intended; the fix (set it in one layer) is in the message.
* `settings.json` baseline composes with host/auth-stage settings instead
  of clobbering them.
* `sac agents explain` gains provenance, so the operator can see the
  effective config + its sources without launching.
* Sibling decision (separate concern): canonical agent identity becomes
  `<resolved-hostname>@<agent-label>` — see scitex-todo
  `sac-identity-host-at-name` (its own ADR when implemented).
