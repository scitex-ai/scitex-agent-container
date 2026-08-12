#!/bin/bash
# -*- coding: utf-8 -*-
# Timestamp: "2026-08-12 (OP-PRIO-FMT rule 2 — thin adapter over _telegram_rules.py)"
# File: ~/.claude/hooks/pre-tool-use/enforce_telegram_no_bare_issue.sh
#
# THIS FILE IS AN ADAPTER, NOT A RULE. The rule — every ``#NNN`` needs a
# parenthetical description — and the refusal wording both live in the
# sibling module ``_telegram_rules.py``, because the operator asked for
# exactly one place (2026-08-12):
#
#   「mcp も同じですね。同じルールなので、ルールは一つの場所に、
#     shell 用の hook と mcp のフィルタで同じルールを適用させて
#     ssot に、が良いかと」
#
# So: read the rule, its five documented decisions and the operator
# quotes that justify them in ``_telegram_rules.py``. Do not add
# judgement here — a second copy of the rule is the failure being
# designed against, since a message blocked on one path and allowed on
# the other is how a bare number reaches him anyway.
#
# Fires on: tool_name matches the matcher in settings.local.json (must
# be the FQ mcp__claude-code-telegrammer__reply name — operator
# 2026-06-09 OP-PRIO-2 matcher fix is a hard prerequisite).
# FAIL-OPEN on any parse error or unexpected payload shape.
# Escape: CC_ALLOW_BARE_ISSUE=1.

set -u
[[ "${CC_ALLOW_BARE_ISSUE:-}" == "1" ]] && exit 0

_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_RULES="${_HERE}/_telegram_rules.py"

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
    run "no-space CJK      #970の話ではなく                        -> block" "$T" "#970の話ではなく" 2

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

    # --- hex colours are not references (decision 4) ------------------
    run "hex colour 6      #589abc                                -> allow" "$T" "#589abc" 0
    run "hex colour 3      #fff                                   -> allow" "$T" "#fff" 0
    run "hex colour prose  use #589abc for the border             -> allow" "$T" \
        "use #589abc for the border" 0
    run "hex colour upper  #58ABCD                                -> allow" "$T" "#58ABCD" 0
    run "html entity       a dash &#8212; here                    -> allow" "$T" \
        "a dash &#8212; here" 0

    # --- code is data, not prose (decision 5) -------------------------
    run "inline code span  the token \`#589\` is data              -> allow" "$T" \
        "the token \`#589\` is data here" 0
    run "fenced block      \`\`\`\\n#589\\n\`\`\`                        -> allow" "$T" \
        "$(printf 'see below\n```\n#589\n```')" 0
    run "code cannot supply the paren across the blank            -> block" "$T" \
        "$(printf '#970 `x` (説明)')" 2

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
    run "markdown heading  '## 5 things'                          -> allow" "$T" "## 5 things" 0
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

# FAIL-OPEN if the rule module is missing: a hook that cannot find its
# rule must not block every message the operator is waiting on. The
# pytest at tests/integration/telegram_hooks/ pins that they ship
# together, so a missing module is a packaging bug caught in CI, not
# something to discover here at send time.
[[ -f "$_RULES" ]] || exit 0

exec python3 "$_RULES" --hook-json
