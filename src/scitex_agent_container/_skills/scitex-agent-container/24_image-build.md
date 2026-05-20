---
description: |
  [TOPIC] scitex-agent-container — agent image build & rebuild (apptainer .sif)
  [DETAILS] Where sac-base / sac-scitex .def + .sif live (dotfiles containers/), how sac is installed from git@develop, the `sac image build scitex` / `scitex-container build` commands, the @develop→@tag stable pin, and the gotcha that runner/channel changes do NOT reach running agents until the image is rebuilt.
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

```bash
# Preferred (versioned):
sac image build scitex            # or: scitex-container build scitex-agent-container-scitex
sac image build base              # rebuild the base layer first if it changed

# Raw apptainer (from the containers/ dir):
apptainer build sac-base.sif   sac-base.def
apptainer build sac-scitex.sif sac-scitex.def   # uses sac-base.sif as bootstrap
```

`sac-scitex.sif` is multi-GB; the build pulls `scitex[all]` (≈1–3 min with uv) plus
apt deps — budget several minutes. Restart agents after a rebuild so they pick up
the new `.sif`.

## Pinning a stable version (vs tracking @develop)

`@develop` means every rebuild tracks the tip of develop. For a reproducible
**stable** image, edit the def to pin a release tag, then rebuild:

```bash
# in sac-scitex.def: change @develop → @v0.17.3 (a released tag)
sac image build scitex -y
```

This is the concrete mechanism behind a "stable vs develop" SAC split: the agent
image = the pinned stable runner; bump it deliberately.
