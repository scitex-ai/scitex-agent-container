#!/bin/bash
# -*- coding: utf-8 -*-
# Timestamp: "2026-06-09 (OP-PRIO-FMT rule 4)"
# File: ~/.claude/hooks/pre-tool-use/enforce_telegram_no_filler.sh
#
# OP-PRIO-FMT rule 4 (operator 2026-06-09, 無駄口): a Telegram message
# MUST NOT contain filler / hedging words that pad the line without
# adding signal. The operator wants terse status, not narration.
# Detects common Japanese + English fillers; BLOCKS with a list of the
# offending tokens.
#
# Fires on: tool_name matches the matcher in settings.local.json
# (must be the FQ mcp__claude-code-telegrammer__reply name — operator
# 2026-06-09 OP-PRIO-2 matcher fix is a hard prerequisite).
# FAIL-OPEN on any parse error or unexpected payload shape.
# Escape: CC_ALLOW_FILLER=1.

set -u
[[ "${CC_ALLOW_FILLER:-}" == "1" ]] && exit 0

if [[ "${1:-}" == "--self-test" ]]; then
    echo "=== Self-test: $(basename "$0") ==="
    pass=0
    fail=0
    run() {
        local desc="$1" tool="$2" text="$3" want="$4" rc
        printf '%s' "{\"tool_name\":\"$tool\",\"tool_input\":{\"chat_id\":\"1\",\"text\":$(printf '%s' "$text" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))')}}" \
            | "$0" >/dev/null 2>&1
        rc=$?
        if [[ "$rc" == "$want" ]]; then echo "  PASS ($rc) $desc"; pass=$((pass+1));
        else echo "  FAIL got $rc want $want: $desc"; fail=$((fail+1)); fi
    }
    T="mcp__claude-code-telegrammer__reply"
    run "EN: basically X is Y              -> block" "$T" "Basically, X is Y." 2
    run "EN: actually we did it            -> block" "$T" "Actually we did it." 2
    run "EN: just a quick update           -> block" "$T" "Just a quick update on PR." 2
    run "JP: とりあえず restart             -> block" "$T" "とりあえず restart します" 2
    run "JP: なんか log が出てる            -> block" "$T" "なんか log が出てる" 2
    run "JP: 一旦 保留                      -> block" "$T" "一旦 保留" 2
    run "clean: PR #343 opened             -> allow" "$T" "PR #343 opened" 0
    run "clean: 完了報告: rename committed -> allow" "$T" "完了報告: rename committed" 0
    run "non-telegram tool ignored         -> allow" "Bash" "Just a test" 0
    run "empty text                        -> allow" "$T" "" 0
    echo "pass=$pass fail=$fail"
    [[ "$fail" == "0" ]] && exit 0 || exit 1
fi

exec python3 -c '
import json
import re
import sys

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
# English filler — match as whole words, case-insensitive.
EN_FILLER = [
    "actually", "basically", "just", "really", "honestly", "obviously",
    "kinda", "sorta", "anyway", "anyways", "literally", "essentially",
]
# Japanese filler — substring match (no word boundaries in JP).
JP_FILLER = [
    "一旦", "とりあえず", "ちょっと", "なんか", "まあ", "えーと",
    "やはり", "つまり", "結局", "なんとなく", "そうですね",
]
hits = []
for w in EN_FILLER:
    if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE):
        hits.append(w)
for w in JP_FILLER:
    if w in text:
        hits.append(w)
if hits:
    sys.stderr.write(
        "BLOCKED by enforce_telegram_no_filler.sh: filler/hedging words "
        f"found: {sorted(set(hits))!r}. Operator wants terse status, not "
        "narration. Strip the filler and report the bare fact.\n\n"
        "  - \"Basically we did X\" -> \"did X\".\n"
        "  - \"一旦 保留\" -> \"保留\".\n"
        "  - \"actually it works\" -> \"works\".\n\n"
        "Rare one-off override: set env CC_ALLOW_FILLER=1.\n"
    )
    sys.exit(2)
sys.exit(0)
'
exit $?
