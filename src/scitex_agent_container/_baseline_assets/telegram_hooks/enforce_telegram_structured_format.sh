#!/bin/bash
# -*- coding: utf-8 -*-
# Timestamp: "2026-06-09 (OP-PRIO-FMT rule 6 / structured-format)"
# File: ~/.claude/hooks/pre-tool-use/enforce_telegram_structured_format.sh
#
# OP-PRIO-FMT rule 6 (operator 2026-06-09 — structured-format directive,
# extended by lead 2026-06-09 amendment for です/ます tone + dangling-
# prose ban): a Telegram message MUST be structured when it carries
# 3+ lines of content. Four failure modes are blocked here; the
# fifth (blank-line spacing between top-level items) is already
# enforced by the companion ``telegram_line_spacing.sh`` and
# intentionally NOT duplicated.
#
# Rule 1 — TOP-LEVEL NUMBERING
#   Top-level items MUST be numbered (1./2./3. or 1a./1b. style).
#   If a message has 3+ top-level hyphen-bullets and ZERO numbered
#   top-level items, the operator cannot quote by index on a phone
#   and is forced to re-read. BLOCK with a nudge to renumber.
#
# Rule 2 — PROSE PARAGRAPH BAN
#   In a multi-line (>=3 non-blank lines) message, every line over
#   ~60 visual cells MUST start with a list marker. Long prose
#   paragraphs hide structure on a phone. BLOCK on first offender.
#   Single short messages (1-2 non-blank lines) are EXEMPT —
#   tweet-length status pings stay legal.
#
# Rule 3 — です/ます LINE ENDING (lead 2026-06-09 amendment)
#   A line that closes with the Japanese full-stop 「。」 MUST stem
#   with one of the allowed verb forms: です / ます / でした /
#   ました / します / しました. Lines closing with 〜する。/
#   〜した。/ 体言止め + 。 are BLOCKED. Lines without 「。」
#   (fragments, headers, sub-bullet labels) are exempt — UNLESS
#   rule 3b fires (see below).
#
# Rule 3b — サ変名詞 体言止め WITHOUT 「。」 (operator 2026-06-09
#   escalation; 3 real crew escapes — "...scratch default 除去",
#   "spartan_submit_array.sh 新設", "background subagent 起動").
#   When a line carries Japanese characters, has content BEFORE
#   the final noun, and the stem ends with a canonical サ変名詞
#   (除去 / 新設 / 起動 / 完了 / ...), it is a 体言止め sentence
#   and must be rewritten as 〜します / 〜しました. Bare header
#   labels (``1. 完了``) and pure file refs (``- config/x.yaml``)
#   are EXEMPT via content-before-noun + file-suffix guards.
#
# Rule 4 — DANGLING TOP-LEVEL PROSE BAN (lead 2026-06-09 amendment)
#   In a multi-line message, every non-blank, non-fence line at
#   column 0 MUST start with a top-level number (``\d+[.)]\s``).
#   Prefatory openers (``[REPORT] ...``), trailing signatures
#   (``— agent-id ...``), and stray prose lines all hide the
#   structure. BLOCK so EVERY line belongs to the numbered list.
#
# Indented sub-bullets (``    - foo``) and code fences (``` ... ```)
# are exempt from the prose-length / dangling-prose checks. Code-
# fence interiors are never scanned. Sub-bullet indentation depth
# is not enforced here.
#
# Fires on: tool_name matches any of mcp__claude-code-telegrammer__
# reply / __send_document / __edit_message (lead 2026-06-09 brief).
# FAIL-OPEN on any parse error or unexpected payload shape.
# Escape: CC_ALLOW_UNSTRUCTURED_TG=1.

