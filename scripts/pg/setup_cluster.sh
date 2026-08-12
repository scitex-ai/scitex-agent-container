#!/bin/bash
# ===========================================================================
# Stand up a rootless PostgreSQL 18 cluster on this host for the card store.
#
# Rootless: runs inside apptainer as the invoking user; PGDATA on local ext4.
# Port 55432 per ADR-0022 (5432 is never used for scitex).
#
# ---------------------------------------------------------------------------
# WHY THE SOCKET AUTH METHOD AND THE .pgpass ENTRY ARE ONE CHANGE
# ---------------------------------------------------------------------------
# This script used to run `initdb --auth-local=trust`, which made every new
# cluster born wide open: every agent container binds /home/ywatanabe, the
# socket is srwxrwxrwx, every agent runs as uid 1000, and the login role is
# the bootstrap SUPERUSER -- so any agent could connect as any role with no
# password and drop the board.
#
# Setting `--auth-local=scram-sha-256` ALONE is equally wrong: it locks every
# socket user out of every new cluster. The two halves must land together,
# and the .pgpass entry has a trap in it:
#
#   libpq rewrites a unix-socket host to the literal string "localhost" ONLY
#   when the socket directory equals its compiled-in default. This cluster
#   sets unix_socket_directories to $RUNDIR, which is NOT that default, so
#   the .pgpass lookup key is the LITERAL PATH $RUNDIR -- not "localhost".
#
# A "localhost" line therefore pre-flights fine over TCP and still fails on
# the socket. That is exactly how scitex-compute-04 broke on 2026-08-11. We
# provision all three keys ($RUNDIR, localhost, 127.0.0.1) with a WILDCARD
# database field, because a db-specific line (db=scitex_cards) does not cover
# a connection to db=postgres -- which is how scitex-compute-03 broke.
#
# Verified by tests/integration/test_pg_cluster_provisioning.py, which stands
# up a throwaway cluster and proves a no-password socket connection is refused
# while a .pgpass one succeeds.
#
# A password is NEVER echoed, logged, or passed on a command line.
# ===========================================================================
set -uo pipefail

PGROOT=${SCITEX_PG_ROOT:-$HOME/.scitex/pg}
SIF=${SCITEX_PG_SIF:-$PGROOT/postgres18.sif}
PORT=${SCITEX_PG_PORT:-55432}
ROLE=${SCITEX_PG_ROLE:-scitex_cards}
PGPASS=${SCITEX_PGPASS:-$HOME/.pgpass}
PGDATA=$PGROOT/18/main
RUNDIR=$PGROOT/run
LOGDIR=$PGROOT/logs

mkdir -p "$PGROOT/18" "$RUNDIR" "$LOGDIR"
chmod 700 "$PGROOT/18"

[ -f "$SIF" ] || { echo "SETUP: FAILED - no postgres SIF at $SIF"; exit 1; }

# --- password + .pgpass provisioning ---------------------------------------
# Resolve the role password from an existing .pgpass entry, else mint one.
# The value is only ever moved between files; it is never printed.
umask 077
touch "$PGPASS" && chmod 600 "$PGPASS"

PWFILE=$(mktemp "$PGROOT/.pw.XXXXXX") || { echo "SETUP: FAILED - mktemp"; exit 1; }
chmod 600 "$PWFILE"
trap 'shred -u "$PWFILE" 2>/dev/null || rm -f "$PWFILE"' EXIT

# Prefer this cluster's own port, then any entry for the role. Deliberately
# NOT keyed on 127.0.0.1:5432 as the old version was: that port is banned by
# ADR-0022, and hosts provisioned after the ban have no such line at all, so
# the old lookup simply failed there.
awk -F: -v port="$PORT" -v role="$ROLE" '
  $4 == role && $2 == port && best == "" { l = $0; sub(/^([^:]*:){4}/, "", l); best = l }
  $4 == role && any  == ""               { l = $0; sub(/^([^:]*:){4}/, "", l); any  = l }
  END { if (best != "") print best; else if (any != "") print any }
' "$PGPASS" > "$PWFILE"

if [ ! -s "$PWFILE" ]; then
  echo "SETUP: no existing .pgpass entry for '$ROLE' - minting a new password"
  # ':' is the .pgpass field separator and '\' its escape; exclude both.
  LC_ALL=C tr -dc 'A-Za-z0-9_.~-' < /dev/urandom | head -c 40 > "$PWFILE"
  echo >> "$PWFILE"
