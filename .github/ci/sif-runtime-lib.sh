#!/usr/bin/env bash
# Source-only CI runtime helpers. No account, history or production Store access.
. "$(dirname "${BASH_SOURCE[0]}")/tmpdir-lib.sh"

ci_sif_verify() {
    local image="${1:-}" digest="${2:-}" actual
    if [ "${#digest}" -ne 64 ] || [[ "$digest" == *[!0-9a-f]* ]]; then
        echo '::error::a lowercase full SHA256 is required for the selected SIF' >&2
        return 1
    fi
    if [ ! -f "$image" ] || [ -L "$image" ]; then
        echo "::error::selected SIF is not a regular non-symlink file: $image" >&2
        return 1
    fi
    actual="$(sha256sum -- "$image")" || return 1
    [ "${actual%% *}" = "$digest" ] || {
        echo '::error::selected SIF SHA256 mismatch' >&2; return 1;
    }
}

ci_pid_birth() {
    local stat rest
    case "${1:-}" in ''|*[!0-9]*) return 1 ;; esac
    IFS= read -r stat < "/proc/$1/stat" || return 1
    rest="${stat##*) }"
    set -- $rest
    printf '%s' "${20}"
}

ci_group_alive() { _ci_tmpdir_group_alive "${1:?group required}"; }

ci_group_birth_record() {
    local pid="${1:?PID required}" file="${2:?record required}" i observed birth extra metadata
    # The same owned group leader writes this before launching work. Reading
    # that proof also preserves very fast child exits, after /proc has gone.
    for ((i=0; i<100; i++)); do
        if [ -f "$file" ] && [ ! -L "$file" ]; then
            metadata="$(stat -c '%u:%a' -- "$file")" || return 1
            [ "$metadata" = "$(id -u):600" ] || return 1
            if IFS='|' read -r observed birth extra < "$file"; then
                if [ "$observed" = "$pid" ] && [ -z "$extra" ]; then
                    case "$birth" in ''|*[!0-9]*) ;; *) printf '%s' "$birth"; return 0 ;; esac
                fi
            fi
        fi
        sleep 0.01
    done
    echo '::error::owned child group birth was not published' >&2
    return 1
}

ci_stop_group() {
    local pid="${1:-}" birth="${2:-}" i current
    [ -n "$pid" ] || return 0
    current="$(ci_pid_birth "$pid" 2>/dev/null)" || current=''
    if [ "$current" = "$birth" ] && [ -n "$birth" ]; then
        kill -TERM -- "-$pid" 2>/dev/null || true
        for ((i=0; i<40; i++)); do
            current="$(ci_pid_birth "$pid" 2>/dev/null)" || current=''
            [ "$current" = "$birth" ] || break
            sleep 0.05
        done
        current="$(ci_pid_birth "$pid" 2>/dev/null)" || current=''
        if [ "$current" = "$birth" ]; then
            kill -KILL -- "-$pid" 2>/dev/null || true
        fi
    fi
    wait "$pid" 2>/dev/null || true
    # A vanished leader does not authorize killing a reused/foreign group.
    if ci_group_alive "$pid"; then
        echo '::warning::owned child group is not quiescent; scratch retained' >&2
        return 1
    fi
}

ci_runtime_env() {
    CI_ENV=("PATH=/usr/local/bin:/usr/bin:/bin" "HOME=$CI_HOME"
        "LANG=C.UTF-8" "LC_ALL=C.UTF-8" "TZ=Asia/Tokyo"
        "APPTAINER_TMPDIR=$APPTAINER_TMPDIR" "APPTAINER_CACHEDIR=$CI_HOME/apptainer-cache")
    local key
    # Data required by the supported drivers, not arbitrary host environment.
    for key in GITHUB_ACTIONS GITHUB_RUN_ID GITHUB_RUN_ATTEMPT GITHUB_JOB GITHUB_SHA \
        GITHUB_REF GITHUB_REF_NAME GITHUB_REF_TYPE GITHUB_EVENT_NAME GITHUB_REPOSITORY \
        CI_XDIST_WORKERS SLURM_CPUS_PER_TASK OMP_NUM_THREADS OMP_THREAD_LIMIT; do
        if [ "${!key+x}" ]; then CI_ENV+=("APPTAINERENV_$key=${!key}"); fi
    done
    # Publish alone needs the original job-scoped OIDC endpoint, held in RAM.
    if [ "$INNER" = publish-in-sif.sh ]; then
        for key in ACTIONS_ID_TOKEN_REQUEST_TOKEN ACTIONS_ID_TOKEN_REQUEST_URL; do
            if [ "${!key+x}" ]; then CI_ENV+=("APPTAINERENV_$key=${!key}"); fi
        done
    fi
    CI_ENV+=("APPTAINERENV_SAC_CI_TMPDIR_ROOT=$SAC_CI_TMPDIR_ROOT"
        "APPTAINERENV_SAC_CI_OWNER_PID=$$"
        "APPTAINERENV_SAC_CI_OWNER_START=$(ci_pid_birth "$$")")
}

