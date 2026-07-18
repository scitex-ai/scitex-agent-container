"""Prompt-anchored TUI auth-banner matcher — near-prompt + distance-frozen.

Exercises the hardened matcher (``_runners/_tmux/auth_status``) against BOTH
real cases from the 2026-07-12 live verification:

  * scitex-hpc  — a REAL wedged agent: the "Login expired * Please run /login"
    banner sits directly above the prompt and stays frozen (same kind, same
    distance) across two captures even as the 0s-turn spinner ticks
    → LOGIN-REQUIRED.
  * scitex-todo — a PROSE false-positive: the agent quotes the banner while
    replying about the incident, with more output after it, so the banner is
    high in scrollback (outside the near-prompt tail) and the pane keeps
    moving → OK.

Plus the real captured pane fixture (``auth_error_head-mba_v2.1.114.txt``) so
the matcher is proven against Anthropic's actual rendering, not a guess, and
``auth_wedged_grant_20260718.txt`` — a second real capture, of an agent still
wedged 6 minutes after a restart — as a WEDGED-agent regression guard.

The Unicode-whitespace section guards a false NEGATIVE rather than a false
positive: the TUI substitutes U+00A0 for ASCII spaces (visible in the ``❯``
prompt gap of every captured fixture here), and a substitution INSIDE an auth
phrase defeats the start-anchored literal match, hiding a wedged agent.

No mocks: every case is a pure function call on captured pane text.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._runners._tmux import prompts as P
from scitex_agent_container._runners._tmux.auth_status import (
    TAIL_LINES,
    banner_kind,
    evaluate,
    is_stuck,
    probe_pane,
    probe_to_state,
)

_REAL_PANE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "pane_states"
    / "auth_error_head-mba_v2.1.114.txt"
)

# Real captured pane of the wedged ``grant`` agent (2026-07-18 22:26:19), taken
# ~6 minutes after its 22:20:14 restart. The operator poked it twice and got the
# banner both times; the banner is the non-chrome line DIRECTLY above the prompt
# and only the 0s-turn spinner follows it. Telegram chat id / attachment file_id
# are redacted — the auth-relevant structure is verbatim, including the real
# NBSP the TUI renders as the ``❯`` prompt gap.
_WEDGED_PANE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "pane_states"
    / "auth_wedged_grant_20260718.txt"
)

# U+00A0 NON-BREAKING SPACE — the variant Claude's Ink TUI is CAPTURED emitting
# where an ASCII space is expected (the ``❯`` prompt gap in every fixture here,
# and the gap after the ``⎿`` result marker in the head-mba pane).
NBSP = " "

# --------------------------------------------------------------------------
# Fixtures — captured-pane shapes (verbatim TUI rendering).
# --------------------------------------------------------------------------

# REAL wedged agent (scitex-hpc): banner directly above the prompt; the
# volatile 0s-turn spinner sits between it and the prompt (must be stripped so
# distance stays 0). Two runs differ ONLY in the spinner verb — still frozen.
_HPC_1 = """\
● Login expired · Please run /login
✻ Sautéed for 0s
────────────────────────────────────────────────
❯
────────────────────────────────────────────────
  Opus 4.8 | ctx:56% | 5h:49%
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""
_HPC_2 = _HPC_1.replace("Sautéed for 0s", "Simmered for 0s")

# PROSE false-positive (scitex-todo): the exact banner is QUOTED high in the
# conversation, then the agent keeps replying — so it is outside the last
# TAIL_LINES non-chrome lines above the prompt.
_PROSE_HIGH = """\
● Investigating the fleet auth incident now. The system banner was:
  ⎿  Login expired · Please run /login
● I checked every flagged agent against the real rendering.
  Two were false positives — they quoted the phrase in prose while
  actively replying to operator DMs, pane still moving.
  The one real case was scitex-hpc: frozen, banner right above the
  prompt, zero-second turns on every /loop wakeup.
  The distinguisher is distance-from-prompt plus a cross-run frozen check.
  I am drafting the hardened matcher in the sac package now.
  It reuses prompts for the prompt anchor and pane capture for the read.
────────────────────────────────────────────────
❯
────────────────────────────────────────────────
  [Opus 4.8 | Max] │ scitex-todo git:(develop)
  Context ██░░░░░░░░ 23% │ Usage ███░░░░░░░ 33% (1h 50m / 5h)
"""

