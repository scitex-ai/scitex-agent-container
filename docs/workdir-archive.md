# Claude workdir archiving

`archive-claude-bloat` moves audit-flagged buckets from a registered agent's
workdir. It covers the existing audit's curated bloat sources and can move
worktrees. `prune-claude` is a separate, narrower operation with pending-record
and merged-worktree checks. Do not treat those operations as interchangeable.

```bash
sac agents archive-claude-bloat worker --dry-run
sac agents archive-claude-bloat worker --yes --json
```

Preview reports arguments only. It does not look up the registry, resolve
workdir, scan files, list candidates or create an archive. A plan is not proof
that the named agent exists or that anything should move. Approval (`--yes`
or `-y`) is required for the existing audit/move path; approval and preview
cannot be combined. Unknown or unusable registrations retain exit status 2.

On apply, the existing resolver reads the registry/spec workdir. The audit
selects buckets by its configured thresholds. Successful move records contain
source, destination, file count and byte count. Local/root `--json` selects
those records; the default output retains one readable line per move.

The mover uses `shutil.move`, including its cross-device copy/remove behavior,
and stores results under `.claude/.archived-<UTC>/<relative path>`. It can skip
sources that disappear or fail to move. Only successful records are returned;
zero records does not establish a clean tree or complete processing. This is
not a transaction, checksum-verified backup, or safe substitute for reviewing
unmerged worktrees. Existing timestamp/destination and partial-move behavior
remain unchanged. Inspect retained data before attempting a manual restoration.

Implementation: `cli_pkg/agents_prune_claude.py`; audit ownership:
`_workdir/_audit.py`. Matching CLI and audit tests use owned temporary trees.
These controls do not qualify live agent recovery or an operator's workdir.
