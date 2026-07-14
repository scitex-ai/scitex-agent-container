#!/usr/bin/env bash
# sac-listen-health-probe.sh — fleet-infrastructure watchdog for the central
# `sac listen` (a2a + registry control plane, default 127.0.0.1:7878).
#
# Full rationale + both incidents: scripts/systemd/README.md ("The health
# watchdog: how it decides"). The short version:
#
# WHY IT EXISTS (incident 2026-06-26). `sac listen` died mid-session with no
# restart and no alarm, and the fleet lost a2a comms SILENTLY. systemd's
# `Restart=always` covers a process EXIT, but a WEDGED listen (alive, port
# bound, HTTP not answering) is invisible to it. This probe closes that gap.
# That coverage is load-bearing and is PRESERVED.
#
# WHY IT WAS REWRITTEN (incident 2026-07-14). The probe used to restart after
# ONE failed curl with a 5-SECOND deadline. On a box that idles at load 60-70
# that is not a health check, it is a coin flip — and the remedy is
# catastrophic: every `sac listen` restart tears down the in-memory a2a
# Broker, DEAFENING EVERY AGENT'S INBOX AT ONCE. So a slow probe did not
# merely mis-report, it MANUFACTURED the outage it claimed to detect — then
# re-probed DURING its own restart, saw a genuinely-down daemon, and
# restarted AGAIN (measured: 2 restarts in 26s of a HEALTHY daemon; 3/3
# against a real server that answered HTTP 200 in 8s).
# A PROBE THAT MUTATES IS NOT A PROBE.
#
# THE DECISION MODEL. Both directions are bugs: never acting hides an outage,
# over-acting CREATES one. The false-RED is the worse of the two, because its
# remedy destroys a healthy thing — so only a CORROBORATED verdict restarts.
#
#   1. THREE STATES, never a bool (two cannot express "I asked and got
#      nothing", which is exactly what a loaded box produces):
#        UP       it ANSWERED — any HTTP status < 500 (see 2)
#        DOWN     a POSITIVE observation of not-serving:
#                   connection REFUSED (kernel sent RST: nothing is
#                     listening) ................................ weight 2
#                   HTTP 5xx (answered, but its health route errors —
#                     bound and speaking HTTP, yet not healthy) .. weight 1
#        UNKNOWN  we asked and got NOTHING (timeout/DNS/reset). Under load
#                 this is what a HEALTHY-but-busy daemon looks like. Not
#                 health — but NOT evidence of death either ...... weight 1
#      Absence of evidence is not evidence of death.
#
#   2. ANY HTTP STATUS < 500 IS "UP" (deliberate — keep it). A 401/403 PROVES
#      the daemon is up: bound, speaking HTTP, auth-gating. Card
#      `sac-listen-restart-healthcheck-bearer` (PR #463) exists because gating
#      liveness on `status == 200` re-classified a live, 401-answering daemon
#      as "down" — a false-RED that killed a HEALTHY process. Only a *server
#      error* (5xx) or *no answer at all* counts against it. Matches
#      `_listen/_holder_health.py`.
#
#   3. CORROBORATION, and a failure is a FACT. Weight must reach
#      SAC_LISTEN_FAIL_THRESHOLD before we act: 2 consecutive REFUSALS act
#      fast (crash coverage); 3 consecutive UNKNOWNs act (wedge coverage —
#      corroboration is what PROMOTES a repeated UNKNOWN into a DOWN).
#      A SINGLE SUCCESS DOES NOT WIPE THE LEDGER: that bug (`consecutive = 0`
#      reset by one lucky reply) is this class's other half, just fixed in
#      `sac listen`'s own holder check (PR #673, `_listen/_standby_ledger.py`),
#      where a flapping holder oscillated 1/2 -> "healthy" forever and was
#      NEVER acted on. Here a success builds a SERVING STREAK and the ledger
#      clears only after SAC_LISTEN_RECOVERY_STREAK consecutive UPs — logged
#      LOUD. Blip once and you are not destroyed; keep failing and you are
#      always eventually healed.
#
#   4. NEVER RESTART SOMETHING THAT IS ALREADY RESTARTING. Two INDEPENDENT
#      guards, so losing one cannot resurrect the 26s double-restart:
#        (a) POST-RESTART BACKOFF — after issuing a restart we do not probe
#            AT ALL for SAC_LISTEN_RESTART_BACKOFF seconds.
#        (b) UNIT-STATE GUARD — if systemd reports the unit `activating` it
#            is already coming back (someone else restarted it, or it
#            crashed and `Restart=always` caught it): stand down. Holds even
#            if the state file is lost or corrupt.
#
#   5. RATE-LIMIT THE REMEDY. At most SAC_LISTEN_MAX_RESTARTS per
#      SAC_LISTEN_RESTART_WINDOW. Beyond that: ALARM LOUDLY and STOP. If N
#      restarts did not fix it the (N+1)th will not either, and an unbounded
#      restarter on a bad signal is how a fleet goes down at 3am.
#
# USAGE
#   sac-listen-health-probe.sh              # probe + heal (the timer path)
#   sac-listen-health-probe.sh --check-only # probe, ZERO side effects
#   sac-listen-health-probe.sh --status     # dump the ledger, exit 0
#   sac-listen-health-probe.sh --reset      # clear the ledger, exit 0
#
# Exit: 0 UP (or heal: restart issued / deliberately stood down)
#       1 not UP (--check-only) / restart attempt FAILED (heal)
#       2 usage error
#
# Env overrides (all optional):
#   SAC_LISTEN_HEALTH_URL      default http://127.0.0.1:7878/v1/health
#   SAC_LISTEN_UNIT            default sac-listen.service
#   SAC_LISTEN_PROBE_TIMEOUT   total curl deadline, s        default 20
#                              (was 5 — the fuse that caused the incident.
#                              The live daemon answers in ~30ms, so 20s is
#                              ~600x the median and still catches a wedge.)
#   SAC_LISTEN_CONNECT_TIMEOUT TCP-connect deadline, s       default 5
#   SAC_LISTEN_FAIL_THRESHOLD  failure weight to act         default 3
#   SAC_LISTEN_RECOVERY_STREAK consecutive UPs that clear it default 2
#   SAC_LISTEN_RESTART_BACKOFF no-probe window post-restart  default 90
#   SAC_LISTEN_MAX_RESTARTS    restarts per window           default 2
#   SAC_LISTEN_RESTART_WINDOW  rate-limit window, s          default 600
#   SAC_LISTEN_HEALTH_STATE    ledger path; default
#                    $HOME/.scitex/agent-container/runtime/listen-health.state
#   SAC_LISTEN_NOTIFY          1 (default) to attempt `sac fleet notify`,
#                              0 to skip (tests / non-lead hosts)
#
# No mocks (STX-NM002): the probe talks to a REAL endpoint over a REAL
# socket; tests stand up a real local HTTP server — slow, refusing, 5xx-ing,
# dying — and assert the REAL decision. Tests NEVER touch port 7878.
set -uo pipefail

