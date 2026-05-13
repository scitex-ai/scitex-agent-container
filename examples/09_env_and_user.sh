#!/usr/bin/env bash
# Lesson 09 — Environment variables and the run-as user.
#
# Pure apptainer:
#   apptainer exec --env FOO=bar my.sif ...
#   apptainer exec --env-file ./vars.env my.sif ...
#   apptainer exec --no-home my.sif ...                     # don't auto-bind $HOME
#   apptainer exec --cleanenv my.sif ...                    # nuke host env
#
# About the user:
#   Apptainer ALWAYS runs as you. There is no -u flag.
#   Files written inside bind-mounted dirs land owned by you.
#   This is the single biggest difference from docker on HPC —
#   no root-owned-output footguns.
#
# sac equivalent — engine-scoped under spec.apptainer.env in v3:
#
#   spec:
#     runtime: apptainer
#     # spec.user is irrelevant for apptainer — it's always the host user.
#     apptainer:
#       env:
#         FOO: bar
#         API_KEY: ${API_KEY}
#         HOME: ${HOME}
#
# Tip:
#   Apptainer auto-forwards every host env var by default. To get
#   docker-like isolation use --cleanenv or sac's apptainer.env block
#   (which only forwards what's listed).
set -euo pipefail

echo "── apptainer --env / --cleanenv examples (not run) ──"
# shellcheck disable=SC2016
echo '$ apptainer exec --env FOO=bar sac-scitex.sif env | grep FOO'
# shellcheck disable=SC2016
echo '$ apptainer exec --cleanenv --env API_KEY="$API_KEY" sac-scitex.sif ...'

echo
echo "── sac equivalent (spec.yaml fragment, v3 schema) ──"
cat <<'YAML'
spec:
  runtime: apptainer
  apptainer:
    env:
      FOO: bar
      API_KEY: ${API_KEY}
      HOME: ${HOME}
YAML
