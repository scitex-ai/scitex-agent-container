# Deployment runbook — merged ≠ deployed

## The lesson, in one line

A merged sac change is **invisible to running agents** until the SIF is
rebuilt **and** the agents are restarted. CI green on `develop` only
means the code is correct — not that any agent is running it.

## Where this bites

Two real incidents on 2026-06-03 illustrate the gap:

| Incident | Code merged | SIF rebuilt? | Agents restarted? | Symptom |
|----------|-------------|--------------|-------------------|---------|
| Account-pinned creds live-bind fix | 2026-06-01 (`d70e608` + `acf2cbb` on develop) | **No** (SIF built 2026-05-28) | n/a | Stale OAuth token after refresh → Claude 401 |
| SAC-from-SAC broker fail-loud (PRs #287/#288/#289) | 2026-06-03 | **No** (pre-existing SIFs) | n/a | Silent-200 disease persisted in running agents |

Both were diagnosed as "the fix is broken" before recon showed the fix
was on `develop` but had never reached the SIF the running agents were
actually using.

## Why a rebuild is required (not just a host install)

The :scitex layer installs sac **into** the SIF
(`containers/apptainer-scitex.def %post` → `pip install
/opt/scitex-agent-container-src`). The runtime entry point inside
every spawned agent is:

```
/opt/venv-sac/bin/python -m scitex_agent_container._runners.claude_session
```

That `_runners.claude_session` and everything it imports —
`_lifecycle/_in_sif_broker`, `_mcp/channel`, `_state/_acl_broker_client`,
the in-SIF HTTP client, the runner session machinery — runs from the
**baked** sac wheel inside the SIF. Host-side `pip install -e .`
updates do not reach it.

The host-side modules in `runtimes/_apptainer_*` (which build the
`apptainer exec` argv: binds, env, isolation flags, creds resolution)
**do** run from the host install, but a running agent was spawned with
the **old argv** — so changes there only take effect on the next
`sac agent spawn`. A running agent stays on its frozen argv until it
exits.

## The deploy checklist

After merging any of the following surfaces to `develop`, the deploy
is **not complete** until the SIF is rebuilt and agents are restarted:

| Surface | What runs from it | Deploy step |
|---------|-------------------|-------------|
| `src/scitex_agent_container/_runners/*` | In-SIF agent runtime (claude_session, hooks, session state) | Rebuild `:scitex`, restart agents |
| `src/scitex_agent_container/_lifecycle/_in_sif_*` | In-SIF broker + HTTP client (SAC-from-SAC) | Rebuild `:scitex`, restart agents |
| `src/scitex_agent_container/_mcp/*` | In-SIF MCP server + channel (push hub client, A2A) | Rebuild `:scitex`, restart agents |
| `src/scitex_agent_container/runtimes/_apptainer_*` | Host argv builder (binds, isolation, creds, fakeroot) | Restart agents (new spawn picks up host install) |
| `src/scitex_agent_container/_listen/*` | Host sac-listen daemon (push hub, spawn broker) | `sac listen restart` or `systemctl --user restart sac-listen.service` |
| `src/scitex_agent_container/_state/_acl_broker_client.py` and other in-SIF state clients | In-SIF state access | Rebuild `:scitex`, restart agents |
| `containers/apptainer-*.def` | The SIF recipes themselves | Rebuild `:base` **and** `:scitex` |
| Anything in `_creds`, `_account` invoked from inside the SIF | In-SIF credential handling | Rebuild `:scitex`, restart agents |

When in doubt: **rebuild both SIFs and restart the agents.** A rebuild
is ~25-45 min wall-clock and is the only safe assumption when the
boundary is unclear.

## Canonical rebuild sequence

```bash
# 1. Make sure the host install is current (sac wheel that gets baked).
git -C ~/proj/scitex-agent-container fetch origin develop
git -C ~/proj/scitex-agent-container switch develop
git -C ~/proj/scitex-agent-container pull --ff-only

# 2. Rebuild both layers. :base does not strictly need rebuilding
#    for sac-only changes, but :scitex pins to a local :base SIF
#    file, so rebuilding :base first guarantees freshness.
sac image build base    -y    # ~15-25 min
sac image build scitex  -y    # ~10-20 min  (FROM :base)

# 3. Restart the host listen daemon (picks up _listen/* changes).
sac listen restart            # or: systemctl --user restart sac-listen.service

# 4. Restart agents. They re-spawn against the freshly built SIF +
#    pick up new host-side argv from the updated runtimes/_apptainer_*.
#    The exact verb depends on how the agent was launched —
#    `sac agent stop <name> && sac agent start <name>`, or the
#    operator's job-scheduler hook.
```

For accounts the rebuild is also the trigger to roll a stale OAuth
token over: a `sac accounts refresh --all --skip-active` is run by the
federated timer (`scripts/systemd/README.md` § sac accounts refresh)
on a 2h cadence, but a fresh rebuild + restart cycle is the right
moment to confirm the snapshot binds the live credential the agent
will actually use.

## Pre-flight check: am I running the SIF I think I am?

Before declaring an incident a "broken fix", check the SIF
modification time against the merge date of the fix:

```bash
# What SIF is the agent using? (default ~/.scitex/agent-container/containers/)
ls -l ~/.scitex/agent-container/containers/*.sif

# What's the latest develop commit that touched the fix?
git -C ~/proj/scitex-agent-container log --oneline -1 \
    -- src/scitex_agent_container/runtimes/_apptainer_creds.py

# If SIF mtime < commit date, the SIF predates the fix.
# Rebuild before filing a bug.
```

This single check would have saved both of today's misdiagnoses.

## Related docs

* `docs/images.md` — full build / sandbox / freeze / rollback flow,
  plus the `~/.scitex/<pkg>/{containers,bin}` convention.
* `scripts/systemd/README.md` — host-daemon (sac-listen) operations,
  including the `sac listen restart` recovery verb that runs the
  SIGTERM → SIGKILL → relaunch sequence atomically.
* `docs/adr/0001-isolation-hardening.md` — the D1/D5 isolation flags
  that `runtimes/_apptainer_*` injects; if these change, a host
  reinstall is enough for the argv builder, but running agents still
  need to be respawned to use the new argv.
