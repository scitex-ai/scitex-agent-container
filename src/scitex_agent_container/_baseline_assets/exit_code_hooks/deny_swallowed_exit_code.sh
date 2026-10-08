#!/bin/bash
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_baseline_assets/exit_code_hooks/deny_swallowed_exit_code.sh
#
# Description: PreToolUse hook for Bash. Refuses commands that HIDE their
# real exit code: a pipeline without `set -o pipefail` (or an explicit
# `${PIPESTATUS}` check), and provable swallows -- `|| true`, `|| :`,
# `|| exit 0`, or a trailing `; true` / `; exit 0`.
#
# DECLARED BY scitex-agent-container (the leaf owns the rule about silent
# terminal failures), APPLIED BY scitex-dev through the `scitex_dev.hooks`
# entry-point group -- see the sibling README.md and
# ``scitex_agent_container._claude_hooks_plugin``. Do not hand-place this
# file; it is deployed to $HOME/.claude/hooks/pre-tool-use/.
#
# WHY THIS RULE EXISTS (operator order CCT 4552)
# ----------------------------------------------
# A pipeline's exit code is its LAST command's. `pytest -q | tail -5`
# reports success whenever `tail` succeeds -- even when every test failed.
# `cmd || true` converts failure into success unconditionally, so any
# automation keyed on the exit code (CI verdicts, retry loops, delivery
# gates) keys on the wrong signal. The failure does NOT fail loudly: it
# surfaces weeks later as a green run nobody can explain, or a "passing"
# gate that never gated anything. Terminal calls must surface their real
# exit code; pipe usage forces pipefail; nothing swallows silently.
#
# THE DISCRIMINATOR, AND WHY
# --------------------------
# Token-based, not regex-based, so quoted text cannot trigger it:
# `grep -E "a|b" file` and `echo "x || true"` are ALLOW (the `|` / `||`
# live inside quotes and are data, not shell). Only standalone operator
# tokens count. `case a|b)` alternations are exempt via case-depth tracking.
# An invalid ERE/quoting the tokenizer cannot parse fails OPEN -- a parser
# disagreement must never wedge the agent's Bash tool.
#
# NOT blocked, deliberately:
#   - `... | ...` under `set -o pipefail` (or `set -euo pipefail`)
#   - `... | ...` that reads `${PIPESTATUS[...]}` (explicit check)
#   - `cmd || <real handler>`: `|| echo`, `|| exit 1`, `|| die ...`
#   - `cmd && true` (does NOT swallow: `false && true` still exits 1)
#   - `! cmd` negation, `cmd 2>/dev/null` (exit code preserved)
#
# Bypass (rare -- know why you are doing it):
#   1. Append marker `hook-bypass: swallowed-exit-code` to the command.
#   2. Or export `SAC_ALLOW_SWALLOWED_EXIT_CODE=1`.

set -u

# ------------------------------------------------------------------
# Env-var escape
# ------------------------------------------------------------------
[[ "${SAC_ALLOW_SWALLOWED_EXIT_CODE:-}" == "1" ]] && exit 0

