<!-- GENERATED. source-sha256: e659eb9aee2c4e337e67cd33b19a8804318b3cbdf74836a4bcd72a49d14a36b8 -->
# Worktree policy (HERMES)

This file is a discovery projection, not policy authority. The authoritative
manifest is selected by `sac worktree policy` from the SAC configuration root
or the installed package defaults; runtime decisions come only from that CLI.

- The authority checkout stays on `develop` and is read-only.
- All edits, commits, pushes, and branch changes happen in linked worktrees.
- Linked worktrees use topic prefixes: `feature/`, `fix/`, `chore/`, `docs/`, `refactor/`, `test/`.
- Worktrees live below `.worktrees/`.
- There is no bypass, exemption, or permissive fallback.

Examples generated from the tested manifest fixtures:

- `sac worktree policy assert-context --repo /path/to/repo --intent edit` → deny
- `sac worktree policy assert-context --repo /path/to/repo/.worktrees/fix-x --intent edit` → allow
- `sac worktree policy inspect --repo .` → resolved JSON policy record
