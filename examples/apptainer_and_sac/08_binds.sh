#!/usr/bin/env bash
# Lesson 08 — Bind mounts (apptainer's --bind).
#
# Pure apptainer:
#   apptainer exec --bind /host:/container my.sif ...
#   apptainer exec --bind /host:/container:ro my.sif ...        # read-only
#   apptainer exec --bind $PWD --bind /scratch my.sif ...       # multi-bind
#
# Defaults you don't see:
#   apptainer auto-binds $HOME, /tmp, $PWD, /sys, /dev, /proc.
#   To opt out: --no-home, --contain (strict isolation), --containall.
#
# Why this matters on HPC:
#   The auto-binds make apptainer "just work" with the user's files
#   and scratch dirs. Most HPC sites also pre-configure system binds
#   like /scratch, /project — see /etc/apptainer/apptainer.conf.
#
# sac equivalent — declared in spec.yaml:
#
#   spec:
#     mounts:
#       - src: ${HOME}/proj
#         dst: ${HOME}/proj
#         mode: rw
#       - src: /scratch/${USER}
#         dst: /scratch/${USER}
#         mode: rw
#
# sac's mounts list is runtime-agnostic — the same yaml works whether
# the agent runs under apptainer or docker.
set -euo pipefail

echo "── apptainer --bind examples (not run) ──"
# shellcheck disable=SC2016
echo '$ apptainer exec --bind "$PWD":/work scitex-agent-container-scitex.sif python -V'
# shellcheck disable=SC2016
echo '$ apptainer exec --bind "$HOME":"$HOME":ro scitex-agent-container-scitex.sif ...'

echo
echo "── sac equivalent (spec.yaml fragment, same for both runtimes) ──"
cat <<'YAML'
spec:
  runtime: apptainer
  mounts:
    - src: ${HOME}
      dst: ${HOME}
      mode: rw
    - src: /scratch/${USER}
      dst: /scratch/${USER}
      mode: rw
YAML
