#!/bin/bash
# -*- coding: utf-8 -*-
# Timestamp: "2026-06-09 (OP-PRIO-FMT rule 3)"
# File: ~/.claude/hooks/pre-tool-use/enforce_telegram_use_lists.sh
#
# OP-PRIO-FMT rule 3 (operator 2026-06-09): a Telegram message that
# enumerates 3+ items MUST present them as a list, not as run-on
# prose. The operator skims structured lists; comma-soup AND
# slash-cramming hide the count and force re-reading.
#
# Detection (any one of these on a single non-list line fires):
#   - 3+ comma-separated fragments (EN ``,`` with ``and``/``or``
#     conjunction, OR JP ``、`` / ``，`` 2+ occurrences), OR
#   - 3+ items separated by `` / `` (slash with SURROUNDING spaces).
#     The operator's most common run-on form (lead 2026-06-09):
#     ``やること: 認証PR / ボード整理 / スキル更新 / 通知設定``.
#     URL / path slashes (``http://...``, ``/home/...``) are guarded
#     because their slashes have NO surrounding spaces.
# Lines that already start with a list marker (``-``, ``*``, ``1.``,
# ``1a.``, ``A)``, etc.) are exempt.
#
# Fires on: tool_name matches the matcher in settings.local.json
# (must be the FQ mcp__claude-code-telegrammer__reply name — operator
# 2026-06-09 OP-PRIO-2 matcher fix is a hard prerequisite).
# FAIL-OPEN on any parse error or unexpected payload shape.
# Escape: CC_ALLOW_PROSE_ENUM=1.

set -u
[[ "${CC_ALLOW_PROSE_ENUM:-}" == "1" ]] && exit 0

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
    run "prose 3 items \"A, B, and C\"      -> block" "$T" "did A, B, and C in one pass." 2
    run "prose 3 items JP 「A、B、C」        -> block" "$T" "A、B、C を実装した。" 2
    # Lead 2026-06-09 detection-gap repro: slash-separated cramming.
    run "slash 4 items JP やること:...      -> block" "$T" "やること: 認証PR / ボード整理 / スキル更新 / 通知設定" 2
    run "slash 3 items EN \"A / B / C\"     -> block" "$T" "did A / B / C in parallel" 2
    run "slash 2 items only \"A / B\"       -> allow" "$T" "ran A / B" 0
    # URL/path slashes must NOT trip (no spaces around the slashes).
    run "URL with many slashes              -> allow" "$T" "see https://example.com/path/to/file" 0
    run "POSIX path many slashes            -> allow" "$T" "edited /home/agent/.claude/hooks/x.sh" 0
    run "two items only \"A and B\"         -> allow" "$T" "did A and B." 0
    run "list already \"- A\\n- B\\n- C\"   -> allow" "$T" $'- A\n- B\n- C' 0
    run "numbered list \"1. A\\n2. B\\n3.C\" -> allow" "$T" $'1. A\n2. B\n3. C' 0
    run "non-enumeration prose             -> allow" "$T" "PR opened" 0
    run "non-telegram tool ignored         -> allow" "Bash" "A / B / C" 0
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
# A line that already STARTS with a list marker is exempt.
LIST_PREFIX = re.compile(r"^\s*(?:[-*•]|\d+[a-z]?[.)]|[A-E]\))\s")
# Detect run-on prose enumeration on a single line:
#   - 2+ commas (EN: ",") OR 2+ JP commas ("、" / "，")
#   - AND a closing conjunction ("and " / "or " / "、" trailing).
EN_PROSE = re.compile(r"\b(?:and|or)\b\s+\S", re.IGNORECASE)
# Slash-cramming detector (lead 2026-06-09 gap fix). Matches a literal
# slash with REQUIRED surrounding spaces, so URLs (``http://``) and
# POSIX paths (``/home/...``) — whose slashes have no spaces around
# them — never trip. 2+ occurrences = 3+ items = "use a list".
SLASH_SEP = re.compile(r" / ")
for raw in text.split("\n"):
    line = raw.strip()
    if not line:
        continue
    if LIST_PREFIX.match(line):
        continue
    en_commas = line.count(",")
    jp_commas = line.count("、") + line.count("，")
    has_conjunction = bool(EN_PROSE.search(line)) or line.count("、") >= 2
    slash_seps = len(SLASH_SEP.findall(line))
    if (en_commas >= 2 and has_conjunction) or jp_commas >= 2 or slash_seps >= 2:
        sys.stderr.write(
            "BLOCKED by enforce_telegram_use_lists.sh: prose "
            f"enumeration detected in line {line!r}. Operator "
            "(2026-06-09) wants 3+ items as a list — comma-soup "
            "AND slash-cramming hide the count and force "
            "re-reading on the phone.\n\n"
            "  - Bad : \"did A, B, and C in one pass.\"\n"
            "  - Bad : \"やること: 認証PR / ボード整理 / スキル更新 / 通知設定\"\n"
            "  - Good: \"- A\\n- B\\n- C\" (or numbered 1./2./3.).\n\n"
            "Rare one-off override: set env CC_ALLOW_PROSE_ENUM=1.\n"
        )
        sys.exit(2)
sys.exit(0)
'
exit $?
