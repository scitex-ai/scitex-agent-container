# Container Isolation in sac

> **TL;DR.** Apptainer's defaults prioritize HPC convenience: it
> auto-binds `$HOME`, `/tmp`, `/proc`, `/sys`, `/dev`; inherits every
> environment variable; shares the host's network, PID, and IPC
> namespaces; and uses the host's UID/GID. For agent workloads —
> where the container is meant to be a security boundary — these
> defaults are upside-down. This document enumerates every leak path
> and the sac-side default flip that closes it.

This is also the reference for the **Clew** reproducibility experiments:
"same SIF + same spec.yaml" can only be a real claim when isolation
is declared, mechanically enforced, and externally verifiable. sac's
position: isolation level is a first-class spec.yaml field, sac
chooses **hardened by default**, and the AgentCard publishes the
agreed level so peers can audit it.

---

## Apptainer's default leak paths (10 categories)

### 1. Auto-bound filesystem paths

`apptainer exec` without options auto-binds:

| Path | What leaks | Impact |
|---|---|---|
| `$HOME` | dotfiles, `.ssh/`, `.gitconfig`, `.bash_history`, `.cache/`, app config | agent can read host identity AND mutate it |
| `/tmp` | other processes' temp files, locks, sockets | cross-container contamination, host-state writes |
| `/var/tmp` | persistent temp files | state persists across runs |
| `/proc` | every host PID's metadata | host process introspection |
| `/sys` | kernel settings, devices | host hardware fingerprint |
| `/dev` | device nodes (`/dev/null`, GPU, etc.) | device access |
| `$PWD` | the cwd at `apptainer exec` time | accidental scope creep |

**Fix:** `--containall` (cuts all of the above in one flag), or
`--contain --no-home` for finer control.

### 2. Environment variable inheritance

Apptainer forwards essentially every host env var into the container:

| Variable | What leaks |
|---|---|
| `$PATH` | host bin paths (`/usr/local/cuda/bin`, etc.) — references that don't exist inside |
| `$HOME` | host home path — tools that expect a specific `$HOME` get confused |
| `$USER`, `$LOGNAME` | host username |
| `$SSH_AUTH_SOCK` | host ssh-agent socket — agent inherits ssh-agent identity |
| `$DISPLAY`, `$WAYLAND_DISPLAY` | host X / Wayland session |
| `$DBUS_SESSION_BUS_ADDRESS` | host D-Bus access |
| `$AWS_*`, `$GCP_*`, `$ANTHROPIC_API_KEY`, … | every cloud / API credential |
| `$XDG_RUNTIME_DIR` | host runtime dir (`/run/user/$UID`) |
| `$LANG`, `$LC_*` | locale (host vs container OS mismatch) |

**Fix:** `--cleanenv` wipes everything; pass only what's needed via
`--env KEY=VAL`.

### 3. User / permission inheritance

| Item | Default | What leaks |
|---|---|---|
| UID / GID | host user inherited as-is | files created in container land on host with host UID |
| supplementary groups | host's full group list | host's group permissions effective inside |
| `/etc/passwd`, `/etc/group` | host's partially visible | host user table partially exposed |
| `/etc/resolv.conf` | host's used as-is | host DNS config |
| `/etc/hosts` | host's used as-is | host hostname map |

**Fix:** `--fakeroot` for UID 0 mapping (with caveats — needs user
namespaces in the kernel), or `--no-mount /etc/passwd,/etc/group`.

### 4. Network transparency

Default: the container **shares the host's network namespace**.

| Leak | What's possible |
|---|---|
| host loopback (127.0.0.1) | container reaches `127.0.0.1:7878` (sac listen) directly |
| host Unix sockets | `/var/run/docker.sock`, `/run/postgresql/...`, etc. |
| host `/etc/resolv.conf` | host DNS resolver |
| every host-bound port | every service the host exposes is reachable |
| host interfaces | `eth0`, `wlan0` directly addressable |

**This is the deepest one for sac.** A container running an agent
should NOT be able to bypass sac listen's bearer auth by talking to
its loopback directly. With shared netns it can.

