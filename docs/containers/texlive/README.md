# `:texlive` — Apptainer LaTeX sub-tool SIF

Self-contained Apptainer LaTeX environment delivered as an
independent, read-only SIF. Wrapper agents (`scitex-writer`,
`proj-neurovista`, ...) mount this SIF into their primary runtime SIF
at `/opt/texlive.sif` and exec-shell into it for the actual `pdflatex`
/ `bibtex` / `latexmk` calls. The wrapper's compile script honours
`STXW_TEXLIVE_APPTAINER_SIF=/opt/texlive.sif` and invokes
`apptainer exec` against the mounted sub-tool.

This pattern (sub-tool SIF) is distinct from the stacked-layer
pattern (`:base` → `:scitex`). The sub-tool SIF is independent — it
does not `FROM ./sac-base.sif`, and rebuilding `:base` or `:scitex`
does not invalidate it.

## What's in it

| Decision | Final | Rationale |
|----------|-------|-----------|
| Distribution | TeX Live `scheme-medium` + explicit extras | medium covers most; explicit list ensures `elsarticle`, `pdfpages`, `accsupp`, ... resolve |
| Base image | `ubuntu:22.04` | Matches `texlive/texlive:latest` compatibility |
| Bibliography | `natbib` + `bibtex` (NO `biber` / `biblatex`) | Current downstream manuscripts (neurovista) use `natbib` |
| Figures | None inside this SIF | Pre-baked PDFs generated externally by `figrecipe` |
| Fonts | Times (TL Type 1) + `fonts-liberation` + `fonts-noto-*` + `fonts-firacode` | Arial-compat for figrecipe SCITEX style match |
| Tools | `pdflatex`, `xelatex`, `lualatex`, `bibtex`, `latexmk`, `latexdiff`, `texcount`, `chktex`, `parallel`, `ghostscript`, `qpdf`, `poppler-utils` | covers compile + diff + post-processing |
| SAC integration | sub-tool pattern (bound at `/opt/texlive.sif:ro`) | wrapper agent owns runtime; texlive is just LaTeX |

## TeX packages verified in `%test`

`kpsewhich` runs against every entry below on every build; a missing
one trips CI red and the SIF doesn't ship:

```
elsarticle.cls
natbib.sty
booktabs.sty longtable.sty tabularx.sty xltabular.sty
colortbl.sty xcolor.sty csvsimple.sty makecell.sty
amsmath.sty amssymb.sty siunitx.sty
graphicx.sty tikz.sty pgfplots.sty pgfplotstable.sty
hyperref.sty xr-hyper.sty
lineno.sty caption.sty geometry.sty indentfirst.sty pdflscape.sty
bashful.sty lipsum.sty tcolorbox.sty
pdfpages.sty accsupp.sty
```

## Build

Canonical, versioned via `sac`:

```bash
sac image build texlive -y
# -> ~/.scitex/agent-container/containers/sac-texlive/sac-texlive.sif
```

Raw apptainer (what `sac` shells out to):

```bash
apptainer build sac-texlive.sif apptainer-texlive.def
```

Verify (runs all 12 commands + 24 TeX packages):

```bash
apptainer test ~/.scitex/agent-container/containers/sac-texlive/sac-texlive.sif
```

## Wrapper integration

Drop the fragments from [`spec.yaml.template`](./spec.yaml.template)
into the wrapper agent's `spec.yaml` under `apptainer.binds` and
`apptainer.raw_args`. Once integrated, the wrapper's compile script
shells out to `apptainer exec $STXW_TEXLIVE_APPTAINER_SIF latexmk
-pdf manuscript.tex`.

Bind preflight (SAC PR-1, `#287`) catches a missing `sac-texlive.sif`
at `POST /agents` time with HTTP 400 `kind=bind_unresolvable` — no
silent FATAL on the underlying apptainer mount.

## Standalone use

Outside the sub-tool integration pattern, the SIF works as a regular
Apptainer container:

```bash
apptainer exec --bind $(pwd):/work \
    ~/.scitex/agent-container/containers/sac-texlive/sac-texlive.sif \
    bash -c "cd /work && latexmk -pdf manuscript.tex"
```

## See also

- `src/scitex_agent_container/containers/apptainer-texlive.def` — canonical SSoT recipe
- [`spec.yaml.template`](./spec.yaml.template) — wrapper integration fragment
- `docs/images.md` — `sac image` lifecycle (build / sandbox / freeze / list / status)
- ADR-0005 (`docs/adr/0005-sif-mode-migration.md`) — SIF mode rationale
