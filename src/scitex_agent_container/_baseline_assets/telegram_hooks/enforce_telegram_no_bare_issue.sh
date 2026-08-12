#!/bin/bash
# -*- coding: utf-8 -*-
# Timestamp: "2026-08-12 (OP-PRIO-FMT rule 2, tightened)"
# File: ~/.claude/hooks/pre-tool-use/enforce_telegram_no_bare_issue.sh
#
# OP-PRIO-FMT rule 2 (operator 2026-06-09, TIGHTENED 2026-08-11): a
# Telegram message MUST NOT carry an issue/PR number the operator
# cannot read. He reads on a phone: he cannot follow a link, and the
# number alone tells him nothing about what changed.
#
#   2026-08-11 「それだと何が何だか私にとってはわからないので、必ず
#   中身を私がわかるようにディスクリプティブな説明をつけて欲しい」
#   ... and the format, in his words: 「ナンバーの後に ( をつけて説明
#   する、っていうのをルールにしてください」
#
# WHY THIS WAS REWRITTEN. The 2026-06-09 implementation only blocked a
# message that reduced ENTIRELY to bare ``#NNN`` tokens. A number
# embedded in a sentence — the overwhelmingly common case — passed
# untouched. On 2026-08-11 an agent sent a line containing ``#970`` with
# no description, it went through, and the operator asked why the rule
# was not enforced. That is the recurring fleet shape: A GUARD WHOSE
# TRIGGER CONDITION IS NARROWER THAN ITS STATED RULE READS AS
# ENFORCEMENT WHILE ENFORCING ALMOST NOTHING.
#
# THE RULE NOW ENFORCED. Every ``#NNN`` token anywhere in the message
# must be immediately followed by a parenthetical description. Both the
# ASCII ``(`` and the full-width ``（`` are accepted — the operator
# writes Japanese and a Japanese IME produces the full-width form. A
# single run of spaces/tabs/ideographic-space between the number and the
# paren is allowed, and the parenthetical must hold at least one
# non-space character (``#970 ()`` describes nothing).
#
#   #970（グループ判定がスペックを読む）    accept
#   #970 (group authority reads the spec)   accept
#   #970 の話ではなく…                      REFUSE
#   …見つかった #578、これは…                REFUSE
#
# THREE DELIBERATE DECISIONS (documented so a reader can disagree with
# the choice rather than guess whether it was one):
#
#   1. URLs ARE NOT REFERENCES. A link is blanked out before scanning,
#      so ``…/pull/970`` (a path segment, no ``#`` at all) and a real
#      ``#`` fragment such as ``…/pull/970#issuecomment-123`` or
#      ``…/page#123`` never trip the rule. The operator cannot follow a
#      link on his phone either, but a URL is not a token pretending to
#      be a description — it is self-evidently a link, and rewriting it
#      is not what he asked for. Blanking preserves offsets (NUL fill)
#      so the refusal can still quote the right part of the message.
#
#   2. A REPEATED REFERENCE INHERITS THE FIRST DESCRIPTION. If ``#970
#      (…)`` is described once, a later bare ``#970`` in the SAME
#      message is accepted. The rule exists so the operator can read the
#      message and know what the number means; once the description is
#      on the page he is already reading, demanding it again would force
#      padding like ``#970（同じPR）`` — exactly the unreadable noise the
#      rule was written to remove. The allowance is strictly
#      LEFT-TO-RIGHT, because he reads top to bottom: a bare ``#970``
#      BEFORE its description is still refused.
#
#   3. A REPO NAME IS NOT A DESCRIPTION. ``scitex-dev #578`` is refused
#      exactly like a naked ``#578``; ``owner/repo#578`` likewise. The
#      repo says where to look, not what happened.
#
# Fires on: tool_name matches the matcher in settings.local.json (must
# be the FQ mcp__claude-code-telegrammer__reply name — operator
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

    # --- the real messages this fleet actually sent -------------------
    run "REAL the message that prompted the rule (bare #967)      -> block" "$T" \
        "lead a2a のグループ判定を修正して #967 を出しました" 2
    run "REAL tonight's #970 の話ではなく                          -> block" "$T" \
        "#970 の話ではなく、その前段のスペック読みの話です" 2
    run "REAL compliant #970（…）                                  -> allow" "$T" \
        "#970（グループ判定がスペックではなくDBを読む）を出しました" 0
    run "REAL github URL in the message                           -> allow" "$T" \
        "マージしました https://github.com/ywatanabe1989/scitex-agent-container/pull/970" 0
    run "REAL no numbers at all                                   -> allow" "$T" \
        "CI is green, nothing blocking" 0

    # --- the required form, both parens, both spacings ----------------
    run "full-width paren  #970（説明）                            -> allow" "$T" "#970（説明）" 0
    run "ascii paren       #970 (description here)                -> allow" "$T" "#970 (description here)" 0
    run "no space + ascii  #970(description)                      -> allow" "$T" "#970(description)" 0
    run "ideographic space #970　（説明）                          -> allow" "$T" "#970　（説明）" 0
    run "empty paren       #970 ()                                -> block" "$T" "#970 ()" 2
    run "blank paren       #970（　）                              -> block" "$T" "#970（　）" 2
    run "paren after prose #970 は良い (説明)                      -> block" "$T" "#970 は良い (説明)" 2
    run "newline before (  #970\\n(説明)                           -> block" "$T" "$(printf '#970\n(説明)')" 2

    # --- embedded in a sentence: the whole point of the tightening ----
    run "mid-sentence      …見つかった #578、これは…                -> block" "$T" \
        "調べていて見つかった #578、これはまだ直っていません" 2
    run "trailing          fix #162                               -> block" "$T" "fix #162" 2
    run "leading + prose   #162 figrecipe caption                 -> block" "$T" "#162 figrecipe caption" 2

    # --- cross-repo: the repo name is not a description ---------------
    run "cross-repo bare   scitex-dev #578                        -> block" "$T" "scitex-dev #578" 2
    run "owner/repo bare   ywatanabe1989/scitex-dev#578           -> block" "$T" "ywatanabe1989/scitex-dev#578" 2
    run "cross-repo ok     scitex-dev #578（型が合わない）          -> allow" "$T" \
        "scitex-dev #578（型が合わない）" 0

    # --- URLs are not references --------------------------------------
    run "url path segment  …/pull/970                             -> allow" "$T" \
        "https://github.com/o/r/pull/970" 0
    run "url # fragment    …/pull/970#issuecomment-123            -> allow" "$T" \
        "https://github.com/o/r/pull/970#issuecomment-123" 0
    run "url numeric frag  https://example.com/page#123           -> allow" "$T" \
        "見てください https://example.com/page#123" 0
    run "markdown link     [x](https://e.com/p#123)               -> allow" "$T" \
        "[x](https://e.com/p#123)" 0
    run "url + bare #      url then a naked #970                  -> block" "$T" \
        "https://github.com/o/r/pull/970 と #970 の件" 2
    run "url + described   url then #970（説明）                    -> allow" "$T" \
        "https://github.com/o/r/pull/970 は #970（グループ判定の修正）です" 0
    run "url cannot supply the paren across the blank             -> block" "$T" \
        "#970 https://e.com/x (説明)" 2

    # --- repeat: first occurrence carries it, later ones inherit ------
    run "repeat described-then-bare                               -> allow" "$T" \
        "#970（グループ判定の修正）を出した。#970 のCIは緑" 0
    run "repeat bare-then-described (left-to-right)               -> block" "$T" \
        "#970 のCIは緑。#970（グループ判定の修正）" 2
    run "two numbers, only one described                          -> block" "$T" \
        "#970（グループ判定の修正）と #971" 2
    run "two numbers, both described                              -> allow" "$T" \
        "#970（グループ判定の修正）と #971（テスト追加）" 0
    run "nested bare inside a description                         -> block" "$T" \
        "#970（#969 の続き）" 2

    # --- the 2026-06-09 block cases must ALL still block ---------------
    run "legacy bare single   #162                                -> block" "$T" "#162" 2
    run "legacy brackets      [#162]                              -> block" "$T" "[#162]" 2
    run "legacy two bare      #162 #163                           -> block" "$T" "#162 #163" 2
    run "legacy trimmed       \"  #162  \"                        -> block" "$T" "  #162  " 2

    # --- not a reference at all ---------------------------------------
    run "markdown heading  '# 970 title'                          -> allow" "$T" "# 970 title" 0
    run "prose only        we are looking at issue                -> allow" "$T" "we are looking at issue" 0
    run "non-telegram      Bash tool ignored                      -> allow" "Bash" "#162" 0
    run "empty text                                               -> allow" "$T" "" 0

    # Escape-var contract: when ``CC_ALLOW_BARE_ISSUE=1`` is exported the
    # script must early-exit 0 even on a bare ``#NNN``. Tested by invoking
    # the script in a child shell that exports the var first.
    if CC_ALLOW_BARE_ISSUE=1 \
        printf '%s' "{\"tool_name\":\"$T\",\"tool_input\":{\"chat_id\":\"1\",\"text\":\"#162\"}}" \
        | CC_ALLOW_BARE_ISSUE=1 "$0" >/dev/null 2>&1; then
        echo "  PASS (0) escape var honored                              -> allow"
        pass=$((pass+1))
    else
        echo "  FAIL escape var honored                              -> allow"
        fail=$((fail+1))
    fi
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
if not isinstance(text, str) or not text:
    sys.exit(0)   # FAIL-OPEN: an unexpected payload shape is not a violation