set -u
[[ "${CC_ALLOW_UNSTRUCTURED_TG:-}" == "1" ]] && exit 0

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
    TS="mcp__claude-code-telegrammer__send_document"
    TE="mcp__claude-code-telegrammer__edit_message"
    # ── Pass cases — operator-correct shapes ─────────────────────
    run "single short line                    -> allow" "$T" "PR #346 merged。" 0
    run "two-line short status                -> allow" "$T" $'PR #346 をマージしました。\n次は #347 に着手します。' 0
    run "numbered top + 4sp hyphen children   -> allow" "$T" $'1. 形式\n\n    - 上位は番号\n\n    - 下位は4スペース+ハイフン\n\n2. 散文禁止\n\n    - 60字超 block' 0
    run "numbered top + nested-only           -> allow" "$T" $'1. 完了\n\n    - test A\n\n    - test B\n\n2. 残\n\n    - test C' 0
    run "code fence with long code line       -> allow" "$T" $'1. 結果\n\n```\nsubprocess.run(["bash", "-lc", "echo this code line is much longer than 60 characters easily"])\n```\n\n2. 次のステップ' 0
    run "short prose paragraph (2 lines)      -> allow" "$T" $'PR #346 をリードへ送りました。\nレビュー待ちです。' 0
    run "numbered tops, allowed JP endings    -> allow" "$T" $'1. PR #346 を push しました。\n\n2. self-test 14/14 緑です。\n\n3. lead に報告します。' 0
    # ── Fail cases — operator-prohibited shapes ──────────────────
    run "3+ hyphen top, no numbering          -> block" "$T" $'- A\n- B\n- C\n- D' 2
    run "3+ hyphen top, no numbering (JP)     -> block" "$T" $'- 起動完了\n- ACK 送信\n- 結果待ち' 2
    run "long prose line (>60) in multi-line  -> block" "$T" $'header\n\nこれは非常に長い散文パラグラフで、構造を持たずに60文字を確実に超えている悪い例です。\n\n末尾' 2
    run "long prose EN (>60) in multi-line    -> block" "$T" $'top\n\nThis is a very long prose paragraph without any list marker that exceeds sixty characters easily.\n\ntail' 2
    # Rule 3 — verb-form line ending (lead 2026-06-09 amendment).
    run "line ends with する。 (verb-base)     -> block" "$T" $'1. PR を push する。\n\n2. self-test 緑です。\n\n3. lead に報告します。' 2
    run "line ends with した。 (verb-past)    -> block" "$T" $'1. PR を push した。\n\n2. self-test 緑です。\n\n3. lead に報告します。' 2
    run "line ends with 体言止め+。           -> block" "$T" $'1. PR を push 完了。\n\n2. self-test 緑です。\n\n3. lead に報告します。' 2
    # Rule 3b — サ変名詞 体言止め WITHOUT 「。」 (operator escalation
    # 2026-06-09: 3 real crew escapes slipped past rule 3 because they
    # had no trailing 「。」 yet still ended on a verbal noun like 除去
    # / 新設 / 起動, which is just 体言止め with the dot dropped).
    run "JP line ends with 除去 (体言止め)     -> block" "$T" $'1. spartan_preamble.sh, spartan_one_capsule.sh の scratch default 除去\n\n2. self-test 緑です。\n\n3. lead に報告します。' 2
    run "JP line ends with 新設 (体言止め)     -> block" "$T" $'1. spartan_submit_array.sh 新設\n\n2. self-test 緑です。\n\n3. lead に報告します。' 2
    run "JP line ends with 起動 (体言止め)     -> block" "$T" $'1. background subagent 起動\n\n2. self-test 緑です。\n\n3. lead に報告します。' 2
    # Rule 3b guard — pure file-name / path bullets (no Japanese) must
    # NOT trigger; they are identifiers, not 体言止め sentences.
    run "ASCII file-name sub-bullet             -> allow" "$T" $'1. 形式\n\n    - config/SPARTAN.yaml\n\n    - scripts/foo.sh\n\n2. 次へ進みます。' 0
    # Rule 4 — dangling top-level prose ban (lead 2026-06-09 amendment).
    run "prefatory [REPORT] preamble          -> block" "$T" $'[REPORT] 起動報告。\n\n1. 完了しました。\n\n2. 次へ進みます。' 2
    run "trailing signature dash line         -> block" "$T" $'1. 完了しました。\n\n2. push しました。\n\n— proj-scitex-agent-container (wyusuuke | /work)' 2
    run "stray prose line between numbered    -> block" "$T" $'1. 完了しました。\n\nところで、ここに散文があります。\n\n2. 次へ進みます。' 2
    # ── Tool gating ──────────────────────────────────────────────
    run "send_document caption, bad shape     -> block" "$TS" $'- a\n- b\n- c\n- d' 2
    run "edit_message text, bad shape         -> block" "$TE" $'- a\n- b\n- c\n- d' 2
    run "non-telegram tool ignored            -> allow" "Bash" $'- a\n- b\n- c\n- d' 0
    run "empty text                           -> allow" "$T" "" 0
    echo "pass=$pass fail=$fail"
    [[ "$fail" == "0" ]] && exit 0 || exit 1
