---
description: |
  [TOPIC] scitex-agent-container — agent image build & rebuild (apptainer .sif)
  [DETAILS] The FOUR-layer chain (system-deps → python-pkgs → base → scitex), where each .def + .sif lives (dotfiles containers/), how sac is installed from the %files-staged source tree, the `sac image build <layer>` / `scitex-container build` commands, which layer to rebuild for a given change (Python pin → python-pkgs; apt/rust → system-deps), the @develop→@tag stable pin, and the gotcha that runner/channel changes do NOT reach running agents until the image is rebuilt.
tags: [scitex-agent-container-image-build]
---

# Image build & rebuild (apptainer .sif)

The container images agents run are **apptainer** `.sif` files, defined in the
dotfiles (canonical SSoT), **not** in this repo:

```
~/.dotfiles/src/.scitex/agent-container/containers/
  sac-base/sac-base.def       sac-base.sif      # base layer (git, uv, node, …)
  sac-scitex/sac-scitex.def   sac-scitex.sif    # default image (sac + scitex[all])
  overlays/
```

An agent uses `sac-scitex` unless its `spec.yaml` sets `spec.image`.

## How sac gets into the image

`sac-scitex.def` (`%post`) installs the runner from **git, branch-pinned**:

```bash
uv pip install --python /opt/venv-sac/bin/python \
    ... claude-agent-sdk "scitex[all]" \
    "git+https://github.com/ywatanabe1989/scitex-agent-container.git@develop"
```

So the image is a **point-in-time snapshot of `@develop`** taken at build time —
NOT an editable mount of the host checkout.

## ⚠️ Critical gotcha — rebuild to ship runner/channel changes

A merged change to the runner / channel adapter (e.g. a new `--channels` flag, the
`sac mcp channel` auto-register, the auto-ack) **does NOT reach running agents**
until the image is rebuilt. The in-container `/opt/venv-sac` runner stays at
whatever `@develop` was when the `.sif` was last built. Symptom of the mismatch:
the runner crash-loops at boot, e.g.

```
python -m scitex_agent_container._runners.claude_session: error:
  unrecognized arguments: --channels server:sac
```

(host emits the new flag; stale container runner rejects it). Always rebuild the
image after a runner/channel change before expecting agents to pick it up.

## Build / rebuild commands

The stack is FOUR layers, each built `FROM` the one below it:

    system-deps  ->  python-pkgs  ->  base  ->  scitex

Split on 2026-08-14 because layers 1 and 2 have very different rebuild
frequencies: the OS floor (apt + rustup + source-built `tree` + cargo-built
`rtk`) is most of the bake wall-clock and changes monthly, while the Python
pin set above it changes weekly. Fused in one recipe, every pin bump re-paid
the whole apt/cargo cost.

A `:base` container carries exactly what it always did — the split moved
*where* things install, not what the image contains.

```bash
# Preferred (versioned). Bottom-up on a cold start:
sac image build system-deps -y    # 1: OS + apt + node + rust + yq/gdu/tree/rtk
sac image build python-pkgs -y    # 2: /opt/venv-sac + claude-agent-sdk + sac
sac image build -y                # 3: :base (default) — bakes the versions manifest
sac image build scitex -y         # 4: FROM :base + scitex[all]

# After the first pass, rebuild ONLY the layer you changed. A Python-pin bump is:
sac image build python-pkgs -y && sac image build -y   # never re-pays the apt/rust cost

# Each layer FAILS LOUD before invoking apptainer when its prerequisite SIF is
# absent, naming the IMMEDIATE parent and the exact command to build it.

# Raw apptainer (from the containers/ dir; you must arrange the prerequisite
# SIF adjacency yourself — `sac image build` does that staging for you):
apptainer build sac-system-deps.sif apptainer-system-deps.def
apptainer build sac-python-pkgs.sif apptainer-python-pkgs.def  # From: ./sac-system-deps.sif
apptainer build sac-base.sif        apptainer-base.def         # From: ./sac-python-pkgs.sif
apptainer build sac-scitex.sif      apptainer-scitex.def       # From: ./sac-base.sif
```

`sac-scitex.sif` is multi-GB; the build pulls `scitex[all]` (≈1–3 min with uv) plus
apt deps — budget several minutes. Restart agents after a rebuild so they pick up
the new `.sif`.

### Low-priority default (incident-local-heavy-build)

