<!-- GENERATED. source-sha256: 15c401d5fddf1580bde7e636a5d52f6595e13c6e6aeb1c536a2fdc15e453f163 -->
# Worktree policy (HERMES)

This file is a discovery projection, not policy authority. The authoritative
manifest is the `worktree-policy.json` bundled with SAC
(`_baseline_assets/worktree_policy/`); runtime decisions come only from SAC's
in-process policy engine.

- The authority checkout stays on `develop` and is read-only.
- All edits, commits, pushes, and branch changes happen in linked worktrees.
- Linked worktrees use topic prefixes: `feature/`, `fix/`, `chore/`, `docs/`, `refactor/`, `test/`.
- Worktrees live below `.worktrees/`.
- There is no bypass, exemption, or permissive fallback.

Examples generated from the tested manifest fixtures:

- `assert-context --repo /path/to/repo --intent edit` → deny
- `assert-context --repo /path/to/repo/.worktrees/fix-x --intent edit` → allow
- `inspect --repo .` → resolved JSON policy record
