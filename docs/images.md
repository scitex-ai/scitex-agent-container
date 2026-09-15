# Apptainer Images

## Builtin layers

The common base carries the supported agent programs; application layers add
domain dependencies on top:

| Tag       | What's inside                                                                                               | When                                   |
|-----------|-------------------------------------------------------------------------------------------------------------|----------------------------------------|
| `:base`   | Ubuntu 24.04 + dev tools, Node 22, pinned Cards source, pinned Hermes 0.21.2 and its official TUI | **Default** when `spec.image` is unset |
| `:scitex` | `FROM :base` + ffmpeg + portaudio + `scitex[all]` + claude-agent-sdk + sac itself                           | Optional heavier layer                 |

Hermes is a capability of `:base`, not a separate `:hermes` layer. The build
stages one immutable upstream commit and uses Hermes' frozen lockfile; the
recipe verifies both the installed version and official TUI artifact. Other
harnesses still have to be present in the selected image — an `openai` agent
needs the `openai-agents` SDK, and a `codex` agent needs `openai-codex` plus
its pinned CLI-binary wheel.

The runtime Cards client is installed from an immutable staged Git commit,
with its commit marker and #1003 DM/doorbell symbols checked during the build.
This is intentional when a required merged fix is newer than the latest PyPI
release: neither the local nor Spartan build path asks a live package index to
choose the Cards code that enters the image.

Recipes ship in the pip wheel — no need to clone the repo to run `sac image build`.
Built artifacts live under `~/.scitex/agent-container/containers/`, never in git.

```
<site-packages>/scitex_agent_container/containers/
  apptainer-{base,scitex}.def    ← canonical SSoT (`:base` includes Hermes)
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

Image builds have a stricter source-authority check than read-only commands.
When SAC is editable-installed from one checkout but ``PYTHONPATH`` would load
another, the console entry point stops before importing the build command or
creating its artifact directory. The error prints both observed roots. Unset
the stale override, or deliberately select the canonical editable checkout:

```bash
unset PYTHONPATH
# or, when an explicit source path is required:
PYTHONPATH=/path/to/canonical/checkout/src uv run sac image build base -y
```

## Sandbox / freeze workflow

Sandbox once, refresh when you want, freeze when stable:

```bash
sac image build scitex --sandbox        # one-time: writable sandbox
sac image update sandbox/               # any time: pip install --upgrade scitex[all]
sac image freeze sandbox/ scitex-2.28.15.sif   # bake to immutable SIF
sac image switch 2026-0914-152140 --layer base  # atomic dual-link flip
sac image rollback --layer base                  # restore previous version
sac image snapshot -o env.json         # full reproducibility capsule
```

Build, sandbox, and freeze delegate their container operations to
[`scitex-container`](https://github.com/ywatanabe1989/scitex-container).
Switch and rollback operate on SAC's layered `sac-<layer>-<version>.sif`
store and update both stable links together.

## Distributing one verified artifact to a fleet

`sac image distribute` is the explicit cross-host publication primitive. It
does not build an image, discover hosts, restart agents, or promote an authority
snapshot. Name the exact local SIF, its logical layer, and every target peer:

```bash
sac image distribute ./sac-base-2026-0914-120000.sif \
  --layer base \
  --host compute-01 \
  --host compute-02 \
  --receipt ./base-distribution.json
```

Each `--host` must resolve through the `peers:` block in SAC's `config.yaml`.
There is deliberately no `--all`: the target set must be reviewable in the
invocation. Use `--dry-run --json` to resolve the source, calculate its SHA-256
and byte count, validate the peers, and print the complete plan without opening
an SSH connection or changing a file.

The destination name is content-addressed with the complete digest, for
example `sac-base-sha256-<64 hex characters>.sif`. Distribution proceeds as a
fleet transaction:

1. Inspect and remember both live-link states on every host.
2. Stream to one explicit `.incoming-distribute-<transaction>.sif` per host;
   verify its size and SHA-256 there.
3. Atomically rename every verified temporary file, then verify every final
   artifact again.
4. Only after all hosts pass, atomically switch the inner and top-level live
   links on each host and verify the published links.
5. If activation fails part-way through, restore already-switched links to the
   states captured in step 1.

No old artifact is pruned. A mismatch therefore fails closed with current
links and rollback material preserved. The JSON output and optional atomically
written `--receipt` use schema `sac.image.distribution-receipt/v1` and include
status, phases, expected digest/size, verification result, prior/current links,
remote path, and any error for each host.

## Selecting an image

SAC-owned images use a portable logical name. Each host resolves the name
through its atomically switched live link; the incarnation birth certificate
records the exact resolved artifact path and SHA-256:

```yaml
spec:
  apptainer:
    image: sac-base
```

Timestamped paths such as `sac-base-2026-0912-140710.sif` are build artifacts,
not valid source-spec declarations. They are deliberately rejected because an
artifact built on one host may never have been distributed to another.

Audit and migrate an existing spec tree before deploying this schema:

```bash
sac agents migrate-images --root ~/.dotfiles/src/.scitex/agent-container/agents
sac agents migrate-images --root ~/.dotfiles/src/.scitex/agent-container/agents --apply
sac agents migrate-images --root ~/.dotfiles/src/.scitex/agent-container/agents --check
```

Dry-run is the default. `--apply` writes each changed spec atomically and
`--check` exits non-zero until the tree contains no managed filesystem-form
image references.

### Custom image

Set `spec.apptainer.image` in your `spec.yaml`:

```yaml
spec:
  apptainer:
    image: /srv/images/my-custom.sif
```

Or use a relative path (resolved relative to `spec.yaml`):

```yaml
spec:
  apptainer:
    image: ./my-custom.sif
```