fi

# Ensure a wildcard-database entry exists for every key this cluster can be
# reached by. Append-only; existing lines are never rewritten.
NEWLINES=$(mktemp "$PGROOT/.pgpass.add.XXXXXX") || exit 1
chmod 600 "$NEWLINES"
awk -F: -v port="$PORT" -v role="$ROLE" -v keys="$RUNDIR|localhost|127.0.0.1" \
        -v pwfile="$PWFILE" '
  { if ($3 == "*" && $2 == port && $4 == role) seen[$1] = 1 }
  END {
    getline pw < pwfile
    n = split(keys, K, "|")
    for (i = 1; i <= n; i++)
      if (!(K[i] in seen)) print K[i] ":" port ":*:" role ":" pw
  }
' "$PGPASS" > "$NEWLINES"

if [ -s "$NEWLINES" ]; then
  awk -F: '{printf "SETUP: .pgpass += %s:%s:%s:%s:<REDACTED>\n",$1,$2,$3,$4}' "$NEWLINES"
  cat "$NEWLINES" >> "$PGPASS"
fi
shred -u "$NEWLINES" 2>/dev/null || rm -f "$NEWLINES"
chmod 600 "$PGPASS"

# --- initdb -----------------------------------------------------------------
if [ -f "$PGDATA/PG_VERSION" ]; then
  echo "SETUP: PGDATA already initialized at $PGDATA (PG_VERSION=$(cat "$PGDATA/PG_VERSION")) - skipping initdb"
else
  echo "SETUP: running initdb ..."
  apptainer exec "$SIF" initdb \
      -D "$PGDATA" \
      -U "$ROLE" \
      --encoding=UTF8 \
      --locale=en_US.UTF-8 \
      --locale-provider=libc \
      --auth-local=scram-sha-256 \
      --auth-host=scram-sha-256 \
      --pwfile="$PWFILE" 2>&1
  rc=$?
  if [ $rc -ne 0 ]; then echo "SETUP: initdb FAILED rc=$rc"; exit 1; fi
fi

# --- configuration: loopback only, non-conflicting port -----------------------
CONF=$PGDATA/postgresql.conf
python3 - "$CONF" "$PORT" "$RUNDIR" "$LOGDIR" <<'PY'
import sys, re
conf, port, rundir, logdir = sys.argv[1:5]
want = {
    "port": port,
    "listen_addresses": "'127.0.0.1'",
    "unix_socket_directories": f"'{rundir}'",
    "logging_collector": "on",
    "log_directory": f"'{logdir}'",
    "log_filename": "'postgresql-%Y%m%d.log'",
    "log_line_prefix": "'%m [%p] %q%u@%d '",
    "log_min_duration_statement": "5000",
}
lines = open(conf).read().splitlines()
out, seen = [], set()
for ln in lines:
    m = re.match(r"\s*#?\s*([a-z_]+)\s*=", ln)
    if m and m.group(1) in want:
        k = m.group(1)
        if k in seen:
            continue
        seen.add(k)
        out.append(f"{k} = {want[k]}\t\t# scitex card store")
    else:
        out.append(ln)
for k, v in want.items():
    if k not in seen:
        out.append(f"{k} = {v}\t\t# scitex card store")
open(conf, "w").write("\n".join(out) + "\n")
print("SETUP: postgresql.conf updated ->", ", ".join(f"{k}={want[k]}" for k in ("port","listen_addresses","unix_socket_directories")))
PY

# --- refuse to hand back a cluster that is open on the socket ----------------
HBA=$PGDATA/pg_hba.conf
if grep -qE '^[[:space:]]*local[[:space:]]+(all|replication)[[:space:]]+\S+[[:space:]]+trust' "$HBA"; then
  echo "SETUP: FAILED - a 'local ... trust' rule is present in $HBA."
  echo "SETUP: every agent shares uid 1000 and can see the socket, so that is"
  echo "SETUP: an unauthenticated superuser path to the card store."
  grep -nE '^[[:space:]]*local' "$HBA"
  exit 1
fi

echo "SETUP: pg_hba.conf host rules:"
grep -E '^(host|local)' "$HBA"
echo "SETUP: done"