# Banner IS in the near-prompt tail but the pane MOVES (agent produced a new
# line) → distance changes → NOT frozen. Exercises the distance refinement.
_MOVING_1 = """\
● Login expired · Please run /login
  Retrying the scheduled task now.
────────────────────────────────────────────────
❯
────────────────────────────────────────────────
  Opus 4.8 | ctx:56% | 5h:49%
"""
_MOVING_2 = """\
● Login expired · Please run /login
  Retrying the scheduled task now.
  Still failing with the same banner.
────────────────────────────────────────────────
❯
────────────────────────────────────────────────
  Opus 4.8 | ctx:56% | 5h:49%
"""

# Healthy idle agent — no banner anywhere.
_HEALTHY = """\
  I'll continue with the coverage fix now.
────────────────────────────────────────────────
❯
────────────────────────────────────────────────
  [Sonnet 5 | Max] │ fix-coverage git:(fix/cov*)
  Context ██░░░░░░░░ 19% │ Usage ██░░░░░░░░ 24% (1h 32m / 5h)
  ⏵⏵ bypass permissions on · 1 shell · ← for agents
"""

# Classic discuss-the-incident prose: every trigger phrase, none at line-start.
_PROSE_INLINE = """\
● figrecipe died in a "Login expired" loop; the banner said Please run /login
  but a valid token existed. invalid_grant / authentication_error are the
  server-side symptoms. I flagged 2 agents showing Login expired on screen.
────────────────────────────────────────────────
❯
────────────────────────────────────────────────
  [Opus 4.8 | Max] │ scitex-agent-container git:(develop)
  Context ██░░░░░░░░ 23% │ Usage ███░░░░░░░ 33% (1h 50m / 5h)
"""


# --------------------------------------------------------------------------
# prompt_line_index — the reused prompt anchor.
# --------------------------------------------------------------------------


def test_prompt_line_index_picks_bottom_most_prompt():
    # Arrange
    pane = "❯\n────\nold output\n────\n❯\n────\n"
    # Act
    idx = P.prompt_line_index(pane)
    # Assert
    assert idx == 4


def test_prompt_line_index_ignores_mid_sentence_marker():
    # Arrange
    pane = "  the banner sits above the ❯ prompt when frozen\nplain line\n"
    # Act
    idx = P.prompt_line_index(pane)
    # Assert
    assert idx is None


def test_prompt_line_index_real_pane_lands_on_prompt_line():
    # Arrange
    pane = _REAL_PANE.read_text(encoding="utf-8")
    # Act
    idx = P.prompt_line_index(pane)
    # Assert
    assert pane.splitlines()[idx].strip().startswith("❯")


def test_prompt_line_index_real_pane_picks_bottom_box():
    # Arrange
    lines = _REAL_PANE.read_text(encoding="utf-8").splitlines()
    # Act
    idx = P.prompt_line_index("\n".join(lines))
    # Assert
    assert idx == max(i for i, ln in enumerate(lines) if ln.strip().startswith("❯"))


# --------------------------------------------------------------------------
# banner_kind — start-anchored, prose-rejecting.
# --------------------------------------------------------------------------


def test_banner_kind_login_expired_after_marker_strip():
    # Arrange
    line = "  ⎿  Login expired · Please run /login"
    # Act
    kind = banner_kind(line)
    # Assert
    assert kind == "Login expired"


def test_banner_kind_not_logged_in_with_bullet_marker():
    # Arrange
    line = "● Not logged in · Please run /login"
    # Act
    kind = banner_kind(line)
    # Assert
    assert kind == "Not logged in"


def test_banner_kind_please_run_login_with_trailing_401():
    # Arrange
    line = '  ⎿  Please run /login · API Error: 401 {"type":"error"}'
    # Act
    kind = banner_kind(line)
    # Assert
    assert kind == "Please run /login"