**Fix:** `--net --network=none` (full isolation; agent loses inference
API access) OR `--net --network=bridge` (independent netns + explicit
egress allowlist for the inference endpoint this agent is configured
for — `api.anthropic.com` by default, the `spec.claude.provider`
`base_url` host under an override). Bridge + allowlist is the
realistic answer; pure isolation kills the agent.

**Realistic trade-off (current sac default).** sac currently uses the
host netns — agents reach `sac listen` on `127.0.0.1` (A2A) and any
MCP server on host loopback (hub push channels, etc.) over the same
path. Naive `--network=bridge` would isolate host services but break
both A2A and MCP-over-loopback. The realistic path forward is
`--network=bridge` + binding `sac listen` on the bridge interface +
injecting an `sac-host` hostname into `/etc/hosts` so MCP URLs stay
transport-stable (e.g. `http://sac-host:7878`) — none of which is
shipped yet. We accept the host-loopback exposure today as a known
limitation; see roadmap row below.

### 5. Process / IPC / UTS namespaces

| Item | Default | Impact |
|---|---|---|
| PID namespace | shared | `ps aux` sees host PIDs; `kill` can target host processes (subject to UID checks) |
| IPC namespace | shared | host SysV IPC + shared memory accessible |
| UTS namespace | shared | `hostname` returns host's name |
| cgroup | inherited | resource limits hit host-wide, not container-wide |

**Fix:** `--pid`, `--ipc`, `--uts` — note these are NOT included in
`--containall` and must be specified separately.

### 6. SIF build-time leaks (Apptainer-specific)

The SIF itself can carry host-derived metadata baked at build time:

| Source | Example leak |
|---|---|
| `.def` `%files` | absolute host paths copied verbatim into the layer |
| build host's `/etc/passwd` | snapshotted into the SIF |
| env-var-passed credentials | accidentally baked into a layer |
| absolute symlinks | targets pointing at the BUILD host |

**Implication for Clew:** "same SIF hash = same environment" is only
true if the SIF was built without host-specific data leaking in.

**Fix:**
- Build SIFs with `--fix-perms --force` (normalizes host metadata)
- Use ARG (build-time variables) in `.def`, never hardcoded paths
- Build on a clean / CI host, not a developer laptop

### 7. Overlay state persistence

`overlay.img` is a writable layer that persists across container
restarts. It's a feature (hot-start cache for pip installs) AND a
leak vector (previous-run state contaminates the next run):

| Persists across runs | Risk |
|---|---|
| `/tmp` writes | accumulating garbage; race conditions if two runs overlap |
| `~/.cache/pip`, build artifacts | unintended reproducibility break (same SIF, different overlay → different result) |
| agent session state | prior conversation leaks into next start |
| accidentally shared overlay | horizontal contamination between agents |

**Fix:**
- Clew experiments: ephemeral overlay (create on start, destroy on stop)
- Persistent overlay: include the overlay hash in the verification chain
- One agent ↔ one overlay; never share

**Declarative auto-create (sac).** sac drives overlay provisioning
from the spec so new peers don't require a manual
`apptainer overlay create` setup step. Two overlay FORMS, each with its
own provisioning path:

**(a) Directory overlay** — the fleet default (persistent per-agent
`$HOME`, package installs, caches). Declared either as the modeled field
or as a raw_arg, in EITHER spelling apptainer accepts:

```yaml
spec:
  apptainer:
    overlay: ~/.scitex/agent-container/containers/overlays/<agent>/
    # …or, under relaxed mode, via the escape hatch:
    raw_args:
      - --overlay=/abs/path/overlays/<agent>/   # =-joined, or
      - --overlay                               # space-separated
      - /abs/path/overlays/<agent>/
```

