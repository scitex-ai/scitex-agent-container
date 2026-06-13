#!/bin/bash
# -*- coding: utf-8 -*-
# Timestamp: "2026-06-09 (OP-PRIO-FMT rule 1)"
# File: ~/.claude/hooks/pre-tool-use/enforce_telegram_numbering.sh
#
# OP-PRIO-FMT rule 1 (operator 2026-06-09): a Telegram message that
# OFFERS OPTIONS / CHOICES MUST number them in the operator's
# preferred 1a / 1b style (or plain 1. / 2. enumeration). The
# motivation is one-tap quoting on a phone: the operator selects
# by INDEX, not by hunting for the letter mid-paragraph.
#
# Detection: the message contains ``A)`` AND ``B)`` (the lettered
# option pattern) anywhere — without a parallel ``1.`` / ``1a.`` /
# ``1)`` numbered alternative ALSO present. Lettered-only options
# BLOCK with a nudge to renumber.
#
# Fires on: tool_name matches the matcher in settings.local.json
# (must be the FQ mcp__claude-code-telegrammer__reply name — operator
# 2026-06-09 OP-PRIO-2 matcher fix is a hard prerequisite).
# FAIL-OPEN on any parse error or unexpected payload shape.
# Escape: CC_ALLOW_LETTERED_OPTIONS=1.

set -u
[[ "${CC_ALLOW_LETTERED_OPTIONS:-}" == "1" ]] && exit 0

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
    run "lettered A) + B) only           -> block" "$T" $'A) issue\nB) fix\nC) workaround' 2
    run "lettered A) B) inline           -> block" "$T" 'Pick: A) ship now B) hold' 2
    run "lettered + numbered 1a/1b mix   -> allow" "$T" $'1a) issue\n1b) fix\n1c) workaround' 0
    run "plain numbered 1. 2.            -> allow" "$T" $'1. issue\n2. fix' 0
    run "no options at all               -> allow" "$T" "completed PR-A merge." 0
    run "prose with A something          -> allow" "$T" "A new issue was opened." 0
    run "non-telegram tool ignored       -> allow" "Bash" "A) x B) y" 0
    run "empty text                      -> allow" "$T" "" 0
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
# Lettered options: at least TWO of {A) B) C) D) E)} appear as option
# markers — either at the start of a line, or after a space + letter +
# close-paren. We anchor on space-or-line-start so prose like "A new
# issue" does not trip.
lettered_pat = re.compile(r"(?:^|\s)([A-E])\)\s", re.MULTILINE)
letters_found = set(lettered_pat.findall(text))
if len(letters_found) < 2:
    sys.exit(0)
# If the same text ALSO has a parallel numbered enumeration (1. / 1a.
# / 1)), let it pass — the operator can still tap by index.
numbered_pat = re.compile(r"(?:^|\s)(\d+[a-z]?[.)])\s", re.MULTILINE)
if numbered_pat.search(text):
    sys.exit(0)
sys.stderr.write(
    "BLOCKED by enforce_telegram_numbering.sh: lettered options "
    f"detected ({sorted(letters_found)!r}) without a parallel "
    "numbered enumeration. Operator (2026-06-09): use 1a/1b/1c "
    "(or 1./2./3.) so options can be quoted by INDEX on a phone, "
    "not by hunting for a letter mid-paragraph.\n\n"
    "  - Bad : \"A) ship now  B) hold  C) revert\".\n"
    "  - Good: \"1a) ship now\\n1b) hold\\n1c) revert\".\n\n"
    "Rare one-off override: set env CC_ALLOW_LETTERED_OPTIONS=1.\n"
)
sys.exit(2)
'
exit $?