def test_banner_kind_standalone_api_error_401():
    # Arrange
    line = '  API Error: 401 {"type":"error"}'
    # Act
    kind = banner_kind(line)
    # Assert
    assert kind == "API Error: 4xx"


def test_banner_kind_rejects_prose_mentioning_the_phrase():
    # Arrange
    line = '● figrecipe died in a "Login expired" loop; Please run /login'
    # Act
    kind = banner_kind(line)
    # Assert
    assert kind is None


def test_banner_kind_ignores_rate_limit_429():
    # Arrange
    line = "  API Error: 429 rate_limit"
    # Act
    kind = banner_kind(line)
    # Assert
    assert kind is None


# --------------------------------------------------------------------------
# banner_kind — Unicode-whitespace normalisation (ported from
# ``--ecc-state-detection--normalize-text`` in emacs-claude-code).
#
# ``_MARKERS`` already strips a LEADING NBSP, so a NBSP in the left decoration
# has always worked. A NBSP INSIDE the phrase does not: ``startswith`` compares
# against ASCII-spaced literals, so "Login<NBSP>expired" matched nothing and a
# wedged agent would be invisible to the watchdog.
# --------------------------------------------------------------------------


def test_banner_kind_matches_nbsp_inside_login_expired():
    # Arrange
    line = "● Login" + NBSP + "expired · Please run /login"
    # Act
    kind = banner_kind(line)
    # Assert
    assert kind == "Login expired"


def test_banner_kind_matches_nbsp_inside_please_run_login():
    # Arrange
    line = "  ⎿  Please" + NBSP + "run /login"
    # Act
    kind = banner_kind(line)
    # Assert
    assert kind == "Please run /login"


def test_banner_kind_matches_nbsp_inside_api_error_401():
    # Arrange
    line = "  API" + NBSP + 'Error: 401 {"type":"error"}'
    # Act
    kind = banner_kind(line)
    # Assert
    assert kind == "API Error: 4xx"


def test_banner_kind_matches_ideographic_space_in_left_decoration():
    # Arrange — U+3000 is NOT in ``_MARKERS``, so stripping alone leaves it
    # leading and the start-anchored match fails; normalisation must run FIRST.
    line = "⎿　Please run /login"
    # Act
    kind = banner_kind(line)
    # Assert
    assert kind == "Please run /login"


def test_banner_kind_normalisation_does_not_widen_into_prose():
    # Arrange — CONTROL: proves the fix was not "achieved" by loosening the
    # start-anchored match into a substring search.
    line = '● figrecipe died in a "Login expired" loop'
    # Act
    kind = banner_kind(line)
    # Assert
    assert kind is None


def test_banner_kind_normalisation_still_ignores_nbsp_rate_limit_429():
    # Arrange — CONTROL: normalising the API-error branch must not widen it
    # onto 429, which a restart does not fix.
    line = "  API" + NBSP + "Error: 429 rate_limit"
    # Act
    kind = banner_kind(line)
    # Assert
    assert kind is None


# --------------------------------------------------------------------------
# probe_pane — near-prompt tail membership + distance.
# --------------------------------------------------------------------------


def test_probe_real_wedged_banner_present_at_distance_zero():
    # Arrange
    pane = _HPC_1
    # Act
    probe = probe_pane(pane)
    # Assert
    assert (probe.present, probe.distance, probe.banner) == (True, 0, "Login expired")


def test_probe_prose_quote_high_is_outside_tail():
    # Arrange
    pane = _PROSE_HIGH
    # Act
    probe = probe_pane(pane)
    # Assert
    assert (probe.present, probe.distance) == (False, None)


def test_probe_inline_prose_is_not_a_banner():
    # Arrange
    pane = _PROSE_INLINE
    # Act
    probe = probe_pane(pane)
    # Assert
    assert probe.present is False


def test_probe_healthy_pane_has_no_banner():
    # Arrange
    pane = _HEALTHY
    # Act
    probe = probe_pane(pane)
    # Assert
    assert probe.present is False


def test_probe_real_captured_fixture_flags_banner():
    # Arrange
    pane = _REAL_PANE.read_text(encoding="utf-8")
    # Act
    probe = probe_pane(pane)
    # Assert
    assert (probe.present, probe.banner) == (True, "Please run /login")


