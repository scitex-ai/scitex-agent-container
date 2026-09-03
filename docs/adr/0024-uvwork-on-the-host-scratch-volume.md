# ADR-0024: `/uvwork` lives on the host scratch volume, not the root LV

**Status:** Accepted (2026-09-03).
**Operator directive** (Telegram, 2026-09-02): 「ディスクは /scratch
使ってくださいね」 — the disk is `/scratch`; use it.
**Relates to:** ADR-0003 (runtime home directory) and ADR-0006 (`to_home`
materialization), which placed the container `$HOME` in the overlay upper.
This ADR takes the one tree that does *not* belong there back out again.

## 1. The failure

The root logical volume on `scitex-compute-04` filled to **0 bytes four
times on 2026-09-02**.

Nothing about that is mysterious once the mechanism is named. Ninety of the
123 agent specs point their build tooling at `/uvwork`:

```yaml
    startup_commands:
      - export TMPDIR=/uvwork/tmp
      - export UV_CACHE_DIR=/uvwork/uv-cache
      - export UV_INSTALL_DIR=/uvwork/bin
      - export UV_PROJECT_ENVIRONMENT=/uvwork/venv-agent
```

The base image *creates* `/uvwork` (`containers/apptainer-base.def`) and
**nothing bound it**. An apptainer container's writes to an unbound path go
to the overlay's upper layer, and every agent's overlay lives under the sac
state root on the host's **root LV**. So the uv binary, the uv cache, the
agent venv and every temporary file each agent has ever written have been
accumulating in `overlays/<agent>/upper/uvwork`.

Measured on compute-04, 2026-09-03:

| agent | `overlays/<agent>/upper/uvwork` |
| --- | --- |
| `sac` | 11.7 GB |
| `scitex-dev` | 3.3 GB |
| `scitex-hub` | 3.0 GB |
| `scitex-cards` | 2.5 GB |
| `scitex-storage` | 1.9 GB |

Meanwhile `/scratch` is a **separate** logical volume on the same hosts, and
it is empty by comparison:

| host | `/scratch` size | free |
| --- | --- | --- |
| compute-04 | 3.0 T | 2.8 T |
| compute-03 | 3.0 T | 2.8 T |
| compute-01 | 295 G | 243 G |

114 of the 123 specs **already** bind `/scratch:/scratch:rw`. The volume was
mounted, bound, and unused for the one thing that was filling the disk.

(The 123 counts every spec directory on compute-04; `sac agents
scratch-migrate` reports 116, which is the same roster after
`fleet_spec_paths` drops `_`-prefixed scaffolding and directories with no
`spec.yaml`. Neither number is wrong; they count different things.)

The shape of the bug is not "uv is big". It is: **a write path that nobody
declared went wherever the container's writable layer happened to be**, and
the writable layer happened to be on the volume that must not fill.

## 2. Decision

### 2.1 One knob: `scratch_root:` in `config.yaml`

Where `/uvwork` lives is a **per-host** fact, so it is declared in the
per-host `config.yaml` and nowhere else. No environment variable selects it;
there is one dish.

```yaml
scratch_root: /scratch          # an absolute path that must EXIST
```

or, for a host that genuinely should keep `/uvwork` in the overlay:

```yaml
scratch_root: none
scratch_root_reason: root LV is 8T and there is no scratch volume here
```

`none` **requires** a reason. Keeping gigabytes of build state on the root
volume is exactly the failure above, so it may be a decision, but it may not
be an accident: a reason-less `none` is refused at config load, and so is a
`scratch_root_reason:` with no `scratch_root:` beside it.

### 2.2 The resolution table, and the fourth row

`_state/host_scratch.resolve_scratch_root()` answers once per start with a
fixed shape — `ScratchRoot(root, source, reason)`, where `root is None`
exactly when `source == "none"`:

| config.yaml | on this host | result |
| --- | --- | --- |
| `scratch_root: /abs` | `/abs` is a directory | `source=config`, `root=/abs` |
| `scratch_root: /abs` | `/abs` is absent | **REFUSE** |
| `scratch_root: none` + reason | — | `source=none`, `root=None` |
| *(nothing)* | `/scratch` is a mount point or directory | `source=default`, `root=/scratch` |
| *(nothing)* | no `/scratch` | **REFUSE** |

The default matters because **compute-01 and compute-03 have no
`config.yaml` at all** — a design that needed one on every host would have
been a design that does not ship.

The refusals matter more. The alternative to refusing is falling back to the
overlay, which is the original bug wearing a shrug: correct-looking,
silent, and back on the root LV. A refusal names the missing path, the
config key, the config file, and all three fixes (mount it, declare it, or
write the `none` decision down).

### 2.3 The bind

When a root resolves, `runtimes/_apptainer_scratch` creates

```
<scratch_root>/sac/agents/<agent-name>/uvwork      mode 0700
```

before exec and appends `--bind <that>:/uvwork:rw` to the flag argv. Notes
that are decisions rather than details:

* **Per agent.** An agent's venv, uv cache and `TMPDIR` are its private
  working set; two agents sharing one directory would share a venv.
* **0700 on creation, and never on an existing directory.** Restarts are
  idempotent, so an operator who tightened or loosened the mode is not
  silently overruled every time the agent restarts.
* **Emitted in `_apptainer_argv_finalize`, after `raw_args` and every
  spec-declared bind.** apptainer keeps the *first* bind to a destination,
  so a spec that binds `/uvwork` itself wins — the same rule the package
  already applies to its other fleet-default binds — and that case is
  logged, not silent. It is emitted *before* the secret-env lift so the
  credentials bind keeps its last-bind position.