sac **auto-provisions** it before every launch, idempotently, creating
`<overlay>/`, `<overlay>/upper/` and `<overlay>/work/` (mode 0755) —
see `runtimes/_apptainer_overlay.ensure_overlay_dirs`, called from
`build_run_argv`, the one choke point both the SDK and TUI runtimes pass
through. This is a hard precondition, not a nicety: apptainer creates
`upper/` and `work/` itself but **`lstat()`s the overlay root and refuses
to create it**, so a missing root is a fatal
`while loading overlay images: failed to open overlay image … no such
file or directory`. Before this was explicit, the fleet's overlays existed
only as a side-effect of the `to_home` upper-home materialisation, whose
resolver read only the space-separated spelling — so a brand-new agent
scaffolded with the `=`-joined spelling was **stillborn**. If the overlay
still cannot be created (unwritable parent), sac raises
`OverlayProvisionError` naming the path and the exact `mkdir -p` fix,
rather than letting apptainer die with a raw FATAL.

**(b) Sized image overlay** — a loopback ext3 file:

```yaml
spec:
  apptainer:
    overlay: proj-<peer>.overlay.img   # path (workdir-relative ok)
    overlay_size: "5G"                  # units: M/MB/G/GB only
    overlay_create_if_missing: true     # default; set false to gate off
```

Semantics:
- `overlay_size` set + overlay path missing + `overlay_create_if_missing`
  true (default) → sac runs
  `apptainer overlay create --size <MB> <path>` before launch.
- `overlay_size` set but `overlay_create_if_missing: false` → sac raises
  `FileNotFoundError` (operator must pre-create).
- `overlay_size` empty + overlay missing → sac raises
  `FileNotFoundError` early with a helpful message instead of letting
  apptainer fail cryptically at exec time. (Directory overlays never
  reach this branch — they are auto-provisioned per (a). `overlay_size`,
  an `.img`/`.ext3`/`.sif` suffix, or an existing non-directory at the
  path are what mark an overlay as an IMAGE; sac never `mkdir`s one.)
- K/KB units are explicitly rejected — apptainer's
  `overlay create --size` takes integer MB.

**Overlay masking of base-baked packages (incident 2026-07-22).**
The overlay upper WINS over the base SIF. A `pip install` of a package the
base already bakes into `/opt/venv-sac` lands in
`<overlay>/upper/opt/venv-sac/.../site-packages/` and silently shadows the
base copy for that one agent — across every later base rebuild and restart.
The fleet carried stale `scitex_cards` fossils (0.16.0–0.17.4) this way:
restarts onto a new base were no-ops, and the all-clear had to be retracted.

The operational rule (also printed by `sac agents check-health` when the
detector fires): **NEVER pip-install a base-baked package into a running
agent's overlay.** A hotfix either force-reinstalls the EXACT base version,
or recreates the overlay (stop the agent, remove
`<overlay>/upper/opt/venv-sac`, restart onto the base).

Detection (per-agent in `sac agents check-health`, fleet-wide via
`scitex_agent_container._maintenance.sweep_agent_overlays`): a
`*.dist-info` in the upper's site-packages for a package the base provides
= MASKED. A bare package directory holding only `__pycache__` (zero
top-level `*.py`, no dist-info) is a benign overlayfs copy-up, NOT masking
— counting those once produced a false fleet scare. The verdict is
three-valued: an overlay we cannot read (missing root, unreadable upper,
unreadable base package set) reports UNKNOWN, never clean.

### 8. `--writable` vs `--writable-tmpfs` confusion

- `--writable` — modifies the SIF itself. Destroys SIF immutability.
  Used by accident → SIF hash changes → "same SIF" claim broken.
- `--writable-tmpfs` — tmpfs (in RAM); writes are ephemeral. Safe.
  Cannot combine with `--overlay <image>`; pick one.
- `--overlay` — writable image file; persists. Safe for reproducibility
  if the overlay hash is recorded.

**Fix for Clew:** never `--writable`. Use `--overlay` (recorded) or
`--writable-tmpfs` (ephemeral). For sac per-agent auditors we use
`--overlay` so pip's editable installs persist for hot-start; tmpfs
is given up because the overlay catches `/tmp` writes anyway.

### 9. Indirect leakage via bind-mounts

The most subtle category — the container respects `:ro` on the bind
itself, but the bound content can chain to host state:

