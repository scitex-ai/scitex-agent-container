"""The NEAR-PROMPT discriminator, MUTATION-PROVEN in both directions.

The discriminator has to do two things at once, and a suite that only checks one
of them cannot tell a real improvement from a loosened threshold:

  1. CATCH the agent that is wedged while its pane still ANIMATES. This is the
     case the deployed detector misses and the operator restarts by hand.
  2. NOT catch the agent that merely QUOTES the banner while working. This is
     what stops us bouncing healthy agents and destroying their context.

Each direction is proven against a REFERENCE IMPLEMENTATION of the rule it
replaces, re-implemented here from the real source so the comparison is a fact
about behaviour rather than a claim in a docstring:

  * :func:`_frozen_whole_pane_stuck` mirrors the deployed
    ``~/.scitex/agent-container/bin/tui_auth_detect.py`` (SHA-1 of the
    volatile-stripped pane, frozen across two runs, plus a banner present). It
    must MISS the animating fixture — that miss IS the defect being fixed.
  * :func:`_bare_substring_stuck` mirrors the naive matcher
    (``_state/_meta/pane._classify_pane_state``: the phrase anywhere on the
    pane). It must FALSELY FLAG the quoting fixture — which is what makes
    direction 2 load-bearing rather than decorative.

Running the old rules here is the point: a change that silently reintroduced a
freeze test, or that flagged on a bare substring, turns one of these red.
Without them, "we improved the discriminator" would be evidence that could not
have disagreed with us.

No mocks: every case is a pure function call on captured pane text.
"""

from __future__ import annotations

import hashlib
import re

import pytest

from scitex_agent_container._authheal._nearprompt import (
    VERDICT_LOGIN_REQUIRED,
    VERDICT_OK,
    VERDICT_UNKNOWN,
    WHY_NEAR_PROMPT,
    WHY_NO_PROMPT_LINE,
    WHY_PANE_UNREADABLE,
    WHY_SCROLLBACK_ONLY,
    classify_pane,
    classify_panes,
)
from scitex_agent_container._runners._tmux.auth_status import (
    banner_kind,
    evaluate,
    probe_to_state,
)

# ---------------------------------------------------------------------------
# Fixtures — verbatim TUI shapes.
# ---------------------------------------------------------------------------

_CHROME = """\
────────────────────────────────────────────────
❯
────────────────────────────────────────────────
  Opus 4.8 | ctx:56% | 5h:49%
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

# THE CASE THAT IS CURRENTLY MISSED.
#
# A wedged agent in a retry loop. The banner is the last thing its TUI rendered
# and never leaves the tail, but the pane MOVES between the two captures: the
# loop wakes and prints a retry countdown UNDER the banner. That line matches no
# volatile/chrome pattern, so it both changes the pane signature AND pushes the
# banner's distance from 0 to 1 — defeating the deployed whole-pane freeze and
# `is_stuck`'s distance freeze alike, while the agent sits there doing nothing.
_ANIMATING_1 = "⏺ Login expired · Please run /login\n" + _CHROME
_ANIMATING_2 = (
    "⏺ Login expired · Please run /login\n  Retrying request in 47 seconds\n" + _CHROME
)

# THE CASE THAT MUST NEVER BE FLAGGED.
#
# A healthy agent quoting the banner verbatim while it writes about the
# incident. It kept producing output afterwards, so its own prose pushed the
# quote up out of the near-prompt tail: the banner is scrollback TEXT, not the
# current UI state.
_QUOTING = (
    """\
⏺ I looked into it. The agent died with:

  Login expired · Please run /login

  That string is a 401, not an expiry. A sibling agent's OAuth refresh
  consumed the single-use refresh_token and revoked the token this one
  still held in memory, so nothing actually expired.

  The cure is a restart, not a /login — Claude never re-reads its
  credentials once it has started.

  I have carded it as sac-auth-401-incident, pinged the owner, and
  attached the pane capture to the incident notes.