HEALTH_URL="${SAC_LISTEN_HEALTH_URL:-http://127.0.0.1:7878/v1/health}"
LISTEN_UNIT="${SAC_LISTEN_UNIT:-sac-listen.service}"
PROBE_TIMEOUT="${SAC_LISTEN_PROBE_TIMEOUT:-20}"
CONNECT_TIMEOUT="${SAC_LISTEN_CONNECT_TIMEOUT:-5}"
FAIL_THRESHOLD="${SAC_LISTEN_FAIL_THRESHOLD:-3}"
RECOVERY_STREAK="${SAC_LISTEN_RECOVERY_STREAK:-2}"
RESTART_BACKOFF="${SAC_LISTEN_RESTART_BACKOFF:-90}"
MAX_RESTARTS="${SAC_LISTEN_MAX_RESTARTS:-2}"
RESTART_WINDOW="${SAC_LISTEN_RESTART_WINDOW:-600}"
NOTIFY_ENABLED="${SAC_LISTEN_NOTIFY:-1}"
STATE_FILE="${SAC_LISTEN_HEALTH_STATE:-${HOME}/.scitex/agent-container/runtime/listen-health.state}"

# A REFUSAL is a hard, positive observation ("nothing is listening"); a
# timeout is the ABSENCE of an observation. Refusal counts harder, and so
# reaches the threshold faster.
WEIGHT_HARD=2
WEIGHT_SOFT=1

