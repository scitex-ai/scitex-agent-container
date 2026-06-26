#!/usr/bin/env bash
# install-sac-listen.sh — install the sac listen control-plane unit AND
# its health watchdog into the systemd-user manager.
#
# Installs, into ~/.config/systemd/user/:
#   sac-listen.service          the control plane (Restart=always)
#   sac-listen-health.service   the oneshot health probe
#   sac-listen-health.timer     fires the probe every ~30s
#   sac-listen-health-probe.sh  the probe script the .service runs
#
# Then daemon-reload, enable --now both the listen and the timer, and
# print status. Idempotent: re-running re-copies the latest unit text
# and re-enables.
#
# Incident 2026-06-26: the listen died with NO unit installed (nothing
# restarted it) and NO alarm. Running this script is what guarantees a
# restart + a LOUD alarm on the next failure.
#
# Usage:
#   scripts/systemd/install-sac-listen.sh            # install + enable + status
#   scripts/systemd/install-sac-listen.sh --uninstall  # disable + remove
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

LISTEN_UNIT="sac-listen.service"
HEALTH_SERVICE="sac-listen-health.service"
HEALTH_TIMER="sac-listen-health.timer"
PROBE_SCRIPT="sac-listen-health-probe.sh"

log() { printf '%s\n' "$*" >&2; }

uninstall() {
  log "# Disabling + removing sac listen units from ${DEST_DIR}"
  systemctl --user disable --now "$HEALTH_TIMER" >/dev/null 2>&1 || true
  systemctl --user disable --now "$LISTEN_UNIT" >/dev/null 2>&1 || true
  rm -f "${DEST_DIR}/${LISTEN_UNIT}" \
        "${DEST_DIR}/${HEALTH_SERVICE}" \
        "${DEST_DIR}/${HEALTH_TIMER}" \
        "${DEST_DIR}/${PROBE_SCRIPT}"
  systemctl --user daemon-reload
  log "# Removed. Verify: systemctl --user list-unit-files 'sac-listen*'"
}

if [ "${1:-}" = "--uninstall" ]; then
  uninstall
  exit 0
fi

if [ "${1:-}" != "" ]; then
  log "usage: $0 [--uninstall]"
  exit 2
fi

mkdir -p "$DEST_DIR"

# 1. Copy the probe script first (the health .service ExecStart points
#    at the installed copy) and make it executable.
install -m 0755 "${SRC_DIR}/${PROBE_SCRIPT}" "${DEST_DIR}/${PROBE_SCRIPT}"
log "# Installed ${DEST_DIR}/${PROBE_SCRIPT}"

# 2. Copy the listen unit and the health timer verbatim.
install -m 0644 "${SRC_DIR}/${LISTEN_UNIT}" "${DEST_DIR}/${LISTEN_UNIT}"
install -m 0644 "${SRC_DIR}/${HEALTH_TIMER}" "${DEST_DIR}/${HEALTH_TIMER}"
log "# Installed ${DEST_DIR}/${LISTEN_UNIT}"
log "# Installed ${DEST_DIR}/${HEALTH_TIMER}"

# 3. Copy the health .service, rewriting the ExecStart %h-relative path
#    to the concrete installed probe path. %h works at runtime too, but
#    pinning the absolute path avoids any ambiguity if XDG_CONFIG_HOME
#    is non-default.
sed "s#^ExecStart=.*#ExecStart=${DEST_DIR}/${PROBE_SCRIPT}#" \
    "${SRC_DIR}/${HEALTH_SERVICE}" > "${DEST_DIR}/${HEALTH_SERVICE}"
chmod 0644 "${DEST_DIR}/${HEALTH_SERVICE}"
log "# Installed ${DEST_DIR}/${HEALTH_SERVICE} (ExecStart -> ${DEST_DIR}/${PROBE_SCRIPT})"

# 4. Reload, enable + start both the listen and the watchdog timer.
systemctl --user daemon-reload
systemctl --user enable --now "$LISTEN_UNIT"
systemctl --user enable --now "$HEALTH_TIMER"
log "# Enabled + started ${LISTEN_UNIT} and ${HEALTH_TIMER}"

# 5. Status for the operator.
log ""
log "# ---- status: ${LISTEN_UNIT} ----"
systemctl --user status "$LISTEN_UNIT" --no-pager || true
log ""
log "# ---- timer: ${HEALTH_TIMER} ----"
systemctl --user list-timers "$HEALTH_TIMER" --no-pager || true
log ""
log "# Health endpoint:"
log "#   curl -s http://127.0.0.1:7878/v1/health"
log "# Watchdog journal (LOUD restart/alarm lines land here):"
log "#   journalctl --user -u ${HEALTH_SERVICE} -n 50"
