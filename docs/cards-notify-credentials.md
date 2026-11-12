# Cards notification credentials

Cards reads and writes use the pooled database endpoint. PostgreSQL LISTEN
needs a session endpoint, declared separately in `SCITEX_CARDS_NOTIFY_DSN`.
`agents provision-cards-notify` derives its password from the selected exact
role's pooled row in the same private passfile. It does not contact PostgreSQL,
verify authentication, start a listener or deliver a message.

```bash
sac agents provision-cards-notify --agent example-agent --dry-run --json
sac agents provision-cards-notify --agent example-agent --check --json
sac agents provision-cards-notify --agent example-agent --yes --json
```

Replace `example-agent` with an exact declared agent name. `--root` selects a
spec directory; `--pgpass` overrides the spec-declared passfile. Without an
agent filter the command inspects all specs under that root. Both local
`--json` and root `sac --json agents provision-cards-notify ...` select JSON.

Modes differ:

- `--dry-run` emits an unverified argument plan before reading specs or secrets.
  It does not establish valid names, paths, DSNs, passwords or readiness.
- Default inspection reads specs and passfiles, reports `planned` or `ready`,
  and writes nothing. `--check` exits 1 if any row is still planned.
- `--yes`/`-y` aliases existing `--apply`. Approval conflicts with `--check` and
  `--dry-run`. Apply validates the complete request set before writing, then
  atomically replaces each affected passfile with mode 0600. Repeated apply
  reports `ready` when the requested rows are already present.

The owning implementation is
[`_agents_provision_cards_notify.py`](../src/scitex_agent_container/cli_pkg/_agents_provision_cards_notify.py).
Its tests exercise actual private files, ambiguous/missing roles, conflicting
rows, symlink refusal, declared endpoint constraints, no-write checks and CLI
modes. Validation before writing and per-file atomic replacement do not make
several passfiles a single transaction: a later filesystem error can follow
an earlier successful replacement. No password is included in output; paths,
agent names, roles and endpoint metadata are observable.
