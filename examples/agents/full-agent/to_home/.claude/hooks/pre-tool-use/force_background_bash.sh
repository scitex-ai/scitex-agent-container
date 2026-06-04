#!/bin/bash
# -*- coding: utf-8 -*-
# Timestamp: "2026-06-04 (proj-scitex-agent-container)"
# File: examples/agents/full-agent/to_home/.claude/hooks/pre-tool-use/force_background_bash.sh
#
# Description: Force foreground Bash into the background for SAC agents
# so the conversation runner's main turn stays SHORT and inbound
# operator Telegram is answered within seconds — exactly how the lead
# stays responsive while working.
#
# Foreground Bash is allowed ONLY if BOUNDED:
#   * run_in_background: true  ─ primary; SDK delivers a completion
#                                notification on a later turn.
#   * explicit detach          ─ trailing `&`, OR contains
#                                `nohup`/`setsid`/`disown`.
#   * `timeout <=7[s]`         ─ wrapped with a 1–7 second timeout.
#   * short trivial check      ─ <=50 chars AND no pipe/redirect/chain
#                                AND first token is NOT in the known
#                                long-runner set.
# Everything else (pytest / pdflatex / make / long pipes / training
# / unbounded jobs) is BLOCKED with a WHY message instructing the
# agent to relaunch via one of the background mechanisms.
#
# Policy mirrored verbatim from the lead's _base/to_home copy
# (dotfiles commit ac582483); the role-agnostic complement to the
# coordinator-only `enforce_delegation.sh` and the Agent/Task-tool
# counterpart `force_background_agents_always.sh`.
#
# Escape: CC_ALLOW_FOREGROUND_HEAVY=1
# Doctrine: ~/.claude/skills/scitex/scitex-agent-container/30_responsiveness-background-work.md

set -u

SHORT_CMD_MAX_LEN=50
LONG_RE='(^|[[:space:]/;&|(])(pytest|py\.test|pdflatex|xelatex|lualatex|latexmk|tectonic|make|cmake|ninja|sphinx-build|nbconvert|jupyter|cargo|npm|yarn|pnpm|sleep)([[:space:]]|$)'
TIMEOUT_RE='(^|[;&|][[:space:]]*|&&[[:space:]]*)timeout[[:space:]]+(-[^[:space:]]+[[:space:]]+)*[1-7]s?([[:space:]]|$)'