MODE="heal"
case "${1:-}" in
  --check-only) MODE="check" ;;
  --status)     MODE="status" ;;
  --reset)      MODE="reset" ;;
  "")           ;;
  -h|--help)    sed -n '2,110p' "$0"; exit 0 ;;
  *)
    echo "usage: $0 [--check-only|--status|--reset]" >&2
    exit 2
    ;;
esac

now() { date +%s; }
iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# --- ledger ---------------------------------------------------------------
# Persisted across timer firings: the script is invoked FRESH every ~30s, so
# an in-memory counter would always read zero and "N consecutive failures"
# could never be observed. Plain key=value, NEVER `source`d — a corrupt or
# hostile state file must not become code. A value that is not a plain
# integer resets that field to 0 (fail-safe: no history == do not restart).

FAILURES=0        # accumulated, un-cleared failure WEIGHT
SERVING_STREAK=0  # consecutive UPs since the last failure
LAST_RESTART=0    # epoch of the last restart WE issued
LAST_ALARM=0      # epoch of the last operator alarm (alarm-spam guard)
RESTARTS=""       # comma-separated epochs of restarts we issued

_int() { case "$1" in ''|*[!0-9]*) echo 0 ;; *) echo "$1" ;; esac; }

read_state() {
  [ -r "$STATE_FILE" ] || return 0
  local key val
  while IFS='=' read -r key val; do
    case "$key" in
      failures)       FAILURES="$(_int "$val")" ;;
      serving_streak) SERVING_STREAK="$(_int "$val")" ;;
      last_restart)   LAST_RESTART="$(_int "$val")" ;;
      last_alarm)     LAST_ALARM="$(_int "$val")" ;;
      restarts)
        case "$val" in
          ''|*[!0-9,]*) RESTARTS="" ;;
          *)            RESTARTS="$val" ;;
        esac
        ;;
    esac
  done < "$STATE_FILE"
  return 0
}

write_state() {
  mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true
  local tmp="${STATE_FILE}.$$"
  {
    echo "failures=${FAILURES}"
    echo "serving_streak=${SERVING_STREAK}"
    echo "last_restart=${LAST_RESTART}"
    echo "last_alarm=${LAST_ALARM}"
    echo "restarts=${RESTARTS}"
  } > "$tmp" 2>/dev/null || return 1
  # Atomic swap: a probe killed mid-write must never leave a torn ledger.
  mv -f "$tmp" "$STATE_FILE" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; return 1; }
  return 0
}

# Drop restart timestamps that have aged out of the rate-limit window.
prune_restarts() {
  local cutoff="$1" kept="" ts
  local IFS=','
  for ts in $RESTARTS; do
    [ -z "$ts" ] && continue
    if [ "$ts" -gt "$cutoff" ] 2>/dev/null; then
      kept="${kept:+${kept},}${ts}"
    fi
  done
  RESTARTS="$kept"
}

count_restarts() {
  local n=0 ts
  local IFS=','
  for ts in $RESTARTS; do
    [ -n "$ts" ] && n=$((n + 1))
  done
  echo "$n"
}

