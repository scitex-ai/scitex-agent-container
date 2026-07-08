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

# Compute the SAC_SECRETS_ENVRC value to bake into the listen unit.
#   - Operator override: if SAC_SECRETS_ENVRC is already exported when the
#     installer runs, use it verbatim.
#   - Default: colon-join the operator's standardized scitex secret files,
#     globbed from ~/.bash.d/secrets/010_scitex/*.src (sorted).
# Prints nothing when neither yields a path (caller then OMITS the line).
secrets_envrc_value() {
  if [ -n "${SAC_SECRETS_ENVRC:-}" ]; then
    printf '%s' "$SAC_SECRETS_ENVRC"
    return 0
  fi
  local joined="" f
  # Sorted glob of the standardized secrets location; nullglob so a
  # no-match expands to nothing rather than the literal pattern. Glob
  # expansion preserves each path as one word (no word-splitting), and
  # bash already returns matches sorted.
  shopt -s nullglob
  for f in "$HOME"/.bash.d/secrets/010_scitex/*.src; do
    if [ -z "$joined" ]; then
      joined="$f"
    else
      joined="${joined}:${f}"
    fi
  done
  shopt -u nullglob
  printf '%s' "$joined"
}

# Idempotently refresh the `Environment=SAC_SECRETS_ENVRC=` line in the
# installed listen unit ($1). WHY: agents are (re)started by the
# systemd-user `sac-listen` daemon and by agent-managed restarts, which do
# NOT inherit the operator's interactive shell — so without this the
# deploy-time `.envrc` fold (src/scitex_agent_container/runtimes/_envrc.py)
# cannot resolve the real CCT_* tokens. Baking the secret-file list into the
# unit's environment lets _envrc.py source them and fold the live tokens.
# Always strips any prior occurrence first so re-running never stacks lines.
apply_secrets_envrc() {
  local unit="$1" value
  value="$(secrets_envrc_value)"
  # Drop any existing line (idempotency) before re-adding.
  sed -i '/^Environment=SAC_SECRETS_ENVRC=/d' "$unit"
  if [ -z "$value" ]; then
    log "# SAC_SECRETS_ENVRC: no override + no *.src files -> omitting Environment line"
    return 0
  fi
  # Insert after StandardError= (a stable anchor inside [Service]); fall
  # back to appending if that key is ever removed.
  if grep -q '^StandardError=' "$unit"; then
    sed -i '/^StandardError=/a Environment=SAC_SECRETS_ENVRC='"$value" "$unit"
  else
    printf 'Environment=SAC_SECRETS_ENVRC=%s\n' "$value" >> "$unit"
  fi
  log "# SAC_SECRETS_ENVRC=${value} -> baked into ${unit}"
}

# Resolve the absolute path to the `sac` binary to bake into the
# installed listen unit's ExecStart. WHY: systemd-user services do NOT
# inherit the operator's interactive shell PATH (no .bashrc/.bash_profile/
# venv-activation) — only the login-manager's default PATH. `sac` is
# typically installed into a per-project venv (e.g.
# .venv/bin/sac), which is never on that default PATH, so the repo
# template's generic `ExecStart=/usr/bin/env sac listen` resolves to
# nothing and the unit fails with exit 127 on first start (confirmed
# live 2026-07-05: an installed-but-never-started unit failed exactly
# this way on first install).
#   - Operator override: if SAC_BIN is already exported when the
#     installer runs, use it verbatim (mirrors the SAC_SECRETS_ENVRC
#     override in secrets_envrc_value above).
#   - Default: `command -v sac`, resolved in the INSTALLER's own shell
#     context — which, unlike systemd-user, DOES see the operator's
#     activated venv/PATH when this script is run interactively.
# UNLIKE secrets_envrc_value (which silently omits its line when
# nothing is found), this function FAILS LOUD: a listen unit with no
# resolvable sac binary must never be installed, since that guarantees
# a silent, unannounced exit-127 restart loop with no useful signal.
resolve_sac_bin() {
  if [ -n "${SAC_BIN:-}" ]; then
    printf '%s' "$SAC_BIN"
    return 0
  fi
  local resolved
  resolved="$(command -v sac || true)"
  if [ -z "$resolved" ]; then
    log "ERROR: sac not found on PATH — activate the venv this script" \
        "should run from (e.g. 'source .venv/bin/activate'), or pass" \
        "SAC_BIN=<absolute-path-to-sac> explicitly. Refusing to install" \
        "a listen unit with an unresolvable ExecStart (systemd-user does" \
        "not inherit your interactive shell PATH, so guessing here would" \
        "just move the exit-127 failure from install-time to boot-time)."
    exit 1
  fi
  printf '%s' "$resolved"
}

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

# 0. Resolve SAC_BIN FIRST, before installing/enabling anything. This is
#    a fail-loud step (see resolve_sac_bin above): an unresolvable sac
#    must abort the whole install, not land a broken unit that only
#    fails once systemd tries to start it.
SAC_BIN="$(resolve_sac_bin)"
log "# SAC_BIN=${SAC_BIN} -> will be baked into ${LISTEN_UNIT}'s ExecStart"

# 1. Copy the probe script first (the health .service ExecStart points
#    at the installed copy) and make it executable.
install -m 0755 "${SRC_DIR}/${PROBE_SCRIPT}" "${DEST_DIR}/${PROBE_SCRIPT}"
log "# Installed ${DEST_DIR}/${PROBE_SCRIPT}"

# 2. Copy the listen unit, rewriting the ExecStart line to the resolved
#    absolute SAC_BIN path (systemd-user does not inherit the
#    interactive shell PATH that `/usr/bin/env sac` in the repo
#    template relies on — see resolve_sac_bin's comment above). Same
#    sed-stream-rewrite technique as the health .service step below;
#    the repo template itself is left untouched (only the INSTALLED
#    copy gets the concrete path). The health timer copies verbatim.
sed "s#^ExecStart=.*#ExecStart=${SAC_BIN} listen#" \
    "${SRC_DIR}/${LISTEN_UNIT}" > "${DEST_DIR}/${LISTEN_UNIT}"
chmod 0644 "${DEST_DIR}/${LISTEN_UNIT}"
install -m 0644 "${SRC_DIR}/${HEALTH_TIMER}" "${DEST_DIR}/${HEALTH_TIMER}"
log "# Installed ${DEST_DIR}/${LISTEN_UNIT} (ExecStart -> ${SAC_BIN} listen)"
log "# Installed ${DEST_DIR}/${HEALTH_TIMER}"

# 2b. Bake SAC_SECRETS_ENVRC into the installed listen unit so the daemon
#     (and agent-managed restarts it spawns) folds the real CCT_* tokens via
#     runtimes/_envrc.py. Operates on the INSTALLED copy only (repo unit stays
#     clean) and is idempotent.
apply_secrets_envrc "${DEST_DIR}/${LISTEN_UNIT}"

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
# `enable --now` does NOT restart an already-running unit, so a re-run that
# refreshed the Environment=SAC_SECRETS_ENVRC line above would not take
# effect until the next crash. try-restart applies the new env immediately
# (and is a no-op when the unit isn't running).
systemctl --user try-restart "$LISTEN_UNIT" || true
log "# Enabled + (re)started ${LISTEN_UNIT} and ${HEALTH_TIMER}"

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