* The spec's own `[ -x /uvwork/bin/uv ] || …` steps are unchanged. They now
  run against scratch, and rebuild there once on the first start after this
  lands, unless the migration below moved the existing tree across first.

### 2.4 `sac agents scratch-migrate`

The bind alone leaves the historical copy stranded in the overlay: shadowed,
never read again, still 11.7 GB on the root LV. The verb moves it.

```
sac agents scratch-migrate                     # dry-run — the DEFAULT
sac agents scratch-migrate --agent sac --json
sac agents scratch-migrate --apply             # the deliberate act
```

* **Dry-run is the default**, printing per-agent sizes, per-agent decisions
  and the total it would move.
* **Only a provably STOPPED agent is moved.** A running agent has the
  overlay mounted; it is refused *by name* with the command to stop it. An
  agent whose liveness the runtime adapter could not determine is refused
  too — "unknown" is not "stopped". The probe is the same adapter
  `sac agents status` uses, never a second opinion.
* **And only from a vantage where "stopped" means anything.** The first real
  dry-run, run from inside the `scitex-agent-container` agent, reported that
  agent — the one executing the probe — as *stopped*, and offered its
  10.3 GiB. `is_running` is a pid file plus `os.kill(pid, 0)`; the recorded
  pid was 3190806 while `/proc` inside the container topped out at 74275,
  because the container has its own PID namespace. Neither the pid file nor
  the adapter is wrong — the vantage is. So the verb now abstains outright
  when `APPTAINER_CONTAINER` / `SINGULARITY_CONTAINER` is set: every agent
  comes back UNKNOWN, which is a refusal, and the message says to run it on
  the host. Sizes still print, because the overlays are read through a bind
  mount and *that* reading is faithful.
* **An overlay two agents declare is refused, naming the others.** Also from
  the first dry-run: `scitex-hub` and `scitex-hub-mobile-ux` name one
  `--overlay`, as do `scitex-cards` / `scitex-todo` and eight `handyman-*`
  specs sharing `local-coder`. One 2.6 GiB tree was listed as movable twice.
  Applying that would move it for the first agent, hand the second a source
  that no longer exists, and file a shared tree into one agent's private
  directory. Which agent owns a shared overlay is a fleet decision sac may
  not make, so both rows refuse. Ownership is computed over the whole
  roster, never the `--agent` subset — sharing that only an unselected spec
  reveals is exactly what a narrow invocation would walk into.
* **Copy → verify → remove.** The overlay copy is deleted only after every
  path, size and symlink target in the destination has been compared with
  the source. A mismatch keeps the source and says so.
* A destination that is **already populated** is refused: the agent has
  restarted under the new bind and rebuilt there, which makes the overlay
  copy the older of the two.
* Exit codes: `0` sound, `1` the plan does not describe the sweep (no roster
  searched, an unreadable spec, an unknown `--agent`), `2` refused.

## 3. Consequences

* **Every start now depends on a resolvable scratch root.** On a host with
  neither `/scratch` nor a `scratch_root:` line, agents refuse to start
  until an operator makes a decision. That is the intended trade: an agent
  that will not start is visible, and an agent quietly refilling the root LV
  is not.
* The spec corpus is **untouched**. No `binds:` entry, no `startup_commands`
  edit, no per-agent migration — the bind is emitted by the runtime for
  every agent at once.
* `/uvwork` is still `/uvwork` inside the container, so `TMPDIR`,
  `UV_CACHE_DIR`, `UV_INSTALL_DIR`, `UV_PROJECT_ENVIRONMENT`,
  `_apptainer_listen_env`, `cli_pkg/_whoami` and `_dev_jobs_backend` all
  keep working with no change.
* **Jailed capsules get the same bind as everyone else**, deliberately: a
  jailed agent's uv cache is exactly as unwelcome on the root LV as anyone
  else's. Two facts make that safe rather than convenient. First,
  `runtimes/_apptainer_jail.enforce_jail` checks *operator-controlled* bind
  sources — `spec.apptainer.binds`, `raw_args --bind`, and the
  `APPTAINER_BIND` family — and this bind is emitted by sac itself, so it
  sits with the credentials, workspace-home and overlay-upper-home binds
  that were already outside that check. Second, `/scratch` is not one of the
  forbidden prefixes (`/data/gpfs`, `/data/scratch`, `/home`) anyway, so
  even a spec that declared it by hand would pass.
  The honest edge: a host that declares `scratch_root:` at a path *under*
  `/home` gives its jailed capsules a `/home`-backed bind that
  `enforce_jail` will not see, because sac's own binds are not the thing
  that check polices. On a host with no separate volume, `scratch_root:
  none` with a written reason is the better answer than a `/home` path.
* The host registry row (`_state/host_registry`) is **not** extended. Where
  scratch lives is per-host configuration, and ADR-0022's rule is that
  configuration lives in files under git, not in the state database.

## 4. What was rejected

* **A `scratch:` field on the host registry row.** It is configuration, not
  state (ADR-0022), and the registry is synchronised between hosts — a
  per-host path is precisely the wrong thing to replicate.
* **An environment variable (`SAC_SCRATCH_ROOT`).** Two knobs for one fact
  is how a host ends up with a `config.yaml` that describes a launch nobody
  is performing.
* **Silently falling back to the overlay when `/scratch` is missing.** This
  is the bug, restated as a feature.
* **Binding `<scratch>/uvwork` shared by all agents.** One venv, many
  writers.
* **Deleting the overlay copies instead of moving them.** Every agent would
  then rebuild uv and its venv on the next start, for no gain over a copy
  that takes seconds on the same host.
