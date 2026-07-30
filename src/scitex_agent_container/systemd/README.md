# systemd `--user` unit templates

Reference source for sac's host services. **Templates, not deployed artifacts** —
nothing in sac installs them, and `@PLACEHOLDER@`s must be filled per host.

| template | service |
|---|---|
| `sac-listen.service.template` | the fleet's HTTP/JSON control plane (`host_exec`, spawn, restart, liveness) |
| `sac.accounts-refresh.service.template` + `.timer.template` | headless OAuth refresh for stored Claude accounts |

Placeholders: `@SAC_BIN@` (absolute path to the **current** `sac`),
`@SAC_SECRETS_ENVRC@` (colon-separated secret `.src` files, host-specific).

## Why these are tracked at all

Until 2026-07-30 both units existed in exactly one place: live-only files under
`~/.config/systemd/user/`, untracked, with no history and no review — while
brokering the entire agent fleet. A host rebuild would have taken them, and any
pin applied to them.

Not committed verbatim: the live listen unit carries an
`Environment=SAC_SECRETS_ENVRC=` line enumerating ~30 secret file paths under
the operator's home. Those paths are not themselves secrets, but publishing a
map of the secrets layout to a public repository is a reconnaissance aid, and a
template has no reason to carry it.

## The measurement that motivated this

Measured on `ywata-note-win`, 2026-07-30. That host carries **three** `sac`
installations:

```
~/.env-3.11/bin/sac                      0.24.20   current release
~/.scitex/agent-container/venv/bin/sac   0.21.22   2026-07-17 wheel
~/.local/bin/sac                         0.21.11   src, "metadata only"
```

**The live listen daemon was running 0.21.22** — thirteen releases behind — read
off the running process (`pid 1590`), not the unit file, and confirmed by
importing metadata from the interpreter it was actually using. It had restarted
that morning and come back onto the old venv, because `ExecStart` pinned it. The
supervisor re-armed the stale code on every restart.

**The staleness propagates.** `_sac_binary.sac_binary()` resolves a child `sac`
by falling back to the executable *next to* `sys.executable` — deliberate, for
venv coherence. So a stale daemon hands its own stale binary to every agent it
spawns or restarts. One wrong path in one unit becomes a fleet-wide version
regression that no agent can see in any version string it reads about itself.

The flip side is that repointing `ExecStart` fixes **both** halves at once: the
daemon's own code, and the binary it hands children.

Separately, `sac.accounts-refresh.service` used `ExecStart=/usr/bin/env sac …` —
a bare name resolved against the unit's inherited PATH. Measured: bare `sac` is
**NOT FOUND** under a minimal PATH, so that unit worked only because systemd's
environment happened to contain one of the three, and which one was unpinned.
That ambiguity cost two agents a wrong measurement the same night: one reported
a JSON field "absent from the payload" having invoked the 0.21.22 binary, which
predates the field.

## Verifying a deployment

Ask the **running process**, never the unit file:

```bash
pgrep -f 'sac listen'
tr '\0' ' ' < /proc/<pid>/cmdline          # which interpreter, which sac
<that venv>/bin/python -c "import importlib.metadata as m; print(m.version('scitex-agent-container'))"
```

A unit file that pins a stale interpreter is a config that is correct about what
it *says* and wrong about what it *runs*. `sac`'s own `listen_cmds.py` already
notes three call sites that invoke `sac` bare, the unit's `ExecStart` among them.

## Deployment is an operator action

Restarting `sac-listen` drops `host_exec` brokering for every agent, so it is
outward-facing in a way ordinary code changes are not. Note also that these live
files are **regular files, not symlinks** into any dotfiles repo, so a dotfiles
deploy will not revert an edit — and will not supply one either.