ci_clean_exec() {
    local key entry
    # Builtins keep the job's OIDC values out of process argv.
    while IFS= read -r key; do export -n "$key"; done < <(compgen -e)
    for entry in "${CI_ENV[@]}"; do export "$entry"; done
    exec "$@"
}

ci_pg_exec() (
    ci_clean_exec timeout --signal=TERM --kill-after=2s 60s "$APPTAINER" exec --cleanenv --no-home --home "$CI_HOME" \
        --bind "$CI_PG_ROOT" "$CI_PG_SIF" "$@"
)

ci_pg_start() {
    local version port probe observed_version observed_data system recovery readonly role extra i
    version="$(ci_pg_exec postgres --version)" || return 1
    [[ "$version" =~ ^postgres\ \(PostgreSQL\)\ 18\. ]] || {
        echo '::error::selected PostgreSQL image does not provide PG18' >&2; return 1;
    }
    # The verified CI image supplies Python even on a bare HPC runner.
    port="$(ci_clean_exec timeout --signal=TERM --kill-after=2s 10s "$APPTAINER" exec --cleanenv --no-home --home "$CI_HOME" \
        "$SIF" "/opt/venv-$V/bin/python" -I -S -c \
        'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')" || return 1
    case "$port" in ''|*[!0-9]*) return 1 ;; esac
    [ "$port" -ge 1024 ] && [ "$port" -le 65535 ] || return 1
    ci_pg_exec initdb -D "$CI_PG_DATA" -U ci_tests --auth=trust >"$CI_PG_ROOT/init.log" 2>&1 || {
        echo '::error::owned CI PostgreSQL initialization failed' >&2; return 1;
    }
    (
        set +m
        trap 'sleep 3' INT TERM
        printf '%s|%s\n' "$BASHPID" "$(ci_pid_birth "$BASHPID")" > "$CI_PG_ROOT/group.identity"
        ci_clean_exec "$APPTAINER" exec --cleanenv --no-home --home "$CI_HOME" \
            --bind "$CI_PG_ROOT" "$CI_PG_SIF" postgres -D "$CI_PG_DATA" -p "$port" \
            -c listen_addresses=127.0.0.1 -c unix_socket_directories='' \
            -c fsync=off -c full_page_writes=off -c synchronous_commit=off &
        child=$!; status=0; wait "$child" || status=$?; exit "$status"
    ) >"$CI_PG_ROOT/server.log" 2>&1 &
    CI_PG_PID=$!
    CI_PG_START="$(ci_group_birth_record "$CI_PG_PID" "$CI_PG_ROOT/group.identity")" || return 1
    for ((i=0; i<40; i++)); do
        kill -0 "$CI_PG_PID" 2>/dev/null || break
        if ci_pg_exec pg_isready -h 127.0.0.1 -p "$port" -U ci_tests -d postgres -q >/dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done
    # A TCP accept is not sufficient: bind database identity to this datadir,
    # forbid the canonical fleet system ID BEFORE any write probe.
    probe="$(ci_pg_exec psql -X -h 127.0.0.1 -p "$port" -U ci_tests -d postgres \
        -v ON_ERROR_STOP=1 -Atq -c \
        "SELECT current_setting('server_version_num')||'|'||current_setting('data_directory')||'|'||system_identifier::text||'|'||pg_is_in_recovery()::text||'|'||current_setting('transaction_read_only')||'|'||current_user FROM pg_control_system();" \
        2>"$CI_PG_ROOT/probe-error.log")" || {
        echo '::error::owned CI PostgreSQL readiness/write probe failed' >&2; return 1;
    }
    IFS='|' read -r observed_version observed_data system recovery readonly role extra <<< "$probe"
    case "$observed_version" in 18[0-9][0-9][0-9][0-9]) ;; *) return 1 ;; esac
    case "$system" in ''|*[!0-9]*) return 1 ;; esac
    [ "$observed_data" = "$CI_PG_DATA" ] && [ "$system" != 7672112238472680366 ] && \
        [ "$recovery" = false ] && [ "$readonly" = off ] && [ "$role" = ci_tests ] && \
        [ -z "$extra" ] && [[ "$probe" != *$'\n'* ]] || {
        echo '::error::CI PostgreSQL identity/readiness mismatch' >&2; return 1;
    }
    # Revalidate this connection's identity inside the write transaction too.
    # The only interpolated value is the owned public datadir, SQL-escaped.
    local sql_data="${CI_PG_DATA//\'/\'\'}"
    ci_pg_exec psql -X -h 127.0.0.1 -p "$port" -U ci_tests -d postgres -v ON_ERROR_STOP=1 -Atq -c \
        "BEGIN; DO \$\$ BEGIN IF current_setting('data_directory') <> '$sql_data' OR (SELECT system_identifier::text FROM pg_control_system()) <> '$system' OR pg_is_in_recovery() THEN RAISE EXCEPTION 'CI target changed'; END IF; END \$\$; CREATE TEMP TABLE sac_ci_write_probe (n integer); INSERT INTO sac_ci_write_probe VALUES (1); ROLLBACK;" \
        >"$CI_PG_ROOT/write-probe.log" 2>"$CI_PG_ROOT/write-probe-error.log" || {
        echo '::error::owned CI PostgreSQL transactional write probe failed' >&2; return 1;
    }
    CI_ENV+=("APPTAINERENV_SAC_TEST_PG_DSN=postgresql://127.0.0.1:$port/postgres"
        "APPTAINERENV_SAC_TEST_PG_REQUIRED=1" "APPTAINERENV_PGUSER=ci_tests"
        "APPTAINERENV_SAC_CI_PG_DATA=$CI_PG_DATA"
        "APPTAINERENV_SAC_CI_PG_DATA_ID=$(stat -c '%d:%i' -- "$CI_PG_DATA")"
        "APPTAINERENV_SAC_CI_PG_SYSTEM_ID=$system"
        "APPTAINERENV_SAC_CI_PG_PID=$CI_PG_PID" "APPTAINERENV_SAC_CI_PG_START=$CI_PG_START")
    echo "exec-in-sif: owned writable PG18 ready (system=$system port=$port)"
}

