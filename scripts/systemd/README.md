# sac accounts refresh — federated systemd-user timer

Headless rotation of the Claude Code OAuth access-token using the
long-lived refresh-token stored under
`~/.scitex/agent-container/accounts/<name>/.credentials.json`. Removes
the need for routine manual `claude /login` for stored accounts.

## The unit files are no longer hand-maintained here

This job is now **federated into `scitex_dev.jobs`** (the ecosystem-wide
scheduled-job registry). sac registers a single `JobSpec`
(`sac.accounts-refresh`) via the `scitex_dev.jobs` entry-point
(`src/scitex_agent_container/_jobs_plugin.py`), and scitex-dev generates
the `.service` + `.timer` unit files from that single source of truth.

The previously committed static `sac-accounts-refresh.service` /
`.timer` templates were **removed** to avoid a second, drifting copy of
the policy (they were pinned to the old `--all`, every-4h cadence).

## Policy (current)

| Field                | Value                                      |
| -------------------- | ------------------------------------------ |
| Command              | `sac accounts refresh --all --skip-active` |
| Cadence              | every **2h** (`OnUnitActiveSec=2h`)        |
| After boot/login     | `OnBootSec=15min`                          |
| Timeout              | `TimeoutStartSec=120s`                     |

`--skip-active` excludes the account matching the currently-active
`~/.claude` login so the in-use refresh_token is never rotated out from
under the live session.

## Install / uninstall

Generate and install the unit files via sac's federated wrapper (which
delegates to scitex-dev's ecosystem aggregator):

```bash
# Inspect what would be installed
sac dev systemd list

# Install (writes ~/.config/systemd/user/sac.accounts-refresh.{service,timer})
sac dev systemd install --yes
systemctl --user daemon-reload
systemctl --user enable --now sac.accounts-refresh.timer

# Verify
systemctl --user list-timers sac.accounts-refresh.timer
journalctl --user -u sac.accounts-refresh.service -n 50

# Remove
sac dev systemd uninstall --yes
```

Equivalent direct scitex-dev invocation:

```bash
scitex-dev ecosystem systemd install --name sac.accounts-refresh --yes
```

> Requires `scitex-dev>=0.16.0` (the release that adds `scitex_dev.jobs`).
> Older scitex-dev installs make `sac dev systemd` print an upgrade hint
> instead of failing.

The service is read-only with respect to source code; the only state
mutation is atomic write-back of the refreshed access_token to the
per-account credentials file. The unit exits non-zero only when EVERY
targeted account's refresh failed — that's the operator's signal that a
real `claude /login` is finally needed.
