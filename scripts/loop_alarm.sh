#!/usr/bin/env bash
# Loop detector: tell the operator when an agent is spinning, not working.
#
# WHY THIS EXISTS (2026-09-20 incident): six agents ran ~11 hours emitting
#   Hermes heartbeat projection failed: ... malformed event ...
# at ~0.4/s each. Every symptom was present and none was visible: the processes
# were alive, their heartbeats never projected, the registry said "stopped", and
# NOBODY WAS TOLD. The absence of this alarm is half of that incident - the code
# bug alone would have been a five-minute outage if anything had shouted.
#
# THE SIGNAL: not the text of any line, but that a log is REPEATING. A healthy
# agent writes varied lines; a wedged one writes the same line thousands of
# times. So the check is "how many lines, how many DISTINCT lines" and the alarm
# is the RATIO, which is independent of what the loop happens to say.
#
# Thresholds are deliberately conservative - this must be silent when healthy,
# or it becomes noise the operator learns to ignore, which is the failure mode
# that produced the original silence.
set -uo pipefail

AGENTS_ROOT="${SAC_AGENTS_RUNTIME_ROOT:-$HOME/.scitex/agent-container/runtime}"
MIN_LINES="${LOOP_ALARM_MIN_LINES:-2000}"   # below this, a short log is just a short log
MIN_RATIO="${LOOP_ALARM_MIN_RATIO:-50}"    # lines per distinct line
FAILED=0
REPORT=""

for dir in "$AGENTS_ROOT"/*/; do
  [ -d "$dir" ] || continue
  agent="$(basename "$dir")"
  for log in "$dir"boot.stderr.log "$dir"boot.stdout.log; do
    [ -f "$log" ] || continue
    lines=$(wc -l < "$log" 2>/dev/null || echo 0)
    [ "$lines" -lt "$MIN_LINES" ] && continue
    distinct=$(sort -u "$log" 2>/dev/null | wc -l)
    [ "$distinct" -lt 1 ] && distinct=1
    ratio=$(( lines / distinct ))
    if [ "$ratio" -ge "$MIN_RATIO" ]; then
      FAILED=1
      # Name the offending line once, truncated: the operator needs to recognise
      # it, not read all 10,000 copies of it.
      sample=$(sort "$log" 2>/dev/null | uniq -c | sort -rn | head -1 | cut -c1-160)
      REPORT="${REPORT}${agent}: ${lines} lines / ${distinct} distinct (${ratio}x) - ${sample}"$'\n'
    fi
  done
done

if [ "$FAILED" -ne 0 ]; then
  printf 'SAC LOOP ALARM: agent log(s) repeating, which means the agent is spinning and NOT working\n\n'
  printf '%s' "$REPORT"
  printf '\nThis is the 2026-09-20 incident shape: processes alive, heartbeats not projecting, nothing visible.\n'
  exit 1
fi

# Silence when healthy, deliberately: this script is the alarm, and an alarm
# that reports "all good" every 30 minutes is one the operator stops reading.
exit 0
