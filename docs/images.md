# Apptainer Images

## Builtin layers

Two `.def` recipes, layered:

| Tag       | What's inside                                                                                               | When                                   |
|-----------|-------------------------------------------------------------------------------------------------------------|----------------------------------------|
| `:base`   | Ubuntu 24.04 + dev tools (git, gh, rust CLIs, mermaid, prettier, eslint, jsonlint, uv, pipx, tree, node 20) | **Default** when `spec.image` is unset |
| `:scitex` | `FROM :base` + ffmpeg + portaudio + `scitex[all]` + claude-agent-sdk + sac itself                           | Optional heavier layer                 |

Recipes ship in the pip wheel — no need to clone the repo to run `sac image build`.
Built artifacts live under `~/.scitex/agent-container/containers/`, never in git.

```
<site-packages>/scitex_agent_container/containers/
  apptainer-{base,scitex}.def    ← canonical SSoT
```

## Cross-package convention: `~/.scitex/<pkg>/{containers,bin}`

Sac owns `~/.scitex/agent-container/` for its own (base / scitex) artifacts.
Other scitex-* packages own their own siblings under `~/.scitex/<pkg>/`:

```
~/.scitex/
├── agent-container/containers/    ← sac's own SIFs (base, scitex, ...)
├── writer/containers/             ← scitex-writer's SIFs (texlive, mermaid, ...)
└── <pkg>/containers/              ← any future package follows the same shape
```

`sac image list` scans `~/.scitex/*/containers/*.sif` generically — sac
does **not** know any other package by name, and new packages light up
automatically with no sac code change. Each owning package ships its
own installer CLI (e.g. `scitex-writer install texlive-sif`) that drops
its SIF at the conventional path. Wrapper agents bind from there
(`~/.scitex/writer/containers/texlive.sif:/opt/texlive.sif:ro`).

This mirrors the `scitex_dev.*` entry_points pattern already in use
across the ecosystem (`scitex_dev.docs`, `scitex_dev.skills`,
`scitex_dev.linter.plugins`, `scitex_dev.jobs`): each package owns its
own surface, the aggregator never hard-codes downstream names. Per
operator design 8566; ecosystem doctrine of minimal scope.

## Build

```bash
sac image build           # :base SIF (default; OS + dev tools, ~15-25 min)
sac image build scitex    # :scitex SIF (FROM :base + scitex[all], ~10-20 min)
sac image build --sandbox # writable sandbox dir instead of frozen SIF
```

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