| Path | How it leaks |
|---|---|
| `~/.ssh` bound | ssh out to other hosts → mutate them OR receive callback → mutate this host |
| `~/proj/foo/.venv/bin/python` symlinks to `/opt/...` | container follows the symlink, lands on container's `/opt` (overlay) OR host `/opt` depending on bind |
| `.git/config` with `[url "..."]` directives | `git clone` reaches unexpected remotes |
| Python `.pth` editable-install files | `sys.path` lands on bound dirs containing more symlinks |
| Symlinks inside a bind-mount pointing OUT of the mount | container follows out of the mount to wherever the symlink points |

**This is what bit the scitex-stats-auditor PoC.** A symlink inside
the bound `~/proj/scitex-stats/.venv/bin/python` pointed at
`/opt/python3.12/bin/python3.12` — the container resolved it to its
OWN `/opt`, decided the path was missing, "fixed" it inside the
overlay. The agent's mental model thought it had patched the host.

**Fix:**
- Don't bind `~/.ssh` unless the agent provably needs it (most don't)
- Don't bind venvs at all if you only need source — bind `src/` only
- Preflight: assert known host-only paths are NOT visible inside (see §sac defaults below)

### 10. Apptainer version differences

Behavior varies across versions:

| Version | Difference |
|---|---|
| `< 1.0` (legacy Singularity) | `--containall` slightly different scope |
| `1.0 – 1.2` | `/dev` bind looser |
| `1.3+` | some isolation tightened by default |
| any | `--fakeroot` requires user namespaces (host kernel config) |

**Implication for Clew:** "same SIF + same spec.yaml" can produce
different results across Apptainer versions. Record `apptainer
--version` in the experiment metadata.

---

## sac's hardened defaults

For agent workloads, sac's recommended `apptainer.raw_args` (and
where it's safe to make these defaults) closes most leak paths:

```yaml
spec:
  apptainer:
    raw_args:
      - "--containall"        # §1: filesystem isolation
      - "--cleanenv"          # §2: environment isolation
      # Network (§4): pick one based on workload —
      #   --net --network=none  → no egress (most secure; agent can't reach its inference API)
      #   --net --network=bridge  → bridged + allowlist (the realistic shape for any agent)
      # PID / IPC / UTS (§5): not in --containall; add if you need them.
      #   "--pid", "--ipc", "--uts"
```

### Canonical container HOME — `/home/agent` (D5)

Apptainer's default behavior sets `$HOME` inside the container from
the host operator's passwd entry (e.g. `/home/ywatanabe`). Even under
`--containall` the directory is scaffolded as a side-effect, so any
bind whose target descends `/home/<operator>/` populates `$HOME` and
makes spec.yaml operator-specific.

sac auto-injects `--home /home/agent` (skipped only when
`apptainer.relaxed: true`). Inside the container:

- `$HOME == /home/agent`, regardless of the operator's host username.
- Bind targets use the canonical HOME: `~/proj/foo:/home/agent/proj/foo:ro`.
- The host operator's home (`/home/ywatanabe`, `/home/alice`, …) is
  never created inside the container.
- spec.yaml is operator-agnostic — the same file runs identically on
  any operator's host.

**Per-agent binds** declare exactly what's allowed; nothing else
visible:

```yaml
    binds:
      # Source side: ~ expands to operator's host home (sac expands it
      # before passing to apptainer). Target side: canonical
      # /home/agent/... — operator-independent.
      - ~/proj/<one-package>:/home/agent/proj/<one-package>:ro
      # NEVER bind ~/.ssh, ~/.gitconfig, ~/.claude unless the agent
      # provably needs them — reduces blast radius of indirect leaks (§9).
```

**Preflight checks** are sac-injected as a `bash -c` wrapper around
the inner command; they run BEFORE any operator `startup_commands`:

```bash
# D5 preflight (auto-injected; not in spec.yaml)
test "$(id -u)" != "0" || exit 11           # not root (or userns-fakeroot — see below)
test "$HOME" = "/home/agent" || exit 12      # canonical HOME — drift = misconfigured
```