# --- self-test -------------------------------------------------------------
if [[ "${1:-}" == "--self-test" ]]; then
    echo "=== Self-test: $(basename "$0") ==="
    pass=0
    fail=0
    run() {
        local desc="$1" payload="$2" want="$3" rc env_prefix="${4:-}"
        if [[ -n "$env_prefix" ]]; then
            rc=$(env $env_prefix bash -c "printf '%s' \"\$0\" | \"$0\" >/dev/null 2>&1; echo \$?" "$payload")
        else
            printf '%s' "$payload" | "$0" >/dev/null 2>&1
            rc=$?
        fi
        if [[ "$rc" == "$want" ]]; then
            echo "  PASS ($rc) $desc"
            pass=$((pass + 1))
        else
            echo "  FAIL got $rc want $want: $desc"
            fail=$((fail + 1))
        fi
    }

    # ---- BLOCK (10) — the lead's verbatim cases ---------------------------
    run "pytest tests/ -x" '{"tool_name":"Bash","tool_input":{"command":"pytest tests/ -x"}}' 2
    run "pdflatex paper.tex" '{"tool_name":"Bash","tool_input":{"command":"pdflatex paper.tex"}}' 2
    run "make -C builddir" '{"tool_name":"Bash","tool_input":{"command":"make -C builddir"}}' 2
    run "tectonic manuscript.tex" '{"tool_name":"Bash","tool_input":{"command":"tectonic manuscript.tex"}}' 2
    run "find with pipe" '{"tool_name":"Bash","tool_input":{"command":"find /work -name '"'"'*.py'"'"' | head -20"}}' 2
    run "install && pytest" '{"tool_name":"Bash","tool_input":{"command":"uv pip install -e .[all] && python -m pytest -q"}}' 2
    run "timeout 60 pytest" '{"tool_name":"Bash","tool_input":{"command":"timeout 60 pytest tests/"}}' 2
    run "timeout 7m pytest" '{"tool_name":"Bash","tool_input":{"command":"timeout 7m pytest tests/"}}' 2
    run "sleep 30" '{"tool_name":"Bash","tool_input":{"command":"sleep 30"}}' 2
    run "long npm install" '{"tool_name":"Bash","tool_input":{"command":"npm install --no-audit --legacy-peer-deps"}}' 2

    # ---- ALLOW (10) — the lead's verbatim cases ---------------------------
    run "timeout 7 pytest" '{"tool_name":"Bash","tool_input":{"command":"timeout 7 pytest tests/ -x"}}' 0
    run "timeout 7s pdflatex" '{"tool_name":"Bash","tool_input":{"command":"timeout 7s pdflatex paper.tex"}}' 0
    run "timeout -k 1 5 make" '{"tool_name":"Bash","tool_input":{"command":"timeout -k 1 5 make -C builddir"}}' 0
    run "pytest run_in_background" '{"tool_name":"Bash","tool_input":{"command":"pytest tests/ -x","run_in_background":true}}' 0
    run "setsid nohup pdflatex &" '{"tool_name":"Bash","tool_input":{"command":"setsid nohup pdflatex paper.tex >/tmp/x.log 2>&1 &"}}' 0
    run "make detached &" '{"tool_name":"Bash","tool_input":{"command":"make all >/tmp/b.log 2>&1 &"}}' 0
    run "pwd" '{"tool_name":"Bash","tool_input":{"command":"pwd"}}' 0
    run "date" '{"tool_name":"Bash","tool_input":{"command":"date"}}' 0
    run "git -C /work status -s" '{"tool_name":"Bash","tool_input":{"command":"git -C /work status -s"}}' 0
    run "ls -la" '{"tool_name":"Bash","tool_input":{"command":"ls -la"}}' 0

    # ---- Additional invariants (non-Bash, escape hatch) -------------------
    run "Read (non-Bash)" '{"tool_name":"Read","tool_input":{"file_path":"/etc/hosts"}}' 0
    run "escape hatch on pytest" '{"tool_name":"Bash","tool_input":{"command":"pytest tests/"}}' 0 'CC_ALLOW_FOREGROUND_HEAVY=1'

    # ---- Numeric tool_input.timeout (enforce_delegation-style) ------------
    # Allow when the Bash tool's own `timeout` param is <=7000ms; block
    # when it's higher or missing for a heavy command.
    run "pytest with tool timeout 5000" \
        '{"tool_name":"Bash","tool_input":{"command":"pytest tests/","timeout":5000}}' 0
    run "pdflatex with tool timeout 7000" \
        '{"tool_name":"Bash","tool_input":{"command":"pdflatex paper.tex","timeout":7000}}' 0
    run "pytest with tool timeout 15000" \
        '{"tool_name":"Bash","tool_input":{"command":"pytest tests/","timeout":15000}}' 2
    run "pytest with tool timeout 0" \
        '{"tool_name":"Bash","tool_input":{"command":"pytest tests/","timeout":0}}' 2

    echo "pass=$pass fail=$fail"
    [[ $fail -eq 0 ]] && exit 0 || exit 1
fi

# --- entry -----------------------------------------------------------------

# Escape hatch via env var (rare — when the caller KNOWS the command
# is sub-second despite hitting a long-runner name).
[[ "${CC_ALLOW_FOREGROUND_HEAVY:-}" == "1" ]] && exit 0

input=$(cat)

