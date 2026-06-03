# Apptainer Images

## Builtin layers

Two patterns:

* **Stacked layers** — primary runtimes; `:scitex` is `FROM :base`.
* **Sub-tool layers** — independent SIFs mounted INTO a wrapper agent's
  runtime SIF as read-only sub-tools (at `/opt/<tool>.sif:ro`). Used for
  heavy, slow-moving toolchains that don't belong on the primary
  runtime — e.g. TeX Live, which would otherwise add 1-2 GB to every
  agent SIF.

| Tag        | Pattern  | What's inside                                                                                               | When                                                                              |
|------------|----------|-------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------|
| `:base`    | stacked  | Ubuntu 24.04 + dev tools (git, gh, rust CLIs, mermaid, prettier, eslint, jsonlint, uv, pipx, tree, node 20) | **Default** when `spec.image` is unset                                            |
| `:scitex`  | stacked  | `FROM :base` + ffmpeg + portaudio + `scitex[all]` + claude-agent-sdk + sac itself                           | Optional heavier layer                                                            |
| `:texlive` | sub-tool | Ubuntu 22.04 + TeX Live scheme-medium + extras + `latexmk` / `latexdiff` / `chktex` / `qpdf` / Ghostscript  | Wrapper agents mount this at `/opt/texlive.sif` (see `docs/containers/texlive/`)  |

Recipes ship in the pip wheel — no need to clone the repo to run `sac image build`.
Built artifacts live under `~/.scitex/agent-container/containers/`, never in git.

```
<site-packages>/scitex_agent_container/containers/
  apptainer-{base,scitex,texlive}.def    ← canonical SSoT
```

## Build

```bash
sac image build           # :base SIF (default; OS + dev tools, ~15-25 min)
sac image build scitex    # :scitex SIF (FROM :base + scitex[all], ~10-20 min)
sac image build texlive   # :texlive sub-tool SIF (LaTeX, ~10-15 min)
sac image build --sandbox # writable sandbox dir instead of frozen SIF
```

## Sub-tool layers

A sub-tool SIF is independent of `:base` and `:scitex` — it has its own
`Bootstrap:` line (typically `docker / From: <upstream>`), and rebuilding
`:base` or `:scitex` does not invalidate it.

Wrapper agents integrate a sub-tool via two `spec.yaml` fragments:

```yaml
spec:
  apptainer:
    binds:
      - $HOME/.scitex/agent-container/containers/sac-texlive/sac-texlive.sif:/opt/texlive.sif:ro
    raw_args:
      - --env
      - STXW_TEXLIVE_APPTAINER_SIF=/opt/texlive.sif
```

The wrapper's compile script honours the `STXW_TEXLIVE_APPTAINER_SIF`
env and shells out via `apptainer exec` into the mounted sub-tool for
the actual `pdflatex` / `bibtex` / `latexmk` work. The bind preflight
(SAC-from-SAC PR-1, `#287`) catches a missing sub-tool SIF at `POST
/agents` with HTTP 400 `kind=bind_unresolvable` — no silent FATAL on
the underlying apptainer mount.

Per-tool integration templates live under `docs/containers/<tool>/`:

* `docs/containers/texlive/README.md` — what's in the LaTeX SIF
* `docs/containers/texlive/spec.yaml.template` — wrapper integration fragment

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
