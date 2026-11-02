#!/usr/bin/env bash
# Add release-only identity/runtime fences; retain the ordinary inner drivers.
set -euo pipefail
INNER="${1:?inner release driver required}"
V="${2:?Python minor required}"
case "$INNER" in run-in-sif.sh|build-in-sif.sh|publish-in-sif.sh) ;; *) exit 1 ;; esac
case "$V" in 3.11|3.12|3.13) ;; *) exit 1 ;; esac
: "${RELEASE_TAG:?}" "${RELEASE_COMMIT:?}"
CI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$CI_DIR/verify-release-runtime.sh"
sac_release_runtime
# Read-only source preflight runs before the old wrapper's PG/reap side effects.
# This is the verified image interpreter, not a new bare-runner dependency.
"$SCITEX_CI_APPTAINER" exec --pwd "$PWD" --bind "$PWD:$PWD" "$SCITEX_CI_SIF" \
    "/opt/venv-$V/bin/python" -I "$CI_DIR/release-identity.py" \
    check-source --tag "$RELEASE_TAG" --commit "$RELEASE_COMMIT"
# Only the old wrapper's newly provisioned private cluster may satisfy this.
unset SAC_TEST_PG_DSN APPTAINERENV_SAC_TEST_PG_DSN SINGULARITYENV_SAC_TEST_PG_DSN
export SAC_TEST_PG_REQUIRED=1 APPTAINERENV_SAC_TEST_PG_REQUIRED=1
exec bash "$CI_DIR/exec-in-sif.sh" "$INNER" "$V"