`sac image build` **self-demotes by default** — the whole bake (staging copy,
`apptainer build`, `%post` apt/pip, mksquashfs) runs at CPU `nice 19` + IO
best-effort lowest (`ionice -c 2 -n 7`) so a ~40-min build can't starve an
interactive host (2026-07-10: load 27 → 50+ during a normal-priority bake).
A one-line notice is printed when demotion is active:

```
building at low priority (nice 19 + ionice best-effort low); pass --no-nice for full speed
```

**Why best-effort-low and NOT the idle IO class (`-c 3`)**: field-tested the
same night — a host SIF build at `ionice -c 3` died silently at the
"Creating SIF file..." (mksquashfs) stage on the loaded host: process
vanished, no error in the build log, no OOM trace, the publish symlink never
swapped. Idle-class IO is only serviced when the disk is otherwise idle, so
under sustained load it can starve indefinitely (and appears to have gotten
the squash stage killed or wedged-then-reaped). `-c 2 -n 7` still yields to
all interactive IO but is guaranteed forward progress. If you ever *want*
harder demotion, run the build under `ionice -c 3` yourself and accept the
starvation risk.

Opt out on dedicated build machines / CI with `sac image build ... --no-nice`,
or fleet-wide via `SAC_BUILD_NO_NICE=1`. When `ionice` is missing the build
degrades gracefully to nice-only (warning line, no crash). Agent-start lazy SIF
builds (`resolve_sif` → `apptainer build` for a cold `docker://` image or
`def_file`) are prefixed with `nice -n 19 ionice -c 2 -n 7` the same way; the
same env var opts out.

## Pinning a stable version (vs tracking @develop)

`@develop` means every rebuild tracks the tip of develop. For a reproducible
**stable** image, edit the def to pin a release tag, then rebuild:

```bash
# in sac-scitex.def: change @develop → @v0.17.3 (a released tag)
sac image build scitex -y
```

This is the concrete mechanism behind a "stable vs develop" SAC split: the agent
image = the pinned stable runner; bump it deliberately.

## When `sac image build` is impossible — bind the host editable over the SIF

On HPC sites (e.g. Spartan) where `sudo` is forbidden AND the user has **no
`/etc/subuid` mapping** (no unprivileged user-namespace), `apptainer build`
cannot run at all — neither the sudo path nor the auto-fakeroot path from
`sac image build` (PR #307) succeeds. Verify with:

```bash
grep "$USER" /etc/subuid /etc/subgid    # empty → fakeroot is unavailable
```

In that case you **cannot rebuild the SIF**. The canonical workaround is to
**bind-mount the host editable `scitex_agent_container` package over the SIF's
site-packages copy**. The SIF's `/opt/venv-sac/bin/sac` is a click entry-point
wrapper that imports `scitex_agent_container`; if that package path is
bind-overridden by the host editable, the SIF's `sac` transparently runs the
host code without any rebuild — `git pull` + re-exec is the whole update loop.

### Spec-level bind (recommended for fleet)

Add to the agent's `spec.apptainer.binds` so every launch picks it up:

```yaml
spec:
  apptainer:
    binds:
      - source: /abs/path/to/scitex-agent-container/src/scitex_agent_container
        target: /opt/venv-sac/lib/python3.12/site-packages/scitex_agent_container
        read_only: true
```

### Direct `apptainer exec` (operator one-shot)

For a quick test before baking into spec:

```bash
apptainer exec \
  --bind /abs/.../src/scitex_agent_container:/opt/venv-sac/lib/python3.12/site-packages/scitex_agent_container:ro \
  sac-scitex.sif \
  sac --version    # → reports the HOST editable's version, not the SIF-baked one
```

### Caveats

* The host editable's **Python deps must be a subset** of what the SIF's venv
  already has installed. If a sac PR adds a new dep (rare — most PRs are
  code-only), `import scitex_agent_container` inside the SIF will hit
  `ModuleNotFoundError` for the new dep. That's the signal a real rebuild
  is needed.
* The host repo's `src/scitex_agent_container/` path **must exist on a
  filesystem the SIF can see** (typically a shared `/data/...` mount on HPC).
* Use `read_only: true` (or `:ro` on the CLI) so the in-SIF process can't
  mutate host source files.
* This is a workaround for the no-sudo/no-subuid case; on hosts where
  rebuild IS possible, prefer `sac image build` so the SIF is a real
  point-in-time snapshot (auditability, reproducibility).

This pattern is also documented in [11_remote-deploy.md](11_remote-deploy.md)
under the HPC-deployment section — the deploy-side reader gets the same
workaround without having to spelunk image-build docs first.
