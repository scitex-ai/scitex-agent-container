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

**Fix:** `--net --network=none` (full isolation; agent loses Claude
API access) OR `--net --network=bridge` (independent netns + explicit
egress allowlist for `api.anthropic.com`). Bridge + allowlist is the
realistic answer; pure isolation kills the agent.

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
| Claude session state | prior conversation leaks into next start |
| accidentally shared overlay | horizontal contamination between agents |

**Fix:**
- Clew experiments: ephemeral overlay (create on start, destroy on stop)
- Persistent overlay: include the overlay hash in the verification chain
- One agent ↔ one overlay; never share

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
      #   --net --network=none  → no egress (most secure; agent can't reach Claude API)
      #   --net --network=bridge  → bridged + allowlist (realistic for Claude agents)
      # PID / IPC / UTS (§5): not in --containall; add if you need them.
      #   "--pid", "--ipc", "--uts"
```

**Per-agent binds** declare exactly what's allowed; nothing else
visible:

```yaml
    binds:
      # Allow only what the agent's task needs. Default to :ro.
      - /home/me/proj/<one-package>:/home/me/proj/<one-package>:ro
      # NEVER bind ~/.ssh, ~/.gitconfig, ~/.claude unless the agent
      # provably needs them — reduces blast radius of indirect leaks (§9).
```

**Preflight checks** in `startup_commands` fail-fast on leak detection:

```yaml
    startup_commands:
      # §3 — not root inside container.
      - 'test "$(id -u)" != "0" || (echo "ERROR: running as root" && exit 1)'
      # §1, §9 — known host-only path should be invisible.
      - 'test ! -e /opt/python3.12 || (echo "ERROR: host /opt leaked" && exit 1)'
      # §9 — no host identity.
      - 'test ! -d /home/$USER/.ssh || (echo "ERROR: host ~/.ssh visible" && exit 1)'
      - 'test ! -e /home/$USER/.gitconfig || (echo "ERROR: host ~/.gitconfig visible" && exit 1)'
      # Then the real install.
      - "uv pip install -e ./scitex-foo[all] --quiet"
```

These checks run BEFORE any real work. Any leak hard-stops the agent
with an explicit error, never silently propagates.

---

## Roadmap for sac's isolation surface

| Item | Status |
|---|---|
| `apptainer.raw_args` field (operator-declared) | ✅ shipped |
| Per-agent preflight in `startup_commands` | ✅ pattern documented (this doc) |
| Default `--containall` in apptainer argv if operator doesn't override | ✅ shipped (auto-prepended when `apptainer.relaxed: false`) |
| `apptainer.relaxed: true` opt-out to disable hardened defaults | ✅ shipped (`spec.apptainer.relaxed`) |
| sac-injected preflight (before user's `startup_commands`) | ⏳ planned |
| AgentCard `isolation_level: hardened \| relaxed \| custom` field | ⏳ planned |
| `sac image overlay {init,reset,prune}` for ephemeral-overlay workflows | ⏳ planned |

The AgentCard field is the differentiator: external verifiers (orochi,
Clew) can attest "this agent ran at isolation level X" by reading the
card alone, no SIF introspection required. **Today** the field doesn't
exist; the operator's spec.yaml is the only source of truth.

## One-line summary for papers / READMEs

> Apptainer's default behavior prioritizes HPC convenience: it
> auto-binds the user's home, `/tmp`, `/proc`, `/sys`, and `/dev`;
> inherits all environment variables; shares the host's network, PID,
> and IPC namespaces; and uses the host's UID/GID. For
> reproducibility, all of these defaults must be inverted. sac uses
> `--containall` (filesystem isolation), `--cleanenv` (environment
> isolation), `--net --network=none` or controlled bridge (network
> isolation), and explicit `--bind` declarations for every host path
> that must be visible. Preflight checks within `startup_commands`
> verify each isolation property at boot and fail hard on any breach.

## See also

- [`spec-reference.md`](spec-reference.md) — `spec.apptainer.raw_args` field
- [`talking-to-agents.md`](talking-to-agents.md) — A2A surface (where isolation level should be advertised)
- [`how-sac-works.md`](how-sac-works.md) — overall architecture
