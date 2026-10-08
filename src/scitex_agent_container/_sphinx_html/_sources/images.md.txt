# Apptainer Images

## Builtin layers

Two `.def` recipes, layered:

| Tag       | What's inside                                                                                               | When                                   |
|-----------|-------------------------------------------------------------------------------------------------------------|----------------------------------------|
| `:base`   | Ubuntu 24.04 + dev tools (git, gh, rust CLIs, mermaid, prettier, eslint, jsonlint, uv, pipx, tree, node 20) | **Default** when `spec.image` is unset |
| `:scitex` | `FROM :base` + ffmpeg + portaudio + `scitex[all]` + claude-agent-sdk + sac itself                           | Optional heavier layer                 |

**Neither layer is harness-complete on its own.** `:base` ships no agent
SDK at all; `:scitex` adds `claude-agent-sdk` only. Whichever harness your
specs select has to be present in the image you point them at — an
`openai` agent needs the `openai-agents` SDK on top, a `codex` agent needs
`openai-codex` plus its ~285 MB pinned CLI-binary wheel.

Recipes ship in the pip wheel — no need to clone the repo to run `sac image build`.
Built artifacts live under `~/.scitex/agent-container/containers/`, never in git.

```
<site-packages>/scitex_agent_container/containers/
  apptainer-{base,scitex}.def    ← canonical SSoT
```

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
sac image freeze sandbox/ candidate.sif # bake an explicit immutable SIF

# Managed timestamped artifacts already under the SAC layer store:
sac image switch 2026-0914-152140 --layer base  # atomic dual-link flip
sac image rollback --layer base                  # restore previous version
sac image snapshot -o env.json         # full reproducibility capsule
```

Build, sandbox, and freeze delegate their container operations to
[`scitex-container`](https://github.com/ywatanabe1989/scitex-container).
Switch and rollback operate on SAC's layered `sac-<layer>-<version>.sif`
store and update both stable links together.

## Distributing one verified artifact to a fleet

The cross-host publication command requires an exact local SIF, a logical
layer, and an explicit list of peers from SAC's ``config.yaml``:

```bash
sac image distribute ./sac-base-2026-0914-120000.sif \
  --layer base --host compute-01 --host compute-02 \
  --receipt ./base-distribution.json
```

There is no ``--all``. ``--dry-run --json`` hashes the resolved source and
prints the complete plan without opening SSH connections. On a real run, each
host receives a content-addressed ``sac-base-sha256-<sha>.sif`` through an
explicit temporary path. Size and SHA-256 are checked before atomic rename and
again at the final path on every host. Only then are the live links switched.
If a link switch fails part-way through, already-switched hosts are restored to
their preflight link states. Old artifacts are never pruned, and the structured
``sac.image.distribution-receipt/v1`` output records every host's evidence.

## Pinning a custom image

Set `spec.apptainer.image` in your `spec.yaml`:

```yaml
spec:
  apptainer:
    image: sac-base
```

Or use a relative path (resolved relative to `spec.yaml`):

```yaml
spec:
  apptainer:
    image: ./my-custom.sif
```
