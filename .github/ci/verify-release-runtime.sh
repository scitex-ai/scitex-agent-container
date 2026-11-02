#!/usr/bin/env bash
# Source-only helper; the release executor supplies the fixed reviewed pins.
sac_verified_file() {
    local candidate="$1" expected="$2" resolved before after digest
    [[ "$candidate" = /* && "$candidate" != *$'\n'* && "$expected" =~ ^[a-f0-9]{64}$ ]] || return 1
    resolved="$(/usr/bin/readlink -f -- "$candidate")" || return 1
    [[ -f "$resolved" && ! -L "$resolved" ]] || return 1
    before="$(/usr/bin/stat -c '%d:%i:%s:%f:%y:%z' -- "$resolved")" || return 1
    digest="$(/usr/bin/sha256sum -- "$resolved")" || return 1
    digest="${digest%% *}"
    after="$(/usr/bin/stat -c '%d:%i:%s:%f:%y:%z' -- "$resolved")" || return 1
    [[ "$before" = "$after" && "$digest" = "$expected" ]] || return 1
    printf '%s\n' "$resolved"
}

sac_release_runtime() {
    local image runtime pg
    image="${SCITEX_CI_SIF:?release image is required}"
    runtime="${SCITEX_CI_APPTAINER:?release Apptainer is required}"
    image="${image/#\~/$HOME}"
    runtime="${runtime/#\~/$HOME}"
    SCITEX_CI_SIF="$(sac_verified_file "$image" aa5836a6c317640d7e20f01eb79aa7385b3dca2f369b595065c50ba3dd34a7d5)" || {
        echo '::error::release image differs from the qualified AA583 bytes' >&2
        return 1
    }
    SCITEX_CI_APPTAINER="$(sac_verified_file "$runtime" 7bdb501fdddbb7264b4e4d6038e0ec27201f63e217519e5f7f0db950c9285ddd)" || {
        echo '::error::release Apptainer differs from the qualified runtime bytes' >&2
        return 1
    }
    test -x "$SCITEX_CI_APPTAINER" || return 1
    pg="${SAC_RELEASE_PG_SIF:-$HOME/.scitex/pg/runtime/postgres18-sha256-842a1fcf5abdc512af312c1527a0700c52e4b1a6b6e3e3dddc6edde69113ee23.sif}"
    SAC_RELEASE_PG_SIF="$(sac_verified_file "$pg" 842a1fcf5abdc512af312c1527a0700c52e4b1a6b6e3e3dddc6edde69113ee23)" || {
        echo '::error::release PostgreSQL image differs from the qualified PG18.6 bytes' >&2
        return 1
    }
    export SCITEX_CI_SIF SCITEX_CI_APPTAINER SAC_RELEASE_PG_SIF
}