# --- probe ----------------------------------------------------------------
# Classify ONE observation as UP / DOWN / UNKNOWN. curl hands us BOTH halves
# of the discrimination in a single call: the exit code separates "refused"
# (7) from "timed out" (28), and %{time_connect} says whether the TCP
# handshake COMPLETED — i.e. whether anything is bound at all — even when no
# HTTP ever arrived.
#
# Echoes: "<verdict> <weight> <evidence...>"
probe_once() {
  local out code connect rc
  out="$(curl -s -o /dev/null \
              -w '%{http_code} %{time_connect}' \
              --connect-timeout "$CONNECT_TIMEOUT" \
              --max-time "$PROBE_TIMEOUT" \
              "$HEALTH_URL" 2>/dev/null)"
  rc=$?
  code="${out%% *}"
  connect="${out##* }"
  [ -z "$code" ] && code=000

  if [ "$rc" -eq 0 ] && [ "$code" != "000" ]; then
    if [ "$code" -ge 500 ] 2>/dev/null; then
      # It ANSWERED — bound, speaking HTTP — but its health route is
      # erroring. An answer, but not health. SOFT: it is demonstrably alive,
      # so destroying it demands the full corroboration.
      echo "DOWN ${WEIGHT_SOFT} HTTP ${code} (server error from the health route)"
    else
      # Any status < 500 — including 401/403/404 — PROVES it is serving.
      echo "UP 0 HTTP ${code}"
    fi
    return
  fi

  case "$rc" in
    7)
      # Connection REFUSED: the kernel sent RST. NOTHING is listening. A
      # positive observation of absence, not a failure to observe.
      echo "DOWN ${WEIGHT_HARD} connection refused (nothing is listening on the port)"
      ;;
    28)
      # Timed out. Did the TCP handshake at least COMPLETE? If so the socket
      # is BOUND — the daemon has not exited — and this is the genuinely
      # ambiguous case: busy (healthy) or wedged. UNKNOWN, never DOWN.
      if [ -n "$connect" ] && [ "$connect" != "0.000000" ] && [ "$connect" != "0" ]; then
        echo "UNKNOWN ${WEIGHT_SOFT} no HTTP answer within ${PROBE_TIMEOUT}s (but TCP connected in ${connect}s — the port IS bound)"
      else
        echo "UNKNOWN ${WEIGHT_SOFT} could not connect within ${CONNECT_TIMEOUT}s (no RST — blackholed, or the accept backlog is full)"
      fi
      ;;
    6)
      echo "UNKNOWN ${WEIGHT_SOFT} DNS resolution failed (our resolver — not necessarily the daemon)"
      ;;
    *)
      # Reset mid-reply, empty reply, etc: the TCP was answered but the
      # exchange broke. Ambiguous under load. UNKNOWN.
      echo "UNKNOWN ${WEIGHT_SOFT} no usable answer (curl rc=${rc})"
      ;;
  esac
}

unit_active_state() {
  command -v systemctl >/dev/null 2>&1 || { echo "unknown"; return; }
  local st
  st="$(systemctl --user show -p ActiveState --value "$LISTEN_UNIT" 2>/dev/null)"
  echo "${st:-unknown}"
}

# The ONE operator alarm rail (`sac fleet notify`). Not forked, not replaced.
alarm() {
  local summary="$1" detail="$2"
  [ "$NOTIFY_ENABLED" = "1" ] || return 0
  command -v sac >/dev/null 2>&1 || return 0
  if sac fleet notify blocker \
      --from-agent "sac-listen-watchdog" \
      --summary "$summary" \
      --detail "$detail" \
      >/dev/null 2>&1; then
    echo "sac-listen-health: alarm pushed via 'sac fleet notify blocker'." >&2
  else
    echo "WARN: sac-listen-health: 'sac fleet notify blocker' failed (expected if the listen itself is the down transport)." >&2
  fi
}

# ==========================================================================
# MAIN
# ==========================================================================

read_state
NOW="$(now)"
TS="$(iso)"

if [ "$MODE" = "reset" ]; then
  FAILURES=0
  SERVING_STREAK=0
  LAST_RESTART=0
  LAST_ALARM=0
  RESTARTS=""
  write_state
  echo "sac-listen-health: ledger reset (${STATE_FILE})."
  exit 0
fi

if [ "$MODE" = "status" ]; then
  prune_restarts "$((NOW - RESTART_WINDOW))"
  echo "state_file:      ${STATE_FILE}"
  echo "health_url:      ${HEALTH_URL}"
  echo "unit:            ${LISTEN_UNIT} (ActiveState=$(unit_active_state))"
  echo "probe_timeout:   ${PROBE_TIMEOUT}s (connect ${CONNECT_TIMEOUT}s)"
  echo "failure_weight:  ${FAILURES} / ${FAIL_THRESHOLD}  (weight required to restart)"
  echo "serving_streak:  ${SERVING_STREAK} / ${RECOVERY_STREAK}  (consecutive UPs that clear the ledger)"
  echo "restarts_in_${RESTART_WINDOW}s: $(count_restarts) / ${MAX_RESTARTS}"
  if [ "$LAST_RESTART" -gt 0 ]; then
    echo "last_restart:    $((NOW - LAST_RESTART))s ago (no-probe backoff ${RESTART_BACKOFF}s)"
  else
    echo "last_restart:    never"
  fi
  exit 0
fi

