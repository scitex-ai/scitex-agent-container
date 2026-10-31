#!/usr/bin/env bash
# Test fixture for the exit-code propagation contract: exits with the code
# in $1 so the suite can prove a nonzero inner-script exit travels through
# exec-in-sif.sh's `exec apptainer ...` handoff to the GitHub step instead
# of being swallowed into a green build.
set -u
code="${1:?usage: exit-code-probe.sh <exit-code>}"
exit "$code"