**Interaction with `apptainer.fakeroot: true`.** Under fakeroot the
in-container `id -u` returns 0, which would normally trip the first
check. sac detects fakeroot at preflight time via `/proc/self/uid_map`:
when the map is a single-line `0 <host_uid> 1` with `host_uid != 0`,
the agent is running as userns-fakeroot (root inside, operator on
host) — the root-check is treated as passed. A bare `id -u == 0`
without that map (somehow escaped to real root) still fails fast.

Two static lines, attestable by hash. The "no host leak" property
falls out of `--containall` (no auto-mounts) + canonical HOME (no
operator-specific `$HOME` scaffolding) + the declared `binds:` list
(reviewable on the AgentCard). Any leak hard-stops the agent with an
explicit error.

---

## Roadmap for sac's isolation surface

| Item | Status |
|---|---|
| `apptainer.raw_args` field (operator-declared) | ✅ shipped |
| Per-agent preflight in `startup_commands` | ✅ pattern documented (this doc) |
| Default `--containall` in apptainer argv if operator doesn't override | ✅ shipped (auto-prepended when `apptainer.relaxed: false`) |
| `apptainer.relaxed: true` opt-out to disable hardened defaults | ✅ shipped (`spec.apptainer.relaxed`) |
| Default `--cleanenv` + `--writable-tmpfs` auto-prepend (D1) | ✅ shipped |
| sac-injected static preflight (D2; refined to D5 invariants) | ✅ shipped |
| AgentCard structured `isolation` block (D3) | ✅ shipped |
| `sac agents check` warns on host-mirroring bind targets (D4) | ✅ shipped |
| Canonical container `$HOME=/home/agent` auto-injected via `--home` (D5) | ✅ shipped |
| `apptainer.fakeroot: true` opt-in (userns root inside container) | ✅ shipped |
| Network: shared host netns for MCP/A2A interop (known limitation) | ⏳ planned migration to `--network=bridge` + bridge-IF bind + `sac-host` hostname injection — keeps MCP URLs transport-stable while closing host-loopback exposure |
| `sac image overlay {init,reset,prune}` for ephemeral-overlay workflows | ⏳ planned |

The AgentCard field is the differentiator: external verifiers (fleet
hubs, Clew) can attest "this agent ran at isolation level X" by reading the
card alone, no SIF introspection required. The shape published at
`/.well-known/agent-card.json` (D3 + D5):

```json
"x-scitex-agent-container": {
  "isolation": {
    "level": "hardened",
    "containall": true,
    "cleanenv": true,
    "writable_tmpfs": false,
    "home_canonical": "/home/agent",
    "fakeroot": false,
    "preflight_passed": ["uid-nonzero", "home-canonical"],
    "preflight_allowed": [],
    "binds_count": 3,
    "binds_writable_count": 0
  }
}
```

`level: hardened` is the human shorthand for "all booleans align with
sac defaults + `preflight_allowed: []`". A run with any opt-out
(`relaxed: true`, `fakeroot: true`, an entry in `preflight_allowed`)
publishes `level: custom` plus the explicit booleans, so the verifier
sees exactly what changed instead of a flat "non-standard" label.

## One-line summary for papers / READMEs

> Apptainer's default behavior prioritizes HPC convenience: it
> auto-binds the user's home, `/tmp`, `/proc`, `/sys`, and `/dev`;
> inherits all environment variables; shares the host's network, PID,
> and IPC namespaces; and uses the host's UID/GID. For
> reproducibility, all of these defaults must be inverted. sac uses
> `--containall` (filesystem isolation), `--cleanenv` (environment
> isolation), `--home /home/agent` (canonical operator-independent
> HOME), `--net --network=none` or controlled bridge (network
> isolation), and explicit `--bind` declarations for every host path
> that must be visible. A sac-injected two-line preflight verifies
> `id -u != 0` and `$HOME == /home/agent` at boot and fails hard on
> any breach.

## See also

- [`spec-reference.md`](spec-reference.md) — `spec.apptainer.raw_args` field
- [`talking-to-agents.md`](talking-to-agents.md) — A2A surface (where isolation level should be advertised)
- [`how-sac-works.md`](how-sac-works.md) — overall architecture
