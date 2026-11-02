#!/usr/bin/env bash
# Source before any release Python/installer: ambient stores are never test input.
sac_release_store_floor() {
    local root="${1:?owned CI scratch root required}" name value prefix pass_dir pass_file
    case "$root" in /*) ;; *) return 1 ;; esac
    [ "$root" != / ] || return 1
    mkdir -p -- "$root"
    [ "$(/usr/bin/readlink -f -- "$root")" = "$root" ] || return 1
    [ "$(/usr/bin/stat -c %u -- "$root")" = "$EUID" ] || return 1
    mkdir -p -- "$root/postgres"
    [ ! -L "$root/postgres" ] || return 1
    [ "$(/usr/bin/stat -c %u -- "$root/postgres")" = "$EUID" ] || return 1
    pass_dir="$(umask 077; mktemp -d "$root/postgres/release-env.XXXXXXXX")" || return 1
    pass_file="$pass_dir/empty.pgpass"
    (umask 077; : > "$pass_file") || return 1
    # Remove even aliases whose precedence would otherwise defeat host unsets.
    while IFS= read -r name; do
        case "$name" in
        PG*|APPTAINERENV_PG*|SINGULARITYENV_PG*|SCITEX_STORE_DSN|APPTAINERENV_SCITEX_STORE_DSN|SINGULARITYENV_SCITEX_STORE_DSN|SAC_TEST_PG_DSN|APPTAINERENV_SAC_TEST_PG_DSN|SINGULARITYENV_SAC_TEST_PG_DSN)
            unset "$name" || return 1 ;;
        esac
    done < <(compgen -e)
    # Explicit unavailable defaults also override qualified image defaults.
    # The old executor later injects the proven private fixture DSN + identity.
    while IFS='=' read -r name value; do
        for prefix in '' APPTAINERENV_; do
            export "${prefix}${name}=$value"
        done
    done <<EOF
SCITEX_STORE_DSN=postgresql://sac_tests@127.0.0.1:1/tests_must_not_write_to_the_fleet_store
PGHOST=127.0.0.1
PGHOSTADDR=
PGPORT=1
PGDATABASE=tests_must_not_write_to_the_fleet_store
PGUSER=sac_tests
PGPASSFILE=$pass_file
PGOPTIONS=
PGPASSWORD=
PGSSLMODE=disable
PGGSSENCMODE=disable
PGTARGETSESSIONATTRS=any
EOF
}