# Blank every URL to NULs before scanning. NUL (not space) so a link can
# never bridge a number to a parenthesis that follows it, and same-length
# so offsets into the ORIGINAL text stay valid for the excerpt.
URL = re.compile(r"(?:https?://|ftp://|www\.)\S+", re.IGNORECASE)
scan = URL.sub(lambda m: "\x00" * len(m.group(0)), text)

REFERENCE = re.compile(r"#(\d+)")
# ...immediately followed by (optional horizontal space then) an opening
# paren, ASCII or full-width, holding at least one non-space character.
DESCRIBED = re.compile(r"[ \t　]*[(（][ \t　]*[^\s)）]")

described = set()
offender = None
for match in REFERENCE.finditer(scan):
    number = match.group(1)
    if DESCRIBED.match(scan, match.end()):
        described.add(number)      # this occurrence carries its description
        continue
    if number in described:
        continue                   # described earlier in this same message
    offender = match
    break

if offender is None:
    sys.exit(0)

token = "#" + offender.group(1)
start = max(0, offender.start() - 24)
end = min(len(text), offender.end() + 24)
excerpt = " ".join(text[start:end].split())
if start > 0:
    excerpt = "..." + excerpt
if end < len(text):
    excerpt = excerpt + "..."

sys.stderr.write(
    "BLOCKED by enforce_telegram_no_bare_issue.sh: the message contains "
    f"{token}, which is not followed by a description.\n\n"
    f"  found: {excerpt}\n\n"
    "The operator reads on a phone. He cannot follow a link, and a bare "
    "number tells him nothing about what changed. His rule, 2026-08-11: "
    "put a parenthesis after the number and explain inside it.\n\n"
    f"  required:  {token}(what it is)   or   {token}（中身の説明）\n"
    f"  you wrote: {token}\n"
    f"  fix it to: {token}（グループ判定がスペック"
    "を読む問題を修正）\n\n"
    "Both ( and （ are accepted. A repo name is not a description "
    "(scitex-dev #578 still needs one). Once a number is described, "
    "later mentions of that SAME number in this message are fine. URLs "
    "are exempt.\n\n"
    "Rare one-off override: set env CC_ALLOW_BARE_ISSUE=1.\n"
)
sys.exit(2)
'
exit $?
