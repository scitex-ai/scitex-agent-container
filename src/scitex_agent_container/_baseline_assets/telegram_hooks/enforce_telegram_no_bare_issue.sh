#!/bin/bash
# -*- coding: utf-8 -*-
# Timestamp: "2026-06-09 (OP-PRIO-FMT rule 2)"
# File: ~/.claude/hooks/pre-tool-use/enforce_telegram_no_bare_issue.sh
#
# OP-PRIO-FMT rule 2 (operator 2026-06-09): a Telegram message MUST NOT
# be a bare issue/PR number ("#162") with no surrounding context. The
# operator's phone shows the message itself; an opaque "#162" forces
# the operator to open GitHub to learn what is being reported. Demand
# at least a verb + the noun the issue describes.
#
# Detection: the WHOLE message text (trimmed of leading/trailing
# whitespace and outer brackets) reduces to ONLY one or more bare
# ``#\d+`` tokens. ``#162``, ``#162 #163``, ``[#162]`` → BLOCK.
# ``#162 figrecipe caption overlap`` → ALLOW. ``fix #162`` → ALLOW.
#
# Fires on: tool_name matches the matcher in settings.local.json
# (must be the FQ mcp__claude-code-telegrammer__reply name — operator
# 2026-06-09 OP-PRIO-2 matcher fix is a hard prerequisite).
# FAIL-OPEN on any parse error or unexpected payload shape.
# Escape: CC_ALLOW_BARE_ISSUE=1.

set -u
[[ "${CC_ALLOW_BARE_ISSUE:-}" == "1" ]] && exit 0

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
    run "bare single   #162                    -> block" "$T" "#162" 2
    run "bare in brackets [#162]               -> block" "$T" "[#162]" 2
    run "two bare      #162 #163               -> block" "$T" "#162 #163" 2
    run "trimmed       \"  #162  \"            -> block" "$T" "  #162  " 2
    run "verb + #      fix #162                -> allow" "$T" "fix #162" 0
    run "noun + #      #162 figrecipe caption  -> allow" "$T" "#162 figrecipe caption" 0
    run "prose only    we are looking at issue -> allow" "$T" "we are looking at issue" 0
    run "non-telegram  Bash tool ignored       -> allow" "Bash" "#162" 0
    run "empty text                            -> allow" "$T" "" 0
    # Escape-var contract: when ``CC_ALLOW_BARE_ISSUE=1`` is exported the
    # script must early-exit 0 even on a bare ``#NNN``. Tested by invoking
    # the script in a child shell that exports the var first.
    if CC_ALLOW_BARE_ISSUE=1 \
        printf '%s' "{\"tool_name\":\"$T\",\"tool_input\":{\"chat_id\":\"1\",\"text\":\"#162\"}}" \
        | CC_ALLOW_BARE_ISSUE=1 "$0" >/dev/null 2>&1; then
        echo "  PASS (0) escape var honored                    -> allow"
        pass=$((pass+1))
    else
        echo "  FAIL escape var honored                    -> allow"
        fail=$((fail+1))
    fi
    echo "pass=$pass fail=$fail"
    [[ "$fail" == "0" ]] && exit 0 || exit 1
fi

exec python3 -c '
import json
import os
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
stripped = text.strip()
# Strip outer brackets if the WHOLE message is wrapped.
inner = stripped
if (inner.startswith("[") and inner.endswith("]")) or (inner.startswith("(") and inner.endswith(")")):
    inner = inner[1:-1].strip()
# Tokenise: are ALL tokens bare #NNN?
tokens = inner.split()
if not tokens:
    sys.exit(0)
bare_issue = re.compile(r"^#\d+$")
if all(bare_issue.match(tok) for tok in tokens):
    sys.stderr.write(
        "BLOCKED by enforce_telegram_no_bare_issue.sh: the whole message "
        f"is a bare issue/PR number ({inner!r}). The operator reads on a "
        "phone; an opaque #NNN forces a GitHub round-trip just to learn "
        "what is being reported.\n\n"
        "  - Add the verb + noun: \"figrecipe #162 caption overlap "
        "reproduced (3 bugs)\".\n"
        "  - Or report the STATE: \"merged #343 (lead a2a auto-grant)\".\n\n"
        "Rare one-off override: set env CC_ALLOW_BARE_ISSUE=1.\n"
    )
    sys.exit(2)
sys.exit(0)
'
exit $?