tool_name=$(printf '%s' "$input" | python3 -c '
import sys, json
try:
    print(json.load(sys.stdin).get("tool_name", ""))
except Exception:
    print("")
' 2>/dev/null)

[[ "$tool_name" != "Bash" ]] && exit 0

cmd=$(printf '%s' "$input" | python3 -c '
import sys, json
try:
    print(json.load(sys.stdin).get("tool_input", {}).get("command", "") or "")
except Exception:
    print("")
' 2>/dev/null)

run_bg=$(printf '%s' "$input" | python3 -c '
import sys, json
try:
    v = json.load(sys.stdin).get("tool_input", {}).get("run_in_background", False)
    print("True" if v is True else "False")
except Exception:
    print("False")
' 2>/dev/null)

# Bash tool's `timeout` parameter (milliseconds, JSON field — NOT the
# shell `timeout` command). Bounded to <=7000ms is an allowed
# foreground variant. Lead asked for this enforce_delegation-style
# numeric parse because it's structured data — more robust than a
# regex on the command text.
tool_timeout=$(printf '%s' "$input" | python3 -c '
import sys, json
try:
    v = json.load(sys.stdin).get("tool_input", {}).get("timeout", None)
    if isinstance(v, (int, float)):
        print(int(v))
    else:
        print(-1)
except Exception:
    print(-1)
' 2>/dev/null)

# Allow path 1: run_in_background=true.
[[ "$run_bg" == "True" ]] && exit 0

# Allow path 2: explicit detach (& at end, nohup, setsid, disown).
if printf '%s' "$cmd" | grep -qE '&[[:space:]]*$|nohup|setsid|disown'; then
    exit 0
fi

# Allow path 3a: Bash tool's `timeout` param <= 7000ms (numeric).
if [[ "$tool_timeout" != "-1" ]] && [[ "$tool_timeout" -ge 1 ]] && [[ "$tool_timeout" -le 7000 ]] 2>/dev/null; then
    exit 0
fi

# Allow path 3b: shell `timeout [1-7][s]?` wrapper (regex fallback).
if printf '%s' "$cmd" | grep -qE "$TIMEOUT_RE"; then
    exit 0
fi

# Allow path 4: short trivial — short, no pipe/redirect/chain, not a
# known long-runner.
if [[ ${#cmd} -le $SHORT_CMD_MAX_LEN ]] \
    && ! printf '%s' "$cmd" | grep -qE '[|<>]|&&|;' \
    && ! printf '%s' "$cmd" | grep -qE "$LONG_RE"; then
    exit 0
fi

# --- block -----------------------------------------------------------------
log_file="${HOME}/.claude/hooks/pre-tool-use/.force_background_bash.sh.log"
mkdir -p "$(dirname "$log_file")" 2>/dev/null || true
printf '[%s] BLOCK unbounded foreground Bash: cmd=%q\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$cmd" >>"$log_file" 2>/dev/null || true

cat >&2 <<EOF
BLOCKED by force_background_bash.sh: unbounded foreground Bash command.
Only BACKGROUND (or <=7s-bounded) Bash is allowed.

cmd: ${cmd:0:200}

WHY THIS IS BLOCKED (operator's #1 fleet UX rule, 2026-06-04):

  Your conversation runner reads ONE inbox sequentially. While THIS
  turn is busy with a long Bash, every inbound Telegram message from
  the operator queues behind it and is NOT processed until the Bash
  finishes. A 4-minute compile = a 4-minute Telegram silence — the
  operator hits this repeatedly and loses trust in the agent.

  Fix: keep your main loop FREE so you can answer the operator within
  SECONDS, exactly like the lead does. The work itself is NOT
  interrupted; it CONTINUES off the main loop, and you handle the
  result when the runtime delivers the completion notification.

  Operator wording (8843 / 8845): "作業中断はしてほしくない" — don't
  interrupt the work; just keep the main loop free.

Do ONE of:

  (a) Bash(..., run_in_background=true)     ← primary; 95% of cases.
        Pure shell command, SDK delivers a <task-notification> with
        stdout/stderr on a later turn.

  (b) setsid nohup <cmd> >/tmp/job.log 2>&1 </dev/null &
        ← explicit detach; survives agent restart. Tail the log on a
          later turn.

  (c) Task / Agent(..., run_in_background=true)
        ← for genuine MULTI-STEP delegated work only (research,
          audit, code review). Don't spawn a subagent just to run
          pytest — use (a).

  (d) timeout 7 <cmd>
        ← if the command truly finishes in <=7 seconds, just bound
          it. (\`timeout 7\` and \`timeout 7s\` both work.)

Then END YOUR TURN promptly. Do not stay in this turn watching the
background job — the runtime wakes you on completion.

Short trivial checks (<=50 chars, no pipe/redirect/chain, not a
build/test/training command) are always allowed in the foreground —
e.g. \`pwd\`, \`date\`, \`git -C /work status -s\`, \`ls -la\`.

Bypass (rare — only when you KNOW it is sub-second despite the
heavy pattern, e.g. \`pytest --version\`):
  set in the shell: \`CC_ALLOW_FOREGROUND_HEAVY=1\`

Doctrine: ~/.claude/skills/scitex/scitex-agent-container/30_responsiveness-background-work.md
EOF
exit 2
