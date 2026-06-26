#!/usr/bin/env bash
# sac-listen-health-probe.sh — fleet-infrastructure watchdog for the
# central `sac listen` (a2a + registry control plane, default
# 127.0.0.1:7878).
#
# Incident 2026-06-26: the listen died mid-session with NO auto-restart
# and NO alarm, so the whole fleet lost agent-to-agent comms SILENTLY.
# systemd's own Restart=always covers a *process exit*, but a listen
# that is wedged (process alive, port bound, but the HTTP server no
# longer answering /v1/health) is invisible to systemd. This probe
# closes that gap: it HTTP-probes the health endpoint and, on failure,
#   1. logs a LOUD ERROR (journal, via stderr — the timer unit routes
#      both streams to `journalctl --user -u sac-listen-health`),
#   2. restarts `sac-listen.service` (clearing any `failed` state first
#      so a tripped StartLimit self-heals),
#   3. best-effort emits the anomaly on the operator alarm path
#      (`sac fleet notify blocker`) so the restart is VISIBLE, not
#      silent.
#
# Run by `sac-listen-health.timer` every ~30s. Also runnable by hand:
#
#   scripts/systemd/sac-listen-health-probe.sh            # probe+heal
#   scripts/systemd/sac-listen-health-probe.sh --check-only   # probe, no side effects
#
# Exit codes:
#   0  healthy (or, in heal mode, a restart was issued successfully)
#   1  unhealthy (in --check-only) / restart attempt failed (heal mode)
#   2  usage error
#
# Env overrides (all optional):
#   SAC_LISTEN_HEALTH_URL   default http://127.0.0.1:7878/v1/health
#   SAC_LISTEN_UNIT         default sac-listen.service
#   SAC_LISTEN_PROBE_TIMEOUT  curl --max-time seconds, default 5
#   SAC_LISTEN_NOTIFY       1 (default) to attempt `sac fleet notify`,
#                           0 to skip (used by tests / non-lead hosts)
#
# No mocks (STX-NM002): the probe talks to a REAL HTTP endpoint over a
# REAL socket; tests stand up a real local HTTP server and assert the
# real curl-based classification.
set -uo pipefail

HEALTH_URL="${SAC_LISTEN_HEALTH_URL:-http://127.0.0.1:7878/v1/health}"
LISTEN_UNIT="${SAC_LISTEN_UNIT:-sac-listen.service}"
PROBE_TIMEOUT="${SAC_LISTEN_PROBE_TIMEOUT:-5}"
NOTIFY_ENABLED="${SAC_LISTEN_NOTIFY:-1}"

CHECK_ONLY=0
case "${1:-}" in
  --check-only) CHECK_ONLY=1 ;;
  "" ) ;;
  -h|--help)
    sed -n '2,40p' "$0"
    exit 0
    ;;
  *)
    echo "usage: $0 [--check-only]" >&2
    exit 2
    ;;
esac

# probe_health URL TIMEOUT -> 0 if the daemon answered with ANY HTTP
# status (incl. 401/403 under bearer auth — the daemon is up and
# auth-gating), 1 if the transport failed (connection refused / DNS /
# timeout). This mirrors the liveness contract in
# `_listen/_restart.py::wait_for_health`: "got an HTTP response" == up,
# only a transport failure == down. That is auth-change-proof: a future
# decision to bearer-gate /v1/health must NOT make the watchdog SIGKILL
# a live, 401-answering daemon.
probe_health() {
  local url="$1" timeout="$2" code
  # -s silent, -o /dev/null discard body, -w '%{http_code}' print the
  # numeric status, --max-time bound the whole request. curl exits
  # non-zero on a transport failure and prints "000" as the code.
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$timeout" "$url" 2>/dev/null)"
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    return 1
  fi
  # "000" == curl reached no HTTP layer (e.g. connection refused that
  # still returned rc 0 on some curl builds). Treat as down.
  if [ -z "$code" ] || [ "$code" = "000" ]; then
    return 1
  fi
  return 0
}

if probe_health "$HEALTH_URL" "$PROBE_TIMEOUT"; then
  # Quiet on the happy path — a healthy 30s probe should NOT spam the
  # journal. Operators tail for the LOUD line below.
  exit 0
fi

# --- UNHEALTHY ------------------------------------------------------------

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "sac-listen-health: UNHEALTHY ${HEALTH_URL} (no HTTP response) at ${ts}" >&2
  exit 1
fi

# LOUD ERROR — this is the line that makes a restart VISIBLE in the
# journal. Prefixed ERROR + the incident reference so a `journalctl`
# grep finds it.
echo "ERROR: sac-listen DOWN — ${HEALTH_URL} did not answer (transport failure)." >&2
echo "ERROR: sac-listen watchdog is RESTARTING ${LISTEN_UNIT} at ${ts}." >&2
echo "ERROR: incident-class=sac-listen-watchdog-autorestart-alarm; fleet a2a comms were interrupted." >&2

# Best-effort operator alarm. The listen is the transport for
# `sac fleet notify`, so this MAY fail (the listen is down). We still
# try: (a) if only the HTTP layer wedged but the inbox path recovers,
# or (b) if a peer/lead listen on another host receives it. Failure is
# logged but does NOT change the restart outcome.
if [ "$NOTIFY_ENABLED" = "1" ] && command -v sac >/dev/null 2>&1; then
  if sac fleet notify blocker \
      --from-agent "sac-listen-watchdog" \
      --summary "sac listen DOWN — watchdog restarting ${LISTEN_UNIT}" \
      --detail "health probe to ${HEALTH_URL} failed at ${ts}; fleet a2a comms interrupted. Auto-restart in progress." \
      >/dev/null 2>&1; then
    echo "sac-listen-health: alarm pushed via 'sac fleet notify blocker'." >&2
  else
    echo "WARN: sac-listen-health: 'sac fleet notify blocker' failed (expected if the listen itself is the down transport)." >&2
  fi
fi

# Restart. `reset-failed` first so a tripped StartLimitBurst (the unit
# entered `failed` after 5 restarts/60s) is cleared and `restart` can
# proceed — otherwise systemd refuses with "start request repeated too
# quickly".
restart_rc=0
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user reset-failed "$LISTEN_UNIT" >/dev/null 2>&1 || true
  if systemctl --user restart "$LISTEN_UNIT"; then
    echo "sac-listen-health: issued 'systemctl --user restart ${LISTEN_UNIT}'." >&2
  else
    restart_rc=$?
    echo "ERROR: sac-listen-health: 'systemctl --user restart ${LISTEN_UNIT}' FAILED (rc=${restart_rc})." >&2
  fi
else
  echo "ERROR: sac-listen-health: systemctl not found; cannot restart ${LISTEN_UNIT}." >&2
  restart_rc=1
fi

exit "$restart_rc"
