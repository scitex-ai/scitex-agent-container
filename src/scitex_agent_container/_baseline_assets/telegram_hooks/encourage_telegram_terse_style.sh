#!/bin/bash
# -*- coding: utf-8 -*-
# Timestamp: "2026-06-09 (OP-PRIO-FMT rule 5)"
# File: ~/.claude/hooks/pre-tool-use/encourage_telegram_terse_style.sh
#
# OP-PRIO-FMT rule 5 (operator 2026-06-09): prefer the terse
# ``します`` / ``しました`` style. Messages should read as concise
# INTENT (will do) or completed FACT (done) — not as rambling
# narration. This rule is SUBJECTIVE — Japanese sentence structure
# admits too many valid endings to mechanically demand only ``する``
# system verbs. Instead this hook acts as a STRONG REMINDER: it
# emits a stderr nudge but always exits 0 (non-blocking) so legit
# variants pass while the agent stays aware of the convention.
#
# Heuristic: a single sentence > 35 chars whose end (last 15
# chars) does NOT contain a ``する/した/します/しました`` ending,
# nor a terse English sentence verb (did/done/opened/merged), gets
# the nudge. JP chars are visually denser than ASCII so 35 is a
# realistic floor for "long enough to deserve a terse closer".
#
# Fires on: tool_name matches the matcher in settings.local.json
# (must be the FQ mcp__claude-code-telegrammer__reply name — operator
# 2026-06-09 OP-PRIO-2 matcher fix is a hard prerequisite).
# FAIL-OPEN on any parse error or unexpected payload shape. ALWAYS
# exits 0 — this hook is a reminder, not a blocker.
# Escape: CC_ALLOW_NON_TERSE=1 silences the reminder entirely.

set -u
[[ "${CC_ALLOW_NON_TERSE:-}" == "1" ]] && exit 0

# The nudge SENTINEL — single source of truth. The hook body prefixes
# its stderr message with it; --self-test greps for it. Exported so the
# embedded python reads THE SAME string; nothing else may hardcode it.
#
# WHY A SENTINEL, AND NOT "stderr is non-empty":
# this hook is nudge-only (it always exits 0), so the exit code carries
# no verdict and stderr is the only observable. But stderr is a SHARED
# channel — a broken .pth, a DeprecationWarning, a locale complaint all
# land there, and some of them print before this hook's own code runs.
# "stderr is non-empty" therefore measures AMBIENT NOISE, not the nudge:
# it passes when unrelated noise appears and fails when the hook nudges
# quietly. Measured 2026-08 on a venv whose subprocess-coverage .pth
# raised ModuleNotFoundError at interpreter startup: the SAME script
# reported pass=1 fail=5 under /opt/venv-sac and pass=6 fail=0 under
# /usr/bin/python3. Matching this sentinel makes the assertion fail for
# the right reason and pass for the right reason. Do not regress it to
# a length/emptiness test.
TERSE_NUDGE_MARKER="REMINDER from encourage_telegram_terse_style.sh"
export TERSE_NUDGE_MARKER

