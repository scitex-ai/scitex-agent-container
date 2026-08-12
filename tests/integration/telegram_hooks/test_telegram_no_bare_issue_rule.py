"""The operator's `#NNN(description)` rule, asserted on the real messages
this fleet actually sent — plus a MUTATION CHECK against the predicate
that used to be shipped.

Rule (operator 2026-08-11): a Telegram message must never carry an
issue/PR number he cannot read. He is on a phone; he cannot follow a
link and the number alone tells him nothing. His format, his words:
「ナンバーの後に ( をつけて説明する、っていうのをルールにしてください」
— put a parenthesis after the number and explain inside it. Both `(`
and the full-width `（` count, because a Japanese IME produces the
latter.

WHY A MUTATION CHECK IS PART OF THIS FILE. The hook already existed on
2026-06-09 and was already "enforcing" the rule — but its trigger only
fired when the WHOLE message reduced to bare `#NNN` tokens, so a number
inside a sentence sailed through. That is the recurring shape this fleet
keeps finding: A GUARD WHOSE TRIGGER CONDITION IS NARROWER THAN ITS
STATED RULE READS AS ENFORCEMENT WHILE ENFORCING ALMOST NOTHING. A test
suite that passes identically against the old and the new predicate
would have documented the same illusion, so
``test_mutation_old_narrow_predicate_flips_exactly_the_newly_enforced_cases``
runs the historical predicate over the same table and pins the exact
delta: seven cases flip refuse-ward, nothing flips the other way.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "telegram_hooks"
    / "enforce_telegram_no_bare_issue.sh"
)

_REPLY_TOOL = "mcp__claude-code-telegrammer__reply"

_BLOCK = 2
_ALLOW = 0

# (case_id, tool_name, message_text, expected_rc_under_the_shipped_hook)
_CASES: list[tuple[str, str, str, int]] = [
    # --- the real messages, named in the operator's own complaint -----
    (
        "real_prompted_the_rule_967",
        _REPLY_TOOL,
        "lead a2a のグループ判定を修正して #967 を出しました",
        _BLOCK,
    ),
    (
        "real_tonight_970_no_description",
        _REPLY_TOOL,
        "#970 の話ではなく、その前段のスペック読みの話です",
        _BLOCK,
    ),
    (
        "real_compliant_970_fullwidth_paren",
        _REPLY_TOOL,
        "#970（グループ判定がスペックではなくDBを読む）を出しました",
        _ALLOW,
    ),
    (
        "real_github_url",
        _REPLY_TOOL,
        "マージしました "
        "https://github.com/ywatanabe1989/scitex-agent-container/pull/970",
        _ALLOW,
    ),
    ("real_no_numbers_at_all", _REPLY_TOOL, "CI is green, nothing blocking", _ALLOW),
    # --- the required form ---------------------------------------------
    (
        "ascii_paren_with_space",
        _REPLY_TOOL,
        "#970 (group authority reads the spec)",
        _ALLOW,
    ),
    ("empty_parenthetical_describes_nothing", _REPLY_TOOL, "#970 ()", _BLOCK),
    # --- embedded in a sentence: the whole point of the tightening ------
    (
        "mid_sentence_578",
        _REPLY_TOOL,
        "調べていて見つかった #578、これはまだ直っていません",
        _BLOCK,
    ),
    # --- a repo name is not a description --------------------------------
    ("cross_repo_bare", _REPLY_TOOL, "scitex-dev #578", _BLOCK),
    ("cross_repo_described", _REPLY_TOOL, "scitex-dev #578（型が合わない）", _ALLOW),
    # --- URLs are not references -----------------------------------------
    (
        "url_numeric_fragment",
        _REPLY_TOOL,
        "見てください https://example.com/page#123",
        _ALLOW,
    ),
    (
        "url_issuecomment_fragment",
        _REPLY_TOOL,
        "https://github.com/o/r/pull/970#issuecomment-123",
        _ALLOW,
    ),
    (
        "url_then_bare_number",
        _REPLY_TOOL,
        "https://github.com/o/r/pull/970 と #970 の件",
        _BLOCK,
    ),
    # --- a repeat inherits the first description, left to right ----------
    (
        "repeat_described_then_bare",
        _REPLY_TOOL,
        "#970（グループ判定の修正）を出した。#970 のCIは緑",
        _ALLOW,
    ),
    (
        "repeat_bare_then_described",
        _REPLY_TOOL,
        "#970 のCIは緑。#970（グループ判定の修正）",
        _BLOCK,
    ),
    # --- everything the 2026-06-09 hook blocked must still block ---------
    ("legacy_bare_single", _REPLY_TOOL, "#162", _BLOCK),
    ("legacy_bare_in_brackets", _REPLY_TOOL, "[#162]", _BLOCK),
    ("legacy_two_bare", _REPLY_TOOL, "#162 #163", _BLOCK),
    # --- out of scope ----------------------------------------------------
    ("non_telegram_tool_ignored", "Bash", "#162", _ALLOW),
    ("empty_text", _REPLY_TOOL, "", _ALLOW),
]

# The cases the 2026-06-09 predicate let through and the tightened one
# refuses. Frozen deliberately: if this set grows or shrinks, the
# tightening changed scope and a human should say whether that is wanted.
_NEWLY_ENFORCED = {
    "real_prompted_the_rule_967",
    "real_tonight_970_no_description",
    "empty_parenthetical_describes_nothing",
    "mid_sentence_578",
    "cross_repo_bare",
    "url_then_bare_number",
    "repeat_bare_then_described",
}

# The predicate as it was shipped from 2026-06-09 to 2026-08-11, quoted
# verbatim from the script it lived in (only the stderr copy is dropped —
# it never influenced the exit code). This is the MUTANT: the test runs
# the same table through it to prove the new cases are genuinely new.
_OLD_NARROW_PREDICATE = '''
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
stripped = text.strip()
# Strip outer brackets if the WHOLE message is wrapped.
inner = stripped
if (inner.startswith("[") and inner.endswith("]")) or (
    inner.startswith("(") and inner.endswith(")")
):
    inner = inner[1:-1].strip()
# Tokenise: are ALL tokens bare #NNN?
tokens = inner.split()
if not tokens:
    sys.exit(0)
bare_issue = re.compile(r"^#\\d+$")
if all(bare_issue.match(tok) for tok in tokens):
    sys.stderr.write("BLOCKED (2026-06-09 predicate)\\n")
    sys.exit(2)
sys.exit(0)
'''


def _payload(tool: str, text: str) -> str:
    return json.dumps({"tool_name": tool, "tool_input": {"chat_id": "1", "text": text}})


def _env_with_python_on_path() -> dict[str, str]:
    """The hook shells ``python3``; guarantee the running interpreter's
    bin dir is reachable so the test measures the PREDICATE and never a
    PATH accident."""
    import os

    env = dict(os.environ)
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    env.pop("CC_ALLOW_BARE_ISSUE", None)
    return env


def _run_shipped_hook(tool: str, text: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_HOOK)],
        input=_payload(tool, text),
        capture_output=True,
        text=True,
        check=False,
        env=_env_with_python_on_path(),
    )


def _run_old_predicate(mutant: Path, tool: str, text: str) -> int:
    return subprocess.run(
        [sys.executable, str(mutant)],
        input=_payload(tool, text),
        capture_output=True,
        text=True,
        check=False,
    ).returncode


def test_hook_script_is_present_and_executable():
    # Arrange — the deployment invariant the SDK depends on.
    is_file = _HOOK.is_file()
    is_executable = bool(_HOOK.stat().st_mode & 0o111) if is_file else False
    state = (is_file, is_executable)
    # Act — nothing to do; the filesystem is the measurement.
    # Assert
    assert state == (True, True), f"{_HOOK} must exist and be executable (is_file, +x)"


@pytest.mark.parametrize(
    ("case_id", "tool", "text", "want_rc"),
    _CASES,
    ids=[case[0] for case in _CASES],
)
def test_shipped_hook_enforces_the_parenthetical_rule(
    case_id: str, tool: str, text: str, want_rc: int
):
    # Arrange — the hook is the deployable itself, run as the SDK runs it.
    # Act
    result = _run_shipped_hook(tool, text)
    # Assert
    assert result.returncode == want_rc, (
        f"case {case_id}: got rc={result.returncode} want rc={want_rc}\n"
        f"stderr:\n{result.stderr[-600:]}"
    )


def test_refusal_names_the_token_the_form_and_a_corrected_example():
    # Arrange — a refusal that only says "blocked" costs the sender a
    # round trip to work out what the hook wanted.
    text = "#970 の話ではなく、その前段のスペック読みの話です"
    # Act
    stderr = _run_shipped_hook(_REPLY_TOOL, text).stderr
    # Assert
    present = (
        "#970" in stderr,  # the offending token
        "（" in stderr and "(" in stderr,  # both accepted paren forms
        "required:" in stderr,  # the required form
        "fix it to:" in stderr,  # a corrected example
        "CC_ALLOW_BARE_ISSUE" in stderr,  # the escape hatch
    )
    assert present == (True, True, True, True, True), stderr


def test_unexpected_payload_shape_fails_open():
    # Arrange — the header promises FAIL-OPEN on an unexpected payload; a
    # non-string `text` must not crash the hook into a noisy non-zero.
    # Kept out of the case table on purpose: it is a fail-open contract,
    # not one of the tightening's newly-refused cases.
    payload = json.dumps({"tool_name": _REPLY_TOOL, "tool_input": {"text": ["#970"]}})
    # Act
    result = subprocess.run(
        ["bash", str(_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
        env=_env_with_python_on_path(),
    )
    # Assert
    assert result.returncode == _ALLOW, result.stderr[-600:]


def test_mutation_old_narrow_predicate_flips_exactly_the_newly_enforced_cases(
    tmp_path: Path,
):
    """MUTATION CHECK. Restore the 2026-06-09 predicate and prove the
    delta is exactly ``_NEWLY_ENFORCED`` — a test that passed both before
    and after the tightening would be worth nothing."""
    # Arrange
    mutant = tmp_path / "old_narrow_predicate.py"
    mutant.write_text(_OLD_NARROW_PREDICATE, encoding="utf-8")
    # Act
    flipped = {
        case_id
        for case_id, tool, text, want_rc in _CASES
        if _run_old_predicate(mutant, tool, text) != want_rc
    }
    # Assert
    assert flipped == _NEWLY_ENFORCED, (
        "the old predicate's disagreement with the new expectations is not "
        f"the intended tightening.\n  only-old-differs: {flipped - _NEWLY_ENFORCED}\n"
        f"  expected-but-agreed: {_NEWLY_ENFORCED - flipped}"
    )


def test_mutation_tightening_never_permits_what_the_old_predicate_refused(
    tmp_path: Path,
):
    """The neighbouring guarantee: this hook was TIGHTENED, so no case may
    move from refuse to allow. A one-way delta is what makes the change
    safe to deploy without re-auditing every message the fleet sends."""
    # Arrange
    mutant = tmp_path / "old_narrow_predicate.py"
    mutant.write_text(_OLD_NARROW_PREDICATE, encoding="utf-8")
    # Act
    loosened = {
        case_id
        for case_id, tool, text, want_rc in _CASES
        if _run_old_predicate(mutant, tool, text) == _BLOCK and want_rc == _ALLOW
    }
    # Assert
    assert loosened == set(), (
        f"these cases were refused by the old predicate and are now allowed: {loosened}"
    )