def test_probe_real_captured_fixture_banner_within_tail():
    # Arrange
    pane = _REAL_PANE.read_text(encoding="utf-8")
    # Act
    probe = probe_pane(pane)
    # Assert
    assert probe.distance is not None and probe.distance <= TAIL_LINES


def test_probe_wedged_grant_pane_flags_banner_at_distance_zero():
    # Arrange — regression guard for a WEDGED agent, not a healthy one: in this
    # capture the banner is the last non-chrome line before the prompt.
    pane = _WEDGED_PANE.read_text(encoding="utf-8")
    # Act
    probe = probe_pane(pane)
    # Assert
    assert (probe.present, probe.distance, probe.banner) == (True, 0, "Login expired")


def test_probe_wedged_grant_pane_prompt_gap_is_a_real_nbsp():
    # Arrange — documents WHY normalisation is needed: the production TUI emits
    # U+00A0 where an ASCII space is expected.
    lines = _WEDGED_PANE.read_text(encoding="utf-8").splitlines()
    # Act
    prompt_line = lines[P.prompt_line_index("\n".join(lines))]
    # Assert
    assert prompt_line == "❯" + NBSP


# --------------------------------------------------------------------------
# is_stuck / evaluate — the cross-run distance-frozen decision.
# --------------------------------------------------------------------------


def test_first_capture_is_never_stuck():
    # Arrange
    pane = _HPC_1
    # Act
    _probe, stuck = evaluate(pane, None)
    # Assert
    assert stuck is False


def test_frozen_banner_across_two_runs_is_stuck():
    # Arrange
    probe1, _ = evaluate(_HPC_1, None)
    # Act
    probe2, stuck = evaluate(_HPC_2, probe_to_state(probe1))
    # Assert
    assert (stuck, probe2.distance == probe1.distance) == (True, True)


def test_moving_banner_in_tail_is_not_stuck():
    # Arrange
    probe1, _ = evaluate(_MOVING_1, None)
    # Act
    probe2, stuck = evaluate(_MOVING_2, probe_to_state(probe1))
    # Assert
    assert (probe1.distance, probe2.distance, stuck) == (1, 2, False)


def test_moving_banner_stays_present_in_tail():
    # Arrange
    probe1, _ = evaluate(_MOVING_1, None)
    # Act
    probe2, _stuck = evaluate(_MOVING_2, probe_to_state(probe1))
    # Assert
    assert (probe1.present, probe2.present) == (True, True)


def test_prose_quote_never_stuck_across_runs():
    # Arrange
    probe1, _ = evaluate(_PROSE_HIGH, None)
    # Act
    _probe2, stuck = evaluate(_PROSE_HIGH, probe_to_state(probe1))
    # Assert
    assert stuck is False


def test_real_fixture_frozen_two_reads_is_stuck():
    # Arrange
    pane = _REAL_PANE.read_text(encoding="utf-8")
    probe1, _ = evaluate(pane, None)
    # Act
    _probe2, stuck = evaluate(pane, probe_to_state(probe1))
    # Assert
    assert stuck is True


def test_is_stuck_without_prior_state_is_false():
    # Arrange
    probe = probe_pane(_HPC_1)
    # Act
    stuck = is_stuck(probe, None)
    # Assert
    assert stuck is False


def test_is_stuck_with_matching_prior_state_is_true():
    # Arrange
    probe = probe_pane(_HPC_1)
    # Act
    stuck = is_stuck(probe, probe_to_state(probe))
    # Assert
    assert stuck is True


def test_uncapturable_pane_probe_is_not_present():
    # Arrange
    prev = {"present": True, "distance": 0, "banner": "Login expired"}
    # Act
    probe, _stuck = evaluate(None, prev)
    # Assert
    assert probe.present is False


def test_uncapturable_pane_is_never_stuck():
    # Arrange
    prev = {"present": True, "distance": 0, "banner": "Login expired"}
    # Act
    _probe, stuck = evaluate(None, prev)
    # Assert
    assert stuck is False