fi

exec python3 -c '
import json
import re
import sys
import unicodedata


def visual_len(s):
    """Approximate display width on a phone — CJK fullwidth = 2 cells.

    Matches the operator intent for ``60字`` better than ``len(s)``
    on multi-script lines: a 43-char kanji run is visually wider
    than a 50-char ASCII run on Telegram for iOS / Android.
    """
    return sum(
        2 if unicodedata.east_asian_width(c) in ("F", "W") else 1
        for c in s
    )

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
tool = data.get("tool_name", "")
TELEGRAM_TOOLS = (
    "claude-code-telegrammer__reply",
    "claude-code-telegrammer__send_document",
    "claude-code-telegrammer__edit_message",
)
if not any(t in tool for t in TELEGRAM_TOOLS):
    sys.exit(0)
tool_input = data.get("tool_input", {}) or {}
# Both ``text`` (reply / edit_message) and ``caption`` (send_document)
# are user-facing strings. Check whichever is present.
text = tool_input.get("text") or tool_input.get("caption") or ""
if not text:
    sys.exit(0)

raw_lines = text.split("\n")
# Walk once: count top-level numbered + top-level hyphen bullets,
# and remember which lines fall inside a fenced code block so the
# prose-length check can skip them.
TOP_NUMBERED = re.compile(r"^\d+[a-z]?[.)]\s")
TOP_HYPHEN = re.compile(r"^[-*•]\s")
LIST_PREFIX = re.compile(r"^\s*(?:[-*•]|\d+[a-z]?[.)]|[A-E]\))\s")
top_numbered = 0
top_hyphen = 0
in_fence = False
fence_mask = []
for raw in raw_lines:
    stripped = raw.strip()
    if stripped.startswith("```"):
        in_fence = not in_fence
        fence_mask.append(True)  # fence delimiter itself is exempt
        continue
    fence_mask.append(in_fence)
    if in_fence:
        continue
    if TOP_NUMBERED.match(raw):
        top_numbered += 1
    elif TOP_HYPHEN.match(raw):
        top_hyphen += 1

# Short-message exemption: 1-2 non-blank, non-fence lines stay legal.
content_lines = [
    raw for raw, masked in zip(raw_lines, fence_mask)
    if raw.strip() and not masked
]
if len(content_lines) <= 2:
    sys.exit(0)

# Rule 1 — 3+ top-level hyphen bullets WITHOUT any numbered top item.
if top_hyphen >= 3 and top_numbered == 0:
    sys.stderr.write(
        "BLOCKED by enforce_telegram_structured_format.sh: "
        f"{top_hyphen} top-level hyphen bullets without numbering. "
        "Operator (2026-06-09): top-level items MUST be numbered "
        "(1. / 2. / 3.) so the operator can quote by INDEX on a "
        "phone. Use 4-space hyphen sub-bullets UNDER each numbered "
        "item.\n\n"
        "  Bad:\n"
        "    - 起動完了\n"
        "    - ACK 送信\n"
        "    - 結果待ち\n\n"
        "  Good:\n"
        "    1. 起動完了\n\n"
        "    2. ACK 送信\n\n"
        "    3. 結果待ち\n\n"
        "Rare one-off override: set env CC_ALLOW_UNSTRUCTURED_TG=1.\n"
    )
    sys.exit(2)

