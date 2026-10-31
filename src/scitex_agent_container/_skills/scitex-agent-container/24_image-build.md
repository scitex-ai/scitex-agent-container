---
description: |
  [TOPIC] Build and deliver SAC Apptainer images
  [DETAILS] Build from the installed package source and immutable Hermes pin, verify the timestamped SIF before distribution, and restart consumers through the lifecycle preflight.
tags: [scitex-agent-container-image-build]
---

# Image builds

Use when a runtime change must reach the code inside Apptainer. A host package
upgrade alone does not replace the Python code baked into an existing SIF.

## Source authority

Recipes ship in `src/scitex_agent_container/containers/`. The CLI selects
`apptainer-base.def`, `apptainer-scitex.def`, or `apptainer-proxy.def` through
`cli_pkg/image_group.py`; editing an unrelated host recipe does not change this
selection.

`cli_pkg/_image_source_build.py` checks package provenance and stages a copy of
the installed SAC source beside the recipe. `%files` copies this source into the
image, where it is installed locally. The build does not silently substitute
GitHub's current `develop` tip for the selected SAC source.

Hermes is a separate staged package. `cli_pkg/_hermes_source.py` resolves
`HERMES_COMMIT`, an immutable full Git commit, from `HERMES_REPOSITORY`.
`SAC_HERMES_SOURCE_DIR` may provide a local source repository, but it must contain
the declared commit. The recipe checks the pin before installation. Keep the
pin consistent with `[tool.scitex-agent-container.hermes-harness]` in
`pyproject.toml`, the recipe assertion/label, and `spartan-sif-bake.sh`.

## Build

```bash
sac image build --help
sac image build base --dry-run
sac image build base --yes
# The scitex layer requires an existing base image.
sac image build scitex --yes
```

Artifacts live under the configured SAC containers directory. The CLI creates
`<containers>/sac-<layer>/build-context/` for staged inputs and delegates to
scitex-container for the build. A successful build produces a timestamped SIF
and atomically updates the stable boot symlink; a failed build preserves the
previous image. Inspect the resolved symlink and resulting image, rather than
assuming a build succeeded from the command's launch message.

Builds use low CPU/IO priority by default. `--no-nice` disables that demotion;
use it only on a host assigned for builds. Check disk capacity on the build and
recipient hosts before transferring multi-GB images. Keep large artifacts and
runtime state outside the configuration Git repository, using configured paths
or symlinks.

`--reproducible` captures the package versions, writes a pinned recipe, rebuilds,
and compares environment identity. This is not a claim of byte-identical SIF
files. `--skip-verify` captures inputs without proving the second build.

## Verify and distribute

1. Verify the built SAC version and expected Hermes source inside the actual
   SIF without overlay overrides. Use the recipe's symbol/pin checks and the
   build log; compare expected source hashes when deploying a runtime repair.
2. Select recipient hosts through scitex-dev's `24_host-registry` skill and
   check their free space. Use the configured SSH route and image destination,
   never a copied inventory or an assumed default path.
3. Transfer to a temporary destination, verify the checksum, then publish the
   versioned image and update boot symlinks atomically. Preserve the prior image
   for rollback; do not overwrite an image used by live processes in place.
4. Upgrade host SAC code and restart active listen services so host controls
   use the same release. Restart only the selected running consumers through
   SAC's normal successor preflight. Preserve explicit operator exclusions.
5. Verify the new processes use the intended image and home backing. For Hermes,
   check main and judge access, then run the isolated Say Hi acceptance check
   from `48_model-availability.md` without replacing standing goals.

An interim directory-overlay code repair can mask a subsequently rebuilt base
image. Remove only the exact previously deployed override after verifying that
the new image contains the tested fix; retain backups and receipts. Do not
remove unrelated overlay contents or change the active home backing.

## When builds are unavailable

Check actual Apptainer/user-namespace capabilities. A missing build privilege
is a deployment limitation, not evidence that the host's package upgrade has
changed the image. An explicit package-source bind or a narrowly verified
overlay repair can be a temporary deployment mechanism, but dependency and
Python-version compatibility must be checked against the image. Record the
interim mechanism and remove it after a qualified image replaces it.

Source evidence: `cli_pkg/image_group.py`, `cli_pkg/_image_source_build.py`,
`cli_pkg/_image_repro_build.py`, `cli_pkg/_hermes_source.py`, and
`containers/apptainer-base.def`. Pin/provenance regressions live in
`tests/integration/test_apptainer_base_def_hermes.py` and the image-build tests.
