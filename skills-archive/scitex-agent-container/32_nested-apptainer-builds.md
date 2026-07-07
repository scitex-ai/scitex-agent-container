---
description: |
  [TOPIC] Nested apptainer builds — `spec.apptainer.nested_build`
  [DETAILS] Let a solver agent build/pull a research capsule's PINNED
  environment from INSIDE its own SAC apptainer container (pull a CodeOcean
  published image, or build a Dockerfile-derived def whose %post runs as
  root), then exec it — so the agent reproduces real outputs to ground its
  claims instead of fabricating. The recipe, the knob, the hard limit
  (docker is impossible), and how it pairs with the clew verify gate.
tags: [scitex-agent-container-nested-apptainer-builds, nested-apptainer, nested-build]
---

# Nested apptainer builds (`spec.apptainer.nested_build`)

A SAC agent already runs inside an apptainer SIF. With
`spec.apptainer.nested_build: true` it can run **nested** `apptainer
build`/`pull`+`exec` to reproduce a capsule's pinned environment ITSELF —
the alternative to the harness pre-building it (babysitting + overfit) and
to the agent fabricating when the env won't run in the default interpreter.

## What the knob grants (verified 2026-06-20 in `sac-scitex.sif`)

`nested_build_flags` (`runtimes/_apptainer_nested.py`) adds, at the
`build_run_argv` seam:

- `--bind /dev/fuse` — `--containall` omits it; the squashfuse mount of a
  pulled/built SIF needs it (fail-loud if the host lacks `/dev/fuse`).
- empty-file masks over `/etc/subuid` + `/etc/subgid` — the SIF's
  `newuidmap` is `agent`-owned (not root), so plain `--fakeroot` FATALs
  ("newuidmap must be owned by the root user"). Masking subuid drops the
  user out of `/etc/subuid`, which makes apptainer fall back to the
  **root-mapped namespace + `fakeroot` command** path — no setuid
  `newuidmap` needed. `%post`/`RUN` steps then run as (faked) root.
- `APPTAINER_TMPDIR=/tmp` + `APPTAINER_CACHEDIR=/tmp/.apptainer-cache` —
  build scratch + OCI cache on the real-disk `/tmp` (relocated there by the
  `tmpfs_size` `--workdir`). Size `tmpfs_size` up (the 2G default is too
  small for a multi-GB image).

It adds **no host-FS bind** → composes with `access: capsule` (leak-safety
preserved). It is **infrastructure → give it to BOTH arms**; the *treatment*
is the scitexification skill + clew gate, not the capability.

## The agent recipe

```bash
export APPTAINER_TMPDIR=/tmp APPTAINER_CACHEDIR=/tmp/.apptainer-cache
# (a) pull a pre-built PUBLISHED image (preferred; CodeOcean capsules name one
#     in input/REPRODUCING.md, anonymously pullable — no %post, rootless):
apptainer build env.sif docker://registry.codeocean.com/published/<uuid>:v1
# (b) OR build the capsule's Dockerfile (convert to a def: `Bootstrap: docker`
#     + `%post` with the RUN/conda/pip lines — runs as fakeroot):
apptainer build env.sif env.def
# run the REAL code, read REAL outputs:
apptainer exec --bind <data>:/data --bind <code>:/code --bind "$PWD/results":/results \
  env.sif bash -lc 'cd /code && bash run'
```

## Hard limit — no Docker

You **cannot** run Docker nested: there is no Docker daemon inside the
unprivileged container, and none is grantable (it needs root + cgroup/iptables
control). It is also unnecessary — `apptainer build docker://…` pulls the same
OCI layers with no daemon. Build-from-Dockerfile needs the base image to carry
`/etc/subuid` (every real distro base — debian/ubuntu/miniconda/python — does;
busybox does not).

## Pairs with the clew verify gate

`nested_build` is the *capability*; the honest-grounding **gate** (a `Stop`
hook running `clew verify --strict`, env-gated by `SCITEX_CLEW_VERIFY_GATE`)
is what *forces* the agent to use it: a fabricated value has no
`@stx.session` lineage → `NO_LINEAGE` → `DONE` refused → reproduce (via the
recipe above) or abstain (`null` + reason). See the `scitex-clew`
(`21_agentic-reasoning`) and `scitexification` (`04_repro-clew`, Stage 4.0)
skills. Field reference: [`spec-reference.md`](../../../docs/spec-reference.md)
(`spec.apptainer.nested_build`).