# ------------------------------------------------------------------
# Self-test -- the measured refuse/allow pairs, runnable at any time:
#   bash deny_swallowed_exit_code.sh --self-test
# ------------------------------------------------------------------
if [[ "${1:-}" == "--self-test" ]]; then
    echo "=== Self-test: $(basename "$0") ==="
    pass=0
    fail=0

    # Re-invoke through `bash "$SELF"`: $0 is whatever argv[0] the caller
    # used, and a bare relative name is not on PATH -- that spelling makes
    # every case exit 127 and the suite reports a uniform failure that
    # looks like a logic bug.
    SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

    run() {
        local desc="$1" cmd="$2" want="$3" rc
        printf '%s' "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":$(printf '%s' "$cmd" |
            python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))')}}" |
            bash "$SELF" >/dev/null 2>&1
        rc=$?
        if [[ "$rc" == "$want" ]]; then
            echo "  PASS (rc=$rc) $desc"
            pass=$((pass + 1))
        else
            echo "  FAIL got $rc want $want: $desc -- cmd: $cmd"
            fail=$((fail + 1))
        fi
    }

    echo "-- REFUSED: pipelines that hide mid-pipe failure --"
    run "bare pipe" \
        "pytest tests/ -q | tail -5" 2
    run "tee pipe" \
        "make build 2>&1 | tee build.log" 2
    run "multi-stage pipe" \
        "cat log | grep ERROR | head -20" 2

    echo "-- REFUSED: provable exit-code swallows --"
    run "or-true" \
        "grep foo file || true" 2
    run "or-true no spaces" \
        "grep foo file||true" 2
    run "or-colon" \
        "grep foo file || :" 2
    run "or-exit-zero" \
        "grep foo file || exit 0" 2
    run "or-abs-true" \
        "grep foo file || /bin/true" 2
    run "trailing semicolon true" \
        "run_tests; true" 2
    run "trailing semicolon exit 0" \
        "run_tests; exit 0" 2
    run "subshell swallow" \
        "(build || true)" 2

    echo "-- ALLOWED: exit codes stay visible --"
    run "pipefail prefix" \
        "set -o pipefail; pytest -q | tail -5" 0
    run "strict-mode prefix" \
        "set -euo pipefail; make | tee log" 0
    run "explicit PIPESTATUS check" \
        "pytest -q | tail -5; echo PIPESTATUS=\${PIPESTATUS[0]}" 0
    run "real or-handler" \
        "grep foo file || echo 'not found'" 0
    run "or-exit-nonzero propagates" \
        "grep foo file || exit 1" 0
    run "plain sequence" \
        "run_tests; echo done" 0
    run "and-true does not swallow" \
        "run_tests && true" 0
    run "quoted alternation is data" \
        "grep -E \"a|b\" file" 0
    run "quoted or-true is data" \
        "echo \"x || true\"" 0
    run "case alternation" \
        "case \$x in a|b) echo hit;; esac" 0
    run "stderr redirect preserves code" \
        "run_tests 2>/dev/null" 0
    run "no pipe, no swallow" \
        "ls -la" 0

    echo "-- BYPASS --"
    run "marker bypass" \
        "pytest -q | tail -5 # hook-bypass: swallowed-exit-code" 0
    SAC_ALLOW_SWALLOWED_EXIT_CODE=1 run "env-var bypass" \
        "grep foo file || true" 0

    echo "pass=$pass fail=$fail"
    [[ $fail -eq 0 ]] && exit 0 || exit 1
fi

# ------------------------------------------------------------------
# Enablement switch (centralized project-switch/switch.yaml), when present.
# ------------------------------------------------------------------
THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER_SCRIPT="$(dirname "$THIS_DIR")/project-switch/hook_switch_helper.sh"
if [[ -f "$HELPER_SCRIPT" ]]; then
    # shellcheck source=/dev/null
    source "$HELPER_SCRIPT"
    if declare -f check_hook_enabled_or_exit >/dev/null 2>&1; then
        check_hook_enabled_or_exit "$(basename "$0")"
    fi
fi

# ------------------------------------------------------------------
# Read input + extract command (Bash tool only). Fail OPEN throughout:
# a broken guard must not wedge the agent's Bash tool.
# ------------------------------------------------------------------
INPUT="$(cat)"

CMD=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if d.get("tool_name", "") != "Bash":
    sys.exit(0)
print((d.get("tool_input", {}) or {}).get("command", "") or "", end="")
' 2>/dev/null) || exit 0
[[ -z "$CMD" ]] && exit 0

# ------------------------------------------------------------------
# String-marker escape
# ------------------------------------------------------------------
if printf '%s' "$CMD" | grep -qF 'hook-bypass: swallowed-exit-code'; then
    exit 0
fi

# ------------------------------------------------------------------
# Decide. Fail OPEN: if python3 is missing or the engine errors, allow.
# Prints `<reason>|<evidence>` and exits 2 when the command must be refused.
# ------------------------------------------------------------------
VERDICT="$(printf '%s' "$CMD" | python3 -c '
import re
import shlex
import sys

CMD = sys.stdin.read()