ci_test_pg_require() {
    local port
    [ "${SAC_TEST_PG_REQUIRED:-}" = 1 ] && [ "${PGUSER:-}" = ci_tests ] && \
        [[ "${SAC_TEST_PG_DSN:-}" =~ ^postgresql://127\.0\.0\.1:[0-9]+/postgres$ ]] && \
        [ -n "${SAC_CI_PG_DATA:-}" ] && [ -n "${SAC_CI_PG_DATA_ID:-}" ] && \
        [ -n "${SAC_CI_PG_SYSTEM_ID:-}" ] && [ -n "${SAC_CI_PG_PID:-}" ] && \
        [ -n "${SAC_CI_PG_START:-}" ] || {
        echo '::error::test driver requires the qualified owned PG18 prerequisite' >&2; return 1;
    }
    case "${SAC_CI_PG_SYSTEM_ID}" in ''|*[!0-9]*|7672112238472680366)
        echo '::error::test PostgreSQL system identity is unproven or canonical' >&2; return 1 ;;
    esac
    port="${SAC_TEST_PG_DSN#postgresql://127.0.0.1:}"; port="${port%/postgres}"
    if [ "${#port}" -gt 5 ] || [ "$port" -lt 1024 ] || [ "$port" -gt 65535 ] || \
        [[ ! "$SAC_CI_PG_PID" =~ ^[1-9][0-9]*$ ]] || \
        [[ ! "$SAC_CI_PG_START" =~ ^[1-9][0-9]*$ ]] || \
        [[ ! "$SAC_CI_PG_DATA_ID" =~ ^[0-9]+:[0-9]+$ ]] || \
        [[ ! "$SAC_CI_PG_SYSTEM_ID" =~ ^[1-9][0-9]*$ ]] || \
        [[ "$SAC_CI_PG_DATA" != /* ]]; then
        echo '::error::test PostgreSQL prerequisite metadata is malformed' >&2
        return 1
    fi
}

ci_test_pg_verify() {
    "${1:?Python required}" -I - "${2:-}" <<'PY'
import os
import stat
import sys
from pathlib import Path

if sys.argv[1]:
    sys.path.insert(0, sys.argv[1])
import psycopg

data = Path(os.environ['SAC_CI_PG_DATA'])
metadata = data.lstat()
if not stat.S_ISDIR(metadata.st_mode) or data.resolve() != data:
    raise RuntimeError('CI PostgreSQL physical directory is unproven')
if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
    raise RuntimeError('CI PostgreSQL directory ownership mismatch')
if f'{metadata.st_dev}:{metadata.st_ino}' != os.environ['SAC_CI_PG_DATA_ID']:
    raise RuntimeError('CI PostgreSQL directory incarnation changed')
pid = os.environ['SAC_CI_PG_PID']
if not pid.isdecimal() or int(pid) <= 0:
    raise RuntimeError('CI PostgreSQL process identity is unproven')
fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
if fields[0] == 'Z' or fields[19] != os.environ['SAC_CI_PG_START']:
    raise RuntimeError('CI PostgreSQL process incarnation changed')
with psycopg.connect(os.environ['SAC_TEST_PG_DSN'], user='ci_tests', connect_timeout=5) as connection:
    connection.read_only = True
    row = connection.execute("SELECT current_setting('server_version_num'), current_setting('data_directory'), system_identifier::text, pg_is_in_recovery(), current_user, current_setting('default_transaction_read_only') FROM pg_control_system()").fetchone()
if not row or row != (row[0], str(data), os.environ['SAC_CI_PG_SYSTEM_ID'], False, 'ci_tests', 'off'):
    raise RuntimeError('CI PostgreSQL connection identity mismatch')
if not 180000 <= int(row[0]) < 190000 or row[2] == '7672112238472680366':
    raise RuntimeError('CI PostgreSQL version/fleet guard refused')
print('test driver: current owned non-fleet PG18 identity verified read-only')
PY
}

ci_driver_environment() {
    local key entry
    local kept=("PATH=$PATH" "HOME=$HOME" "LANG=C.UTF-8" "LC_ALL=C.UTF-8" "TZ=Asia/Tokyo")
    for key in GITHUB_ACTIONS GITHUB_RUN_ID GITHUB_RUN_ATTEMPT GITHUB_JOB GITHUB_SHA \
        GITHUB_REF GITHUB_REF_NAME GITHUB_REF_TYPE GITHUB_EVENT_NAME GITHUB_REPOSITORY \
        SAC_CI_TMPDIR_ROOT SAC_CI_OWNER_PID SAC_CI_OWNER_START SAC_CI_GROUP_PID SAC_CI_GROUP_START \
        SAC_TEST_PG_DSN SAC_TEST_PG_REQUIRED PGUSER SAC_CI_PG_DATA SAC_CI_PG_DATA_ID \
        SAC_CI_PG_SYSTEM_ID SAC_CI_PG_PID SAC_CI_PG_START CI_XDIST_WORKERS SLURM_CPUS_PER_TASK \
        OMP_NUM_THREADS OMP_THREAD_LIMIT TMPDIR; do
        if [ "${!key+x}" ]; then kept+=("$key=${!key}"); fi
    done
    while IFS= read -r key; do export -n "$key"; done < <(compgen -e)
    for entry in "${kept[@]}"; do export "$entry"; done
}
