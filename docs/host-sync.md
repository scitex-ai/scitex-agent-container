# Host code synchronization

`sac host sync PEER --dry-run --json` prints an argument-only plan. It does
not load host config, contact a peer, refresh Git refs, inspect CI or record
alarms. A plan is not evidence that its target or checkout is valid.
`--all` replaces the positional peer; it does not resolve the peer list until
an actual check or approved sync runs.

`sac host sync PEER --check --json` contacts the peer and runs `git fetch` to
refresh upstream refs. It preserves HEAD and tracked working-tree content but
can update refs and Git metadata. It is not a wholly read-only operation.
Drift exits 1; an undetermined result exits 2. `--exit-zero` explicitly changes
the process status while retaining the actual verdict in JSON `exit_code`.

`sac host sync PEER --yes` (`-y`) approves the existing reconciliation path.
Dirty, ahead or diverged checkouts, busy/unknown CI and undetermined probes
still refuse reconciliation. Application remains fast-forward only and is
followed by module-path, HEAD and symbol verification. Approval cannot be
combined with `--dry-run` or `--check`; an unapproved sync refuses before
config lookup or SSH.

`--check --all --alarm --exit-zero` remains the existing scheduled detector.
It fetches peer refs and writes local event records, without merging peers.
`--dry-run --check --alarm` only describes those arguments and writes no events.
`--alarm` without `--check` is refused. No schedule, timeout or event semantics
are changed by the new approval requirement.

Implementation: `cli_pkg/_host_sync.py` dispatches owning `_hostsync` providers.
The `_hostsync/_probe.py` probe refreshes refs; `_hostsync/_apply.py` owns the
fast-forward. Existing CLI, exit-status, alarm, probe, policy and apply tests
remain the behavioral controls. Source checks and isolated fixtures do not
qualify live SSH, CI availability, service installation or fleet deployment.