if [[ "${1:-}" == "--self-test" ]]; then
    echo "=== Self-test: $(basename "$0") ==="
    pass=0
    fail=0
    # run <desc> <tool> <text> <want_nudge> [noise]
    #
    # Reminder hook: always rc=0, so we pin the rc=0 contract AND we pin
    # whether stderr carries THIS hook's sentinel.
    #
    # [noise] (optional) is synthetic stderr written into the SAME
    # captured stream as the hook's own stderr, before the hook runs.
    # The verdict must not move when it is present — that is the
    # permanent regression control for the emptiness-test bug.
    run() {
        local desc="$1" tool="$2" text="$3" want_nudge="$4" noise="${5:-}"
        local json payload stderr rc has_nudge
        json=$(printf '%s' "$text" \
            | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))' 2>/dev/null)
        if [[ -z "$json" ]]; then
            # The harness could not encode the case; say so rather than
            # letting a malformed payload fail-open into a false PASS.
            echo "  FAIL harness: could not JSON-encode case text: $desc"
            fail=$((fail+1)); return
        fi
        payload="{\"tool_name\":\"$tool\",\"tool_input\":{\"chat_id\":\"1\",\"text\":$json}}"
        if [[ -n "$noise" ]]; then
            stderr=$( { printf '%s\n' "$noise" >&2; printf '%s' "$payload" | "$0"; } 2>&1 >/dev/null )
            rc=$?
        else
            stderr=$(printf '%s' "$payload" | "$0" 2>&1 >/dev/null)
            rc=$?
        fi
        # THE assertion: did THIS hook nudge? -> is THIS hook's sentinel
        # present? Never "is stderr non-empty".
        has_nudge=0
        grep -Fq -- "$TERSE_NUDGE_MARKER" <<<"$stderr" && has_nudge=1
        if [[ "$rc" == "0" && "$has_nudge" == "$want_nudge" ]]; then
            echo "  PASS rc=$rc nudge=$has_nudge $desc"; pass=$((pass+1))
        else
            echo "  FAIL rc=$rc nudge=$has_nudge want_nudge=$want_nudge: $desc"; fail=$((fail+1))
        fi
    }
    T="mcp__claude-code-telegrammer__reply"
    RAMBLE="色々と調べた結果、結局のところよくわからなかったので、後で確認する予定です、たぶん"
    # A verbatim-shaped sample of the ambient noise that broke the old
    # emptiness test. Its exact text is irrelevant — that is the point.
    NOISE=$'Error processing line 1 of site-packages/_scitex_dev_subprocess_coverage.pth:\nTraceback (most recent call last):\n  File "<frozen site>", line 206, in addpackage\nModuleNotFoundError: No module named coverage'
    run "terse JP します ending             -> no nudge" "$T" "PR #343 opened します" 0
    run "terse JP しました ending           -> no nudge" "$T" "rename commit しました" 0
    run "terse EN done                      -> no nudge" "$T" "merge done" 0
    run "long rambling JP no terse ending   -> NUDGE"   "$T" "$RAMBLE" 1
    run "short non-terse                    -> no nudge" "$T" "hi" 0
    run "non-telegram tool ignored          -> no nudge" "Bash" "rambling rambling rambling rambling rambling rambling rambling" 0
    run "empty text                         -> no nudge" "$T" "" 0
    # --- noise controls: same cases, ambient stderr noise injected. ---
    run "NOISY long rambling JP             -> NUDGE"   "$T" "$RAMBLE" 1 "$NOISE"
    run "NOISY terse JP しました            -> no nudge" "$T" "rename commit しました" 0 "$NOISE"
    run "NOISY non-telegram tool ignored    -> no nudge" "Bash" "rambling rambling rambling rambling rambling rambling rambling" 0 "$NOISE"
    echo "pass=$pass fail=$fail"
    [[ "$fail" == "0" ]] && exit 0 || exit 1
fi

exec python3 -c '
import json
import os
import re
import sys

# Single source of truth for the nudge sentinel — set + exported by the
# bash wrapper above. Read eagerly (no default): if a future edit drops
# the export, EVERY self-test case fails loudly on the rc=0 pin rather
# than the sentinel silently drifting out of sync with the assertion.
MARKER = os.environ["TERSE_NUDGE_MARKER"]

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
tool = data.get("tool_name", "")
if "claude-code-telegrammer__reply" not in tool:
    sys.exit(0)
text = (data.get("tool_input", {}) or {}).get("text", "") or ""
if not text:
    sys.exit(0)
# Heuristic: long sentences (> 35 chars) whose tail (last 15 chars)
# carries no terse ending get the nudge. Tail tokens we accept:
TERSE_TAILS = (
    "します", "しました", "ました", "した", "する", "した。", "します。", "しました。",
    "done", "opened", "merged", "fixed", "shipped", "verified", "pushed",
    "closed", "completed",
)
def is_terse_tail(s):
    # Require the trimmed sentence to END WITH one of the terse tokens
    # (not merely CONTAIN one in the last 15 chars). Otherwise a tail
    # like "確認する予定です、たぶん" would match "する" mid-string and
    # bypass the nudge even though the sentence actually ends with
    # the hedging "たぶん".
    stripped = s.rstrip().rstrip("。").rstrip(".").lower()
    return any(stripped.endswith(t.rstrip("。").lower()) for t in TERSE_TAILS)

sentences = re.split(r"[。\n]", text)
for s in sentences:
    s = s.strip()
    if not s or len(s) <= 35:
        continue
    if is_terse_tail(s):
        continue
    sys.stderr.write(
        f"{MARKER}: long "
        f"sentence ({len(s)} chars) lacks a terse closer "
        "(します / しました / done / opened / merged / etc.). "
        "Operator (2026-06-09) prefers messages that read as "
        "INTENT (will do) or FACT (done), not rambling narration. "
        "Consider tightening or splitting.\n\n"
        f"  offending tail: ...{s[-30:]!r}\n\n"
        "Silence this hook entirely: set env CC_ALLOW_NON_TERSE=1.\n"
    )
    break  # one nudge per message is enough
sys.exit(0)
'
exit $?