"""
    + _CHROME
)

# A clean working agent: no auth text anywhere.
_CLEAN = (
    """\
⏺ Ran the targeted suite: 24 passed, 0 failed.

  Pushed as feat/near-prompt-discriminator.
"""
    + _CHROME
)

# A pane with no locatable input prompt — mid-redraw, or a runtime that is not
# the Ink TUI at all. We do not know where the current UI state ends.
_NO_PROMPT = "⏺ Login expired · Please run /login\n  ... rendering ...\n"


# ---------------------------------------------------------------------------
# Reference implementations of the rules being replaced (mutation controls).
# ---------------------------------------------------------------------------

# Verbatim from the deployed tui_auth_detect.py, so the control is the real rule
# rather than a strawman of it.
_DEPLOYED_VOLATILE_RE = re.compile(
    r"Usage\s|Context\s"
    r"|ctx:\s*\d+%|\b\d+h:\s*\d+%"
    r"|\(\d+h\s*\d+m|\(\d+m\s*\d+s|\(\d+s\b"
    r"|Baked for|Fermenting|Pondering|Simmering|Percolating"
    r"|Running scheduled task|Calling |esc to interrupt|ctrl\+o"
    r"|↓|↑|tokens\)|Tip:"
)


def _pane_signature(pane: str) -> str:
    """SHA-1 of the pane with volatile chrome + blank lines removed."""
    kept = [
        line.rstrip()
        for line in pane.splitlines()
        if line.strip() and not _DEPLOYED_VOLATILE_RE.search(line)
    ]
    return hashlib.sha1("\n".join(kept).encode("utf-8", "replace")).hexdigest()


def _banner_anywhere(pane: str) -> bool:
    return any(banner_kind(line) is not None for line in pane.splitlines())


def _frozen_whole_pane_stuck(pane1: str, pane2: str) -> bool:
    """The DEPLOYED rule: banner present AND whole-pane signature frozen."""
    return _banner_anywhere(pane2) and _pane_signature(pane1) == _pane_signature(pane2)


def _bare_substring_stuck(pane: str) -> bool:
    """The NAIVE rule: the phrase appears anywhere on the pane."""
    return "Login expired" in pane


@pytest.fixture
def animating_finding():
    return classify_pane("wedged", _ANIMATING_2)


@pytest.fixture
def quoting_finding():
    return classify_pane("quoting", _QUOTING)


# ---------------------------------------------------------------------------
# Direction 1 — the animating wedged agent MUST be caught.
# ---------------------------------------------------------------------------


def test_animating_wedged_agent_is_verdict_login_required(animating_finding):
    # Arrange
    finding = animating_finding
    # Act
    verdict = finding.verdict
    # Assert
    assert verdict == VERDICT_LOGIN_REQUIRED


def test_animating_wedged_agent_is_flagged_for_the_near_prompt_reason(
    animating_finding,
):
    # Arrange
    finding = animating_finding
    # Act
    why = finding.why
    # Assert
    assert why == WHY_NEAR_PROMPT


def test_animating_wedged_agent_reports_the_matched_banner(animating_finding):
    # Arrange
    finding = animating_finding
    # Act
    banner = finding.banner
    # Assert
    assert banner == "Login expired"


def test_wedged_agent_is_detected_before_it_animates_too():
    """The FIRST capture is decisive: no second reading is needed at all."""
    # Arrange
    pane = _ANIMATING_1
    # Act
    finding = classify_pane("wedged", pane)
    # Assert
    assert finding.login_required is True


def test_the_animating_fixture_really_does_change_between_captures():
    """Guard the FIXTURE: were the two captures ever to become identical, the
    mutation control below would pass for the wrong reason — agreeing with a
    frozen pane rather than missing a moving one."""
    # Arrange
    first, second = _ANIMATING_1, _ANIMATING_2
    # Act
    same_signature = _pane_signature(first) == _pane_signature(second)
    # Assert
    assert same_signature is False


def test_mutation_deployed_frozen_whole_pane_rule_MISSES_the_animating_agent():
    """MUTATION EVIDENCE, direction 1.

    The rule this discriminator replaces classifies the very same wedged agent
    as healthy, because it asks whether the PANE moved instead of whether the
    BANNER is the current UI state. That gap is the defect; pairing this with
    ``test_animating_wedged_agent_is_verdict_login_required`` is what proves the
    new rule closes it rather than merely restating it.
    """
    # Arrange
    first, second = _ANIMATING_1, _ANIMATING_2
    # Act
    old_rule_says_stuck = _frozen_whole_pane_stuck(first, second)
    # Assert
    assert old_rule_says_stuck is False


def test_mutation_distance_frozen_rule_ALSO_misses_the_animating_agent():
    """The softer in-repo freeze (``is_stuck``) misses it too.

    ``restart-login-expired`` uses that rule, so the miss is not confined to the
    deployed dotfiles script — it is in sac as well, which is why the fix had to
    be a new discriminator rather than a wider volatile-chrome regex.
    """
    # Arrange
    probe1, _ = evaluate(_ANIMATING_1, None)
    # Act
    _probe2, stuck = evaluate(_ANIMATING_2, probe_to_state(probe1))
    # Assert
    assert stuck is False


def test_the_animation_is_what_moves_the_banner_away_from_the_prompt():
    """Name the MECHANISM of the miss, so a fixture drift cannot hide it."""
    # Arrange
    probe1, _ = evaluate(_ANIMATING_1, None)
    probe2, _ = evaluate(_ANIMATING_2, probe_to_state(probe1))
    # Act
    distances = (probe1.distance, probe2.distance)
    # Assert
    assert distances == (0, 1)


# ---------------------------------------------------------------------------
# Direction 2 — the quoting agent MUST NOT be caught.
# ---------------------------------------------------------------------------


def test_agent_quoting_the_banner_is_not_login_required(quoting_finding):
    # Arrange
    finding = quoting_finding
    # Act
    verdict = finding.verdict
    # Assert
    assert verdict == VERDICT_OK


def test_agent_quoting_the_banner_is_cleared_for_the_scrollback_reason(
    quoting_finding,
):
    # Arrange
    finding = quoting_finding
    # Act
    why = finding.why
    # Assert
    assert why == WHY_SCROLLBACK_ONLY


def test_quoting_agents_log_line_still_names_the_phrase_that_was_ruled_out(
    quoting_finding,
):
    """A verdict of "ok, nothing seen" would be unauditable afterwards."""
    # Arrange
    finding = quoting_finding
    # Act
    banner = finding.banner
    # Assert
    assert banner == "Login expired"


def test_mutation_bare_substring_rule_FALSELY_FLAGS_the_quoting_agent():
    """MUTATION EVIDENCE, direction 2.

    Dropping the freeze test is only safe because something else rejects prose.
    A matcher that merely looked for the phrase would bounce this healthy agent
    and destroy its context, so this is what proves the near-prompt geometry —
    not the freeze — is doing that work.
    """
    # Arrange
    pane = _QUOTING
    # Act
    naive_rule_says_stuck = _bare_substring_stuck(pane)
    # Assert
    assert naive_rule_says_stuck is True


def test_quoted_banner_is_a_real_matchable_banner_not_merely_unmatched():
    """Guard the FIXTURE the other way.

    The quoting agent must be rejected for the RIGHT reason — the banner is real
    and matchable, it is simply too far above the prompt. Were the fixture to
    drift into being rejected because the line stopped matching at all, the test
    would stay green while proving nothing about near-prompt geometry.
    """
    # Arrange
    pane = _QUOTING
    # Act
    matchable = _banner_anywhere(pane)
    # Assert
    assert matchable is True


def test_clean_agent_with_no_auth_text_is_ok():
    # Arrange
    pane = _CLEAN
    # Act
    finding = classify_pane("clean", pane)
    # Assert
    assert finding.verdict == VERDICT_OK


def test_clean_agent_reports_no_banner():
    # Arrange
    pane = _CLEAN
    # Act
    finding = classify_pane("clean", pane)
    # Assert
    assert finding.banner is None


# ---------------------------------------------------------------------------
# Tri-state — never "healthy", never "wedged".
# ---------------------------------------------------------------------------


def test_unreadable_pane_is_unknown():
    # Arrange
    pane = None
    # Act
    finding = classify_pane("unreadable", pane)
    # Assert
    assert finding.verdict == VERDICT_UNKNOWN


def test_unreadable_pane_says_why_it_could_not_be_read():
    # Arrange
    pane = None
    # Act
    finding = classify_pane("unreadable", pane)
    # Assert
    assert finding.why == WHY_PANE_UNREADABLE


def test_unreadable_pane_is_never_restarted():
    # Arrange
    pane = None
    # Act
    finding = classify_pane("unreadable", pane)
    # Assert
    assert finding.login_required is False


def test_pane_without_a_prompt_line_is_unknown_despite_carrying_a_banner():
    """No anchor ⇒ no way to say the banner is in the current UI state.

    This pane CONTAINS the banner, so the tempting answer is LOGIN-REQUIRED. But
    without a prompt line we cannot tell a rendered banner from scrollback, and
    guessing would restart on evidence we do not have.
    """
    # Arrange
    pane = _NO_PROMPT
    # Act
    finding = classify_pane("half-drawn", pane)
    # Assert
    assert finding.verdict == VERDICT_UNKNOWN


def test_pane_without_a_prompt_line_says_the_anchor_was_missing():
    # Arrange
    pane = _NO_PROMPT
    # Act
    finding = classify_pane("half-drawn", pane)
    # Assert
    assert finding.why == WHY_NO_PROMPT_LINE


def test_pane_without_a_prompt_line_is_never_restarted():
    # Arrange
    pane = _NO_PROMPT
    # Act
    finding = classify_pane("half-drawn", pane)
    # Assert
    assert finding.login_required is False


# ---------------------------------------------------------------------------
# The sweep-level shape.
# ---------------------------------------------------------------------------


def test_classify_panes_sorts_findings_by_agent_name():
    # Arrange
    panes = {"wedged": _ANIMATING_2, "quoting": _QUOTING, "clean": _CLEAN}
    # Act
    findings = classify_panes(panes)
    # Assert
    assert [f.agent for f in findings] == ["clean", "quoting", "wedged"]


def test_classify_panes_partitions_every_agent_into_exactly_one_verdict():
    # Arrange
    panes = {
        "wedged": _ANIMATING_2,
        "quoting": _QUOTING,
        "unreadable": None,
        "clean": _CLEAN,
    }
    # Act
    verdicts = {f.agent: f.verdict for f in classify_panes(panes)}
    # Assert
    assert verdicts == {
        "clean": VERDICT_OK,
        "quoting": VERDICT_OK,
        "unreadable": VERDICT_UNKNOWN,
        "wedged": VERDICT_LOGIN_REQUIRED,
    }


def test_every_finding_carries_a_machine_readable_why():
    """The field the deployed script dropped into a cache nobody reads."""
    # Arrange
    panes = {"wedged": _ANIMATING_2, "quoting": _QUOTING, "unreadable": None}
    # Act
    whys = [f.to_dict()["why"] for f in classify_panes(panes)]
    # Assert
    assert all(whys)


def test_every_finding_detail_names_the_agent_it_is_about():
    # Arrange
    panes = {"wedged": _ANIMATING_2, "quoting": _QUOTING, "unreadable": None}
    # Act
    findings = classify_panes(panes)
    # Assert
    assert all(f.agent in f.detail for f in findings)