# --- GUARD (a): POST-RESTART BACKOFF --------------------------------------
# We do not probe AT ALL inside the cooling-off window. The original
# watchdog's SECOND restart happened *because* it probed during its own
# restart, saw a genuinely-down daemon, and fired again. You cannot draw a
# conclusion about a daemon you are in the middle of restarting.
if [ "$MODE" = "heal" ] && [ "$LAST_RESTART" -gt 0 ]; then
  age=$((NOW - LAST_RESTART))
  if [ "$age" -lt "$RESTART_BACKOFF" ]; then
    echo "sac-listen-health: within post-restart backoff (${age}s < ${RESTART_BACKOFF}s) — NOT probing. A daemon that is still coming up cannot be judged." >&2
    exit 0
  fi
fi

# --- OBSERVE --------------------------------------------------------------
read -r VERDICT WEIGHT EVIDENCE <<< "$(probe_once)"

# --- check-only: pure observation, ZERO side effects ----------------------
if [ "$MODE" = "check" ]; then
  if [ "$VERDICT" = "UP" ]; then
    exit 0
  fi
  echo "sac-listen-health: ${VERDICT} ${HEALTH_URL} — ${EVIDENCE} at ${TS}" >&2
  exit 1
fi

# --- RECORD ---------------------------------------------------------------
# A failure is a FACT: one later success does not un-happen it. A success
# builds a SERVING STREAK; the ledger clears only on a SUSTAINED recovery,
# and that clearing is LOUD. (Consistent with _listen/_standby_ledger.py,
# PR #673 — the same bug class, in the other direction.)
if [ "$VERDICT" = "UP" ]; then
  if [ "$FAILURES" -eq 0 ]; then
    # A clean daemon on the quiet path. Stay QUIET — a healthy 30s probe
    # must not spam the journal.
    SERVING_STREAK=0
    write_state
    exit 0
  fi
  SERVING_STREAK=$((SERVING_STREAK + 1))
  if [ "$SERVING_STREAK" -ge "$RECOVERY_STREAK" ]; then
    echo "sac-listen-health: RECOVERED — ${HEALTH_URL} answered ${SERVING_STREAK}x in a row (${EVIDENCE}); clearing a failure ledger that stood at ${FAILURES}/${FAIL_THRESHOLD}. No restart was needed." >&2
    FAILURES=0
    SERVING_STREAK=0
  else
    echo "sac-listen-health: ${HEALTH_URL} answered (${EVIDENCE}), but a failure ledger of ${FAILURES}/${FAIL_THRESHOLD} still stands — it needs ${RECOVERY_STREAK} consecutive answers to clear (${SERVING_STREAK}/${RECOVERY_STREAK}). One lucky reply does not un-happen a failed check." >&2
  fi
  write_state
  exit 0
fi

# Not UP. Accrue.
SERVING_STREAK=0
FAILURES=$((FAILURES + WEIGHT))

if [ "$FAILURES" -lt "$FAIL_THRESHOLD" ]; then
  # UNCORROBORATED. This is the whole fix: ONE failed probe on a loaded box
  # means nothing, and the remedy (a restart) would deafen every agent's
  # inbox. We say exactly what we saw, and we WAIT.
  echo "WARN: sac-listen-health: ${VERDICT} — ${EVIDENCE} at ${TS}. Failure weight ${FAILURES}/${FAIL_THRESHOLD} — NOT restarting: an uncorroborated verdict is not grounds to destroy a control plane the whole fleet depends on." >&2
  write_state
  exit 0
fi

# ==========================================================================
# CORROBORATED — it has failed enough, consecutively, to act.
# ==========================================================================

# --- GUARD (b): is it ALREADY coming back? --------------------------------
# Independent of our own backoff (which a lost or corrupt state file would
# forfeit): if systemd says the unit is `activating`, something is already
# restarting it. Restarting a restart is exactly the 26-second double-restart
# we are here to kill.
ACTIVE_STATE="$(unit_active_state)"
if [ "$ACTIVE_STATE" = "activating" ] || [ "$ACTIVE_STATE" = "deactivating" ]; then
  echo "sac-listen-health: ${LISTEN_UNIT} is already '${ACTIVE_STATE}' — standing down. It is coming back on its own, and restarting a restart is what produced the 2026-07-14 double-restart." >&2
  write_state
  exit 0
fi

