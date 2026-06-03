# Deployment runbook — merged ≠ deployed

## The lesson, in one line

A merged sac change is **invisible to running agents** until the SIF is
rebuilt **and** the agents are restarted. CI green on `develop` only
means the code is correct — not that any agent is running it.

## Where this bites

Three real incidents from 2026-06-03 → 2026-06-04 illustrate the gap:

| Incident | Code merged | SIF rebuilt? | Host venv `pip install -U`? | Agents restarted? | Symptom |
|----------|-------------|--------------|-----------------------------|-------------------|---------|
| Account-pinned creds live-bind fix | 2026-06-01 (`d70e608` + `acf2cbb` on develop) | **No** (SIF built 2026-05-28) | **No** | n/a | Stale OAuth token after refresh → Claude 401 |
| SAC-from-SAC broker fail-loud (PRs #287/#288/#289) | 2026-06-03 | **No** (pre-existing SIFs) | n/a | n/a | Silent-200 disease persisted in running agents |
| Same creds fix, take two — **2026-06-04 03:00 storm** | 2026-06-01 (`d70e608` + `acf2cbb` already merged + `git pull`-ed) | Yes (SIF 0.21.9) | **No** (host venv still 0.21.3) | Yes | `//deleted` mounts fleet-wide → 401 at natural token expiry |

The first two were diagnosed as "the fix is broken" before recon showed
the fix was on `develop` but had never reached the SIF the running
agents were actually using.

**The third bite is the subtle one** and is the reason this section
deserves its own row in the table above: `git pull` on the source tree
updates **the source files**. It does **not** update the installed
`sac` binary in the host's venv. For any spawn-time argv builder
under `runtimes/_apptainer_*` (which lives in the host binary and
emits the apptainer `--bind` argv), the **installed wheel is what
matters**, not the source tree. The fix can be on disk in
`~/proj/scitex-agent-container/src/...` and still not be running on
the agents.

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

## Why a host install is required (not just a `git pull`)

When the host sac is installed in a venv via `pip install`, the venv
holds a **frozen wheel** built at install time. `git pull` updates the
source tree on disk; the venv's `sac` binary keeps running whatever
version was wheeled in when the venv was last bumped.

The canonical resolution check:

```bash
# What sac is on PATH, and what version does it run?
which sac
sac --version

# Where does the source tree on disk think it is?
git -C ~/proj/scitex-agent-container describe --tags --always
git -C ~/proj/scitex-agent-container log --oneline -1

# If the two differ, the installed binary is stale. `git pull` alone
# fixes nothing for any host-side argv emission.
```

Two install modes and what to do:

| Mode | Install command | What `git pull` does | What you ALSO need |
|------|-----------------|----------------------|--------------------|
| **Editable** | `pip install -e ~/proj/scitex-agent-container` | Updates source AND binary in lockstep | Nothing — `git pull` is sufficient |
| **Wheel** | `pip install scitex-agent-container` or `pip install git+...@develop` | Updates source only; binary stays pinned | `pip install -U git+...@develop` against the same venv |

If you don't know which mode you're in:

```bash
~/.env-3.11/bin/pip show scitex-agent-container | grep -E "Version|Location"
# "Location" pointing at site-packages → wheel mode (need pip install -U)
# "Location" pointing at ~/proj/scitex-agent-container/src → editable mode
```

**Recovery command** for wheel mode (this is what the 2026-06-04 storm
needed):

```bash
~/.env-3.11/bin/pip install -U git+https://github.com/ywatanabe1989/scitex-agent-container.git@develop
~/.env-3.11/bin/sac --version    # confirm new version
```

After `pip install -U`, **every running agent still has its old argv
in flight**. The host binary update only affects NEW spawns. To deploy
to running agents: a full agent restart cycle (the same one a SIF
rebuild needs).

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
# 1. Update the source tree on disk.
git -C ~/proj/scitex-agent-container fetch origin develop
git -C ~/proj/scitex-agent-container switch develop
git -C ~/proj/scitex-agent-container pull --ff-only

# 2. Re-install the host sac binary so changes to runtimes/_apptainer_*,
#    cli_pkg, and any other host-side code actually take effect.
#    (Skip ONLY if the venv is editable mode — see "Why a host install
#    is required" above for how to check.)
~/.env-3.11/bin/pip install -U git+https://github.com/ywatanabe1989/scitex-agent-container.git@develop
~/.env-3.11/bin/sac --version    # confirm new version on PATH

# 3. Rebuild both layers. :base does not strictly need rebuilding
#    for sac-only changes, but :scitex pins to a local :base SIF
#    file, so rebuilding :base first guarantees freshness.
sac image build base    -y    # ~15-25 min
sac image build scitex  -y    # ~10-20 min  (FROM :base)

# 4. Restart the host listen daemon (picks up _listen/* changes).
sac listen restart            # or: systemctl --user restart sac-listen.service

# 5. Restart agents. They re-spawn against the freshly built SIF +
#    pick up new host-side argv from the updated (re-installed!)
#    runtimes/_apptainer_*.
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

## Verification: "one clean cycle ≠ closed"

The 2026-06-04 03:00 storm proved the discipline: a single passing
refresh cycle after a creds-related deploy is **insufficient evidence**
that the fix took. The 19:35 cycle on 2026-06-03 passed clean, the fix
was declared closed, and then 23:35 storm-ed — because no
account-rotation had happened to break the file-bind in the 19:35
window. The bind was already broken on disk; we just hadn't tripped the
read.

### The mountinfo probe protocol for creds-related deploys

Use this **after any deploy that touches `runtimes/_apptainer_creds`,
`runtimes/_apptainer_auth`, or `_account/{credentials,claude_usage}`**.
Declare closed only when the probe stays clean across ≥2 full refresh
cycles (≥4-6h with the standard 2h `OnUnitActiveSec`).

```bash
# Probe — run right after fleet restart, then again at +2h and +4h.
for pid in $(pgrep -f "_runners.claude_session"); do
  d=$(grep -c '//deleted' /proc/$pid/mountinfo 2>/dev/null)
  s=$(readlink /proc/$pid/exe 2>/dev/null | xargs -I{} basename {})
  echo "pid=$pid deleted=$d exe=$s"
done
```

Expected at every probe: `deleted=0` for every running agent. If any
row shows `deleted≥1`, the bind has been unlinked by a refresher's
atomic rename — the agent will 401 at next token expiry.

A useful one-shot check that surfaces only the broken bind:

```bash
for pid in $(pgrep -f "_runners.claude_session"); do
  grep '/tmp/sac-claude' /proc/$pid/mountinfo 2>/dev/null | grep -q '//deleted' \
    && echo "BROKEN: pid=$pid (// deleted in /tmp/sac-claude mount)"
done
# Silent = clean.
```

For the desired post-fix shape on every running agent:

```
$snapshot_parent on /tmp/sac-claude type none (rw,bind)
```

A **file** bind (`$snapshot_dir/.credentials.json on /tmp/sac-claude/.credentials.json`)
is the broken shape — that's the spawn-time argv before #262, and is
what surfaced in the 2026-06-04 storm because the host venv hadn't
been bumped past 0.21.3.

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
* `docs/adr/0017-credential-rotation-and-refresh-race.md` — the
  credential-rotation model, the live-bind requirement, and why a
  single-file bind goes `//deleted` under any rotator that uses atomic
  `tmp + rename`. The 2026-06-04 storm was caused by the host venv
  shipping the pre-`#262` file-bind argv, exactly the failure mode the
  ADR's § "Failure mode 1" describes.
