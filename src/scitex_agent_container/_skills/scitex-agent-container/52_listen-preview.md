---
description: |
  [TOPIC] Listener lifecycle argument previews
  [DETAILS] Preview start, stop and restart arguments before token, pidfile,
  socket, signal or supervisor work. Plans report unverified lifecycle state;
  explicit start/stop/restart require approval.
tags: [scitex-agent-container-listen-preview]
---

# Preview listener lifecycle arguments

`listener` is the public namespace. The reviewed source still registers the
hidden `listen` alias, warning on stderr once per shell session. Its deprecation
metadata specifies removal in **0.40.0**, earlier than the declared source
version **0.42.1**: removal and caller migration remain unresolved. Observed
alias retention does not extend that deadline or promise future compatibility.
Group options, subcommands and approval checks are currently shared. Existing
launcher argv still works in this source; this guide does not alter service
units, token paths or bare startup.
The JSON preview target remains `"listen"` for compatibility.

```bash
sac listener start --dry-run
sac listener start --bind '[::1]:7979' --print-token --dry-run
sac listener --bind 127.0.0.1:7979 stop --grace-secs 3.5 --dry-run
sac listener restart --dry-run
```

Each preview emits JSON with `dry_run=true`, `scope="arguments_only"`, the
action, parsed bind and effective options. Start options on the verb take
precedence over group options, following ordinary startup's resolution.
Stop and restart take their bind from the group and report their grace period.
`stop --json --dry-run` returns this argument plan rather than a stop result.

A null token-file option leaves its default unresolved. Combining
`--print-token` with `--dry-run` records the option without reading or printing
a token. Preview does not create tokens/locks, inspect a daemon, bind/connect a
socket, signal a process, register a peer, or choose a supervisor/failover path.
Shared CLI startup can still read global configuration and the version cache
and initialize logging, as with `sac --version`.

The plan preserves start's non-loopback opt-in requirement and rejects malformed
binds. Bind ports must be in 0–65535, using the same private port policy as MCP
HTTP startup. Grace periods must be finite for portable JSON. Its `unverified` list
covers credentials, live daemon/pidfile/socket state, supervisor/failover state
and the lifecycle outcome. Exit zero means an argument plan was produced;
it does not establish that a daemon can start, stop or restart.

Explicit `listener start`, `listener stop` and `listener restart` require `--yes/-y` before runtime lookup or signals;
`SAC_ASSUME_YES=1` does not approve it. Preview takes precedence over approval.
Start approval also covers `--print-token`, which may create a token.
The existing lifecycle backends and result handling remain unchanged.

```bash
sac listener start --yes
sac listener start --yes --print-token
sac listener stop --yes --json
sac listener restart --yes
```

Bare startup remains a compatibility path for the direct respawn fallback in
`_listen/_restart.py`; it reaches the shared backend without the explicit-verb
gate. The source systemd template already uses `listen start --yes`, so it does
not rely on bare startup, but still depends on the hidden alias. This is not
universal startup admission. Removing bare startup or the alias requires caller
rollout. Explicit lifecycle callers must adopt
the flag before upgrading. Token rotation now sends
`restart --yes`; coordinate caller and peer executable versions before rollout.
An older peer lacking the flag refuses it; no unapproved retry is made.
This source change does not migrate deployed callers or peer executables. Preview belongs to the
explicit `start`, `stop` and `restart` verbs.

Source: `cli_pkg/_main.py` (alias/deprecation registration),
`_listen_preview.py`, `_listen_stop_approval.py`,
`_listen_restart_approval.py`, `_listen_start_approval.py`,
`_listen_verbs.py`, `_port_options.py` and `listen_cmds.py`. The source launcher
contracts are `systemd/sac-listen.service.template` and `_listen/_restart.py`.
Behavioral controls: `tests/scitex_agent_container/cli_pkg/test__listen_preview.py`,
`test__listen_verbs.py`, `test__listen_stop_approval.py`,
`test__listen_restart_approval.py`, `test__listen_start_approval.py`,
`test_listen_cmds.py`
and the owning `_listen` stop/restart tests.
`_listen/test__plane_targeting_argv.py` covers disruptive-command detection,
including both CLI spellings with explicit stop approval. Unit collaborators and owned argument checks do not qualify live host
services, real supervision, PostgreSQL peer registration or deployed clients.

Shared port controls: `tests/scitex_agent_container/cli_pkg/test__port_options.py`.