# Tokenize with shell punctuation so a REAL pipe surfaces as a standalone
# `|` token while quoted text (`grep -E "a|b"`) stays one data token.
# `||` lexes as its own single token (verified), never as two pipes.
def tokens(text):
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
        lex.commenters = ""
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return text.split()

TRUE_WORDS = ("true", ":", "/bin/true", "/usr/bin/true")

def swallowed(toks):
    """Provable exit-code swallows. Returns evidence string or None."""
    for i, tok in enumerate(toks):
        if tok != "||" or i + 1 >= len(toks):
            continue
        nxt = toks[i + 1]
        if nxt in TRUE_WORDS:
            return "|| " + nxt
        if nxt == "exit" and i + 2 < len(toks) and toks[i + 2] == "0":
            return "|| exit 0"
    # Trailing `; true` / `; :` / `; exit 0` pins the whole command to 0.
    tail = [t for t in toks if t not in ("", "\n")]
    while tail and tail[-1] in (";", "&"):
        tail.pop()
    if len(tail) >= 2 and tail[-2] == ";" and tail[-1] in TRUE_WORDS:
        return "; " + tail[-1]
    if len(tail) >= 3 and tail[-3] == ";" and tail[-2] == "exit" and tail[-1] == "0":
        return "; exit 0"
    return None

def bare_pipe(toks):
    """A standalone `|` outside `case...esac` alternations."""
    depth = 0
    for tok in toks:
        if tok == "case":
            depth += 1
        elif tok == "esac":
            depth = max(0, depth - 1)
        elif tok == "|" and depth == 0:
            return True
    return False

try:
    toks = tokens(CMD)
except Exception:
    sys.exit(0)

hit = swallowed(toks)
if hit:
    print("swallow|" + hit)
    sys.exit(2)

if "pipefail" not in CMD and "PIPESTATUS" not in CMD and bare_pipe(toks):
    print("pipe||")
    sys.exit(2)

sys.exit(0)
' 2>/dev/null)"
[[ $? == 2 ]] || exit 0
[[ -n "$VERDICT" ]] || exit 0

REASON="${VERDICT%%|*}"
EVIDENCE="${VERDICT#*|}"

LOG_PATH="${SCITEX_AGENT_CONTAINER_HOOK_LOG_PATH:-$THIS_DIR/.$(basename "$0").log}"
printf '[%s] BLOCK %s :: %s :: %s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$REASON" "$EVIDENCE" "$CMD" \
    >>"$LOG_PATH" 2>/dev/null || true

if [[ "$REASON" == "pipe" ]]; then
    cat >&2 <<EOF
BLOCKED by deny_swallowed_exit_code.sh: pipeline without \`set -o pipefail\`.

A pipeline's exit code is its LAST command's -- \`pytest -q | tail -5\`
succeeds whenever \`tail\` succeeds, even when every test failed. The
mid-pipe failure does not fail loudly; it vanishes.

Prefix the command so every stage's failure surfaces:

  set -o pipefail; <your pipeline>

or check stages explicitly with \${PIPESTATUS[...]} after the pipeline.
(CCT 4552: terminal calls must surface real exit codes; pipe usage
forces pipefail.)

Rare override (know why you are doing it):
  SAC_ALLOW_SWALLOWED_EXIT_CODE=1   or append   # hook-bypass: swallowed-exit-code
EOF
else
    cat >&2 <<EOF
BLOCKED by deny_swallowed_exit_code.sh: swallowed exit code ($EVIDENCE).

\`|| true\` / \`|| :\` / \`|| exit 0\` (and a trailing \`; true\`) convert
failure into success unconditionally, so anything keyed on the exit code
-- CI verdicts, retry loops, delivery gates -- keys on the wrong signal.
Handle the failure explicitly instead:

  cmd || exit 1          # propagate
  cmd || echo "reason"   # record, then decide

(CCT 4552: no silent swallowing -- terminal calls surface real exit codes.)

Rare override (know why you are doing it):
  SAC_ALLOW_SWALLOWED_EXIT_CODE=1   or append   # hook-bypass: swallowed-exit-code
EOF
fi

exit 2

# EOF