# Rule 2 — long prose paragraph (>60 visual cells) WITHOUT a list
# marker, in a multi-line (>=3 content lines) message. Code-fence
# interior already filtered above.
PROSE_LIMIT = 60
for raw, masked in zip(raw_lines, fence_mask):
    if masked:
        continue
    line = raw.rstrip()
    if not line.strip():
        continue
    if LIST_PREFIX.match(line):
        continue
    width = visual_len(line)
    if width > PROSE_LIMIT:
        sys.stderr.write(
            "BLOCKED by enforce_telegram_structured_format.sh: "
            f"long prose line (visual {width} > {PROSE_LIMIT}) "
            "without a list marker, in a multi-line message. "
            "Operator (2026-06-09): structured messages must keep "
            "non-list lines short — break long prose into numbered "
            "items (1. / 2. / 3.) with 4-space hyphen sub-bullets.\n\n"
            f"  offending line: {line!r}\n\n"
            "Tip: a single short status (1-2 lines) is exempt from "
            "this rule. If your update is genuinely tweet-length, "
            "trim to 1-2 lines.\n\n"
            "Rare one-off override: set env CC_ALLOW_UNSTRUCTURED_TG=1.\n"
        )
        sys.exit(2)

# Rule 3 — です/ます line ending (lead 2026-06-09 amendment). A line
# closing with the Japanese full-stop 「。」 MUST stem with one of
# {です, ます, でした, ました, します, しました}. Lines without 「。」
# (fragments, headers, list-marker labels) are exempt.
ALLOWED_ENDINGS = ("です", "ます", "でした", "ました", "します", "しました")
for raw, masked in zip(raw_lines, fence_mask):
    if masked:
        continue
    line = raw.rstrip()
    if not line.strip():
        continue
    if not line.endswith("。"):
        continue
    # Strip trailing 。 (one or more) plus any trailing closer 」』)）.
    stem = re.sub(r"[。」』)）\s]+$", "", line)
    if not any(stem.endswith(form) for form in ALLOWED_ENDINGS):
        sys.stderr.write(
            "BLOCKED by enforce_telegram_structured_format.sh: "
            f"line ends with disallowed verb form. Lead (2026-06-09): "
            "lines closing with 「。」 MUST stem with です / ます / "
            "でした / ました / します / しました. 〜する。/〜した。/"
            "体言止め+。 are blocked.\n\n"
            f"  offending line: {line!r}\n\n"
            "  Bad : \"1. PR を push する。\"\n"
            "  Bad : \"1. PR を push 完了。\"  (体言止め+。)\n"
            "  Good: \"1. PR を push します。\"\n"
            "  Good: \"1. PR を push しました。\"\n\n"
            "Rare one-off override: set env CC_ALLOW_UNSTRUCTURED_TG=1.\n"
        )
        sys.exit(2)

