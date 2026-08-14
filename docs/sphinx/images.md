# Apptainer Images

## Builtin layers

Four `.def` recipes, chained — each builds `FROM` the one above it:

| # | Tag            | What's inside                                                                                              | When                                   |
|---|----------------|------------------------------------------------------------------------------------------------------------|----------------------------------------|
| 1 | `:system-deps` | Ubuntu 24.04 + OS tooling: git, gh, node 20 + npm globals, rust toolchain, apptainer, and the pinned static binaries (yq, gdu, tree, rtk) | Rebuild when apt/node/rust changes     |
| 2 | `:python-pkgs` | `FROM :system-deps` + uv/pipx/pre-commit + `/opt/venv-sac` carrying claude-agent-sdk, scitex-cards and sac `[all,dev]` | Rebuild when a Python pin moves        |
| 3 | `:base`        | `FROM :python-pkgs` + the baked `sac versions` manifest                                                     | **Default** when `spec.image` is unset |
| 4 | `:scitex`      | `FROM :base` + ffmpeg + portaudio + `scitex[all]`                                                           | Optional heavier layer                 |

`:base` still contains everything it always did — the split moved *where*
things install, not what a `:base` container carries.

Layers 1 and 2 are split because they have very different rebuild
frequencies: the OS floor is most of the bake wall-clock and changes monthly,
while the Python pin set above it changes weekly. Fused, every pin bump
re-paid the whole apt/cargo cost.

`:proxy` also ships a recipe but is **not** in the chain — it is a standalone
sidecar built straight from the registry.

Recipes ship in the pip wheel — no need to clone the repo to run `sac image build`.
Built artifacts live under `~/.scitex/agent-container/containers/`, never in git.

```
<site-packages>/scitex_agent_container/containers/
  apptainer-{system-deps,python-pkgs,base,scitex,proxy}.def   ← canonical SSoT
```

## Build

Build bottom-up the first time. Each layer FAILS LOUD (before invoking
apptainer) if its prerequisite SIF is missing, naming the immediate parent
and the exact command to build it:

```bash
sac image build system-deps -y  # 1: OS + apt + node + rust + static bins
sac image build python-pkgs -y  # 2: /opt/venv-sac + claude-agent-sdk + sac
sac image build -y              # 3: :base (default) — bakes the versions manifest
sac image build scitex -y       # 4: FROM :base + scitex[all]
sac image build --sandbox       # writable sandbox dir instead of frozen SIF
```

After the first pass, rebuild only the layer you changed — everything below
it is reused untouched.

## Sandbox / freeze workflow

Sandbox once, refresh when you want, freeze when stable:

```bash
sac image build scitex --sandbox        # one-time: writable sandbox
sac image update sandbox/               # any time: pip install --upgrade scitex[all]
sac image freeze sandbox/ scitex-2.28.15.sif   # bake to immutable SIF
sac image switch 2.28.15               # atomic flip (previous remembered)
sac image rollback                     # restore previous version
sac image snapshot -o env.json         # full reproducibility capsule
```

The build / sandbox / version / rollback verbs all delegate to
[`scitex-container`](https://github.com/ywatanabe1989/scitex-container).

## Pinning a custom image

Set `spec.apptainer.image` in your `spec.yaml`:

```yaml
spec:
  apptainer:
    image: ~/.scitex/agent-container/containers/sac-base/sac-base.sif
```

Or use a relative path (resolved relative to `spec.yaml`):

```yaml
spec:
  apptainer:
    image: ./my-custom.sif
```