# --- RATE LIMIT -----------------------------------------------------------
prune_restarts "$((NOW - RESTART_WINDOW))"
RECENT="$(count_restarts)"

if [ "$RECENT" -ge "$MAX_RESTARTS" ]; then
  echo "ERROR: sac-listen DOWN — ${HEALTH_URL}: ${EVIDENCE}." >&2
  echo "ERROR: sac-listen watchdog is GIVING UP: ${RECENT} restart(s) already issued in the last ${RESTART_WINDOW}s (cap ${MAX_RESTARTS}) and ${LISTEN_UNIT} is STILL failing its health check." >&2
  echo "ERROR: NOT restarting again — if ${RECENT} restarts did not fix it, another will not either, and an unbounded restarter is how a fleet goes down at 3am. A HUMAN IS NEEDED." >&2
  echo "ERROR: incident-class=sac-listen-watchdog-giving-up; fleet a2a comms are DOWN and auto-heal is exhausted." >&2
  echo "ERROR: inspect with:  systemctl --user status ${LISTEN_UNIT}  &&  journalctl --user -u sac-listen -n 100" >&2
  # Alarm at most once per window: the journal must scream every cycle (this
  # IS an ongoing outage), but the operator's pager must not.
  if [ $((NOW - LAST_ALARM)) -ge "$RESTART_WINDOW" ]; then
    alarm "sac listen STILL DOWN after ${RECENT} restarts — watchdog GIVING UP, human needed" \
          "health probe to ${HEALTH_URL} failed: ${EVIDENCE} at ${TS}. ${RECENT} restart(s) in the last ${RESTART_WINDOW}s did not fix it (cap ${MAX_RESTARTS}). The watchdog has STOPPED restarting to avoid a restart storm. Fleet a2a comms are down. Inspect: systemctl --user status ${LISTEN_UNIT}"
    LAST_ALARM="$NOW"
  fi
  write_state
  exit 1
fi

# --- RESTART --------------------------------------------------------------
echo "ERROR: sac-listen DOWN — ${HEALTH_URL}: ${EVIDENCE}." >&2
echo "ERROR: verdict CORROBORATED (failure weight ${FAILURES}/${FAIL_THRESHOLD}; unit ActiveState=${ACTIVE_STATE}). This is not one slow probe." >&2
echo "ERROR: sac-listen watchdog is RESTARTING ${LISTEN_UNIT} at ${TS}." >&2
echo "ERROR: incident-class=sac-listen-watchdog-autorestart-alarm; fleet a2a comms were interrupted." >&2

alarm "sac listen DOWN — watchdog restarting ${LISTEN_UNIT}" \
      "health probe to ${HEALTH_URL} failed ${FAILURES}/${FAIL_THRESHOLD} (weighted, consecutive): ${EVIDENCE} at ${TS}. Fleet a2a comms interrupted. Auto-restart in progress (restart $((RECENT + 1))/${MAX_RESTARTS} within the last ${RESTART_WINDOW}s)."

# Record the attempt BEFORE issuing it: if the restart wedges, or this
# process is killed mid-flight, the backoff and the rate limit must STILL
# hold. An unrecorded restart is an unbounded restarter.
LAST_RESTART="$NOW"
RESTARTS="${RESTARTS:+${RESTARTS},}${NOW}"
FAILURES=0
SERVING_STREAK=0
write_state

restart_rc=0
if command -v systemctl >/dev/null 2>&1; then
  # `reset-failed` first so a tripped StartLimitBurst (the unit entered
  # `failed` after 5 restarts/60s) is cleared and `restart` can proceed —
  # otherwise systemd refuses with "start request repeated too quickly".
  systemctl --user reset-failed "$LISTEN_UNIT" >/dev/null 2>&1 || true
  if systemctl --user restart "$LISTEN_UNIT"; then
    echo "sac-listen-health: issued 'systemctl --user restart ${LISTEN_UNIT}'. Not probing again for ${RESTART_BACKOFF}s." >&2
  else
    restart_rc=$?
    echo "ERROR: sac-listen-health: 'systemctl --user restart ${LISTEN_UNIT}' FAILED (rc=${restart_rc})." >&2
  fi
else
  echo "ERROR: sac-listen-health: systemctl not found; cannot restart ${LISTEN_UNIT}." >&2
  restart_rc=1
fi

exit "$restart_rc"