# Rule 3b — サ変名詞 体言止め WITHOUT trailing 「。」 (operator
# escalation 2026-06-09). Three real crew escapes slipped through
# rule 3 because they had no 「。」 yet still ended on a verbal noun
# (除去 / 新設 / 起動). A line is a 体言止め sentence — not a bare
# header label and not a file reference — when ALL of:
#   1. it ends with one of the canonical サ変名詞 below (stem after
#      trimming trailing closers / whitespace),
#   2. it contains at least one Japanese character (CJK / kana),
#   3. there is meaningful content BEFORE the noun on the same line
#      (so a bare ``1. 完了`` header label is still allowed — the
#      existing ``numbered top + nested-only -> allow`` self-test
#      case must keep passing),
#   4. the line is NOT a pure file-name / path reference (a bullet
#      like ``- config/SPARTAN.yaml`` must be allowed even though
#      it has no 体言止め — we just skip it via the noun check, but
#      we also short-circuit on file-suffix endings for clarity).
SAHEN_NOUNS = (
    "除去", "阻止", "新設", "起動", "進行", "完了", "確認", "実装",
    "作成", "削除", "報告", "更新", "登録", "設定", "取得", "対応",
    "調査", "検証", "修正", "追加", "開始", "終了", "抽出", "移行",
    "統合",
)
# Files / paths that should never be treated as sentences even if
# they happen to end with a SAHEN noun string — keep this list
# tight; the noun match below already filters out most file refs.
FILE_SUFFIX_RE = re.compile(
    r"\.(sh|py|yaml|yml|toml|md|json|txt|cfg|ini|lock|conf)$",
    re.IGNORECASE,
)
LIST_MARKER_RE = re.compile(r"^\s*(?:[-*•]|\d+[a-z]?[.)]|[A-E]\))\s+")
# Treat all CJK ideographs + hiragana + katakana as "Japanese".
JP_CHAR_RE = re.compile(
    r"[぀-ゟ゠-ヿ㐀-䶿一-鿿]"
)
for raw, masked in zip(raw_lines, fence_mask):
    if masked:
        continue
    line = raw.rstrip()
    if not line.strip():
        continue
    if line.endswith("。"):
        # Already covered by rule 3 above — rule 3b is the "no 。"
        # complement and must not double-fire.
        continue
    if not JP_CHAR_RE.search(line):
        # ASCII-only / pure-config-key bullets: ``- config/SPARTAN.yaml``
        # are identifiers, not 体言止め sentences. Skip.
        continue
    # Strip list-marker prefix so a bare ``1. 完了`` header is judged
    # as just ``完了`` (no preceding content -> NOT a sentence).
    content = LIST_MARKER_RE.sub("", line, count=1)
    # Strip trailing closers / whitespace before checking the noun.
    stem = re.sub(r"[」』)）\s]+$", "", content)
    # File-path guard: bullets like ``- config/SPARTAN.yaml`` whose
    # remaining content ends with a known source/config suffix are
    # references, not sentences.
    if FILE_SUFFIX_RE.search(stem):
        continue
    matched_noun = None
    for noun in SAHEN_NOUNS:
        if stem.endswith(noun):
            matched_noun = noun
            break
    if matched_noun is None:
        continue
    # Bare header label: stem IS the noun (nothing meaningful before).
    # ``1. 完了`` -> content ``完了`` -> stem ``完了`` == noun -> allow.
    if stem == matched_noun:
        continue
    # Otherwise: the line has content before a サ変名詞 ending and
    # contains Japanese -> it is a 体言止め sentence. Block.
    sys.stderr.write(
        "BLOCKED by enforce_telegram_structured_format.sh: "
        "line ends with a サ変名詞 (体言止め) without 「。」 or a "
        "polite verb form. Operator (2026-06-09 escalation): "
        "sentences ending in 〜除去 / 〜新設 / 〜起動 / 〜完了 etc. "
        "must be rewritten with 〜します / 〜しました (or 〜です / "
        "〜ます). Bare header labels (``1. 完了``) and pure file "
        "references (``- config/SPARTAN.yaml``) are exempt.\n\n"
        f"  offending line: {line!r}\n"
        f"  matched noun  : {matched_noun!r}\n\n"
        "  Bad : \"1. spartan_submit_array.sh 新設\"\n"
        "  Bad : \"2. background subagent 起動\"\n"
        "  Good: \"1. spartan_submit_array.sh を新設しました。\"\n"
        "  Good: \"2. background subagent を起動しました。\"\n\n"
        "Rare one-off override: set env CC_ALLOW_UNSTRUCTURED_TG=1.\n"
    )
    sys.exit(2)

# Rule 4 — dangling top-level prose ban (lead 2026-06-09 amendment).
# In a multi-line message, every non-blank, non-fence line at column 0
# MUST start with a top-level number (``\d+[a-z]?[.)]\s``). Prefatory
# openers, trailing signatures, stray prose lines all violate.
for raw, masked in zip(raw_lines, fence_mask):
    if masked:
        continue
    line = raw.rstrip()
    if not line.strip():
        continue
    # Indented lines are sub-bullets / continuations — fine.
    if line[0] in " \t":
        continue
    # Top-level numbered — fine.
    if TOP_NUMBERED.match(line):
        continue
    # Otherwise: dangling top-level prose. Block.
    sys.stderr.write(
        "BLOCKED by enforce_telegram_structured_format.sh: "
        "dangling top-level prose line detected. Lead (2026-06-09): "
        "EVERY line in a multi-line message must belong to the "
        "numbered list — no prefatory openers (``[REPORT] ...``), "
        "no trailing signatures (``— agent-id ...``), no stray "
        "prose between items.\n\n"
        f"  offending line: {line!r}\n\n"
        "  Bad:\n"
        "    [REPORT] 起動報告。\n\n"
        "    1. 完了しました。\n\n"
        "    — proj-scitex-agent-container (...)\n\n"
        "  Good:\n"
        "    1. 起動を完了しました。\n\n"
        "    2. lead に ACK を返しました。\n\n"
        "    3. 次の指示を待機します。\n\n"
        "Rare one-off override: set env CC_ALLOW_UNSTRUCTURED_TG=1.\n"
    )
    sys.exit(2)

sys.exit(0)
'
exit $?
