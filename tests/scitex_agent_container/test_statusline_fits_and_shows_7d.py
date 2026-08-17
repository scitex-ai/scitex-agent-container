"""The status line must FIT, and it must show the quota that actually kills agents.

OPERATOR, 2026-08-17: 「途中でトランケーションされてしまってんですよ。なので情報が
見えなくなってるのでこれなおせますかね。全部見えるように」 — it gets truncated
partway, so information is invisible; fix it so EVERYTHING is visible.

MEASURED the same day, rendering his live payload through the production
renderer: the line was 131 characters and his pane cut it at ~86, mid
``Opus 5 (1M conte…``. Terminal truncation eats from the RIGHT, so what was
discarded was ctx / 5h / account — every NUMBER — while the identity survived,
which is the one part a human already knows. Two redundancies were paying for
that: ``scitex-agent-container`` appeared TWICE (agent name, then workdir
basename, because sac repos are named after their agent), and the model carried
the payload's prose parenthetical verbatim.

AND 7d IS ADDED, which is the substantive half. scitex-hub ran pinned to an
account at 7d=100%, capped for days, answering "You've hit your weekly limit"
on every turn — while 5h read LOW and every other signal (SUCC, live tmux,
rendered TUI) said healthy. The pane displayed the reassuring number and hid
the fatal one. ``rate_limits.seven_day.used_percentage`` was in the payload the
whole time.

WHY THE WIDTH ASSERTIONS TEST AN INVARIANT, NOT A STRING: ``_render`` reads the
real hostname and the real account store, so an exact expected line would be a
test of THIS machine. ``len(line) <= STATUSLINE_MAX_WIDTH`` is the property the
operator actually asked for and it holds on every host.

PA-306: no mocks. Identity comes from the real env seam (``SAC_AGENT``), and
the payloads are real payload SHAPES captured from a live capture.
"""

from __future__ import annotations

import json

import pytest

import scitex_agent_container.statusline as sl_mod
from scitex_agent_container.statusline import (
    STATUSLINE_MAX_WIDTH,
    _render,
    _short_account,
    _short_host,
    _short_model,
)

# The exact shape of a live payload, captured 2026-08-17 from
# ~/.scitex/agent-container/statusline/scitex-agent-container.json.
_LIVE = {
    "model": {"id": "claude-opus-5[1m]", "display_name": "Opus 5 (1M context)"},
    "workspace": {"current_dir": "/home/ywatanabe/proj/scitex-agent-container"},
    "context_window": {"used_percentage": 47},
    "rate_limits": {
        "five_hour": {"used_percentage": 6},
        "seven_day": {"used_percentage": 21},
    },
}


@pytest.fixture
def named_agent(env_save_restore):
    """Pin the identity so the line is not at the mercy of ambient env."""
    env_save_restore.set("SAC_AGENT", "scitex-agent-container")
    return "scitex-agent-container"


# ---------------------------------------------------------------------------
# The operator's request: it must fit.
# ---------------------------------------------------------------------------


def test_the_budget_is_no_wider_than_a_standard_terminal():
    """Asserted against a LITERAL, because every other width test is not.

    The rest of this file compares ``len(line) <= STATUSLINE_MAX_WIDTH``, which
    is the property we want but is also parameterised by the very constant
    under test — set the constant to 10000 and all of them stay green while the
    operator's pane truncates exactly as before. This is the one assertion that
    can fail in that scenario, so widening the budget to make a line "fit" has
    to come through here and be argued for.
    """
    # Arrange
    standard_terminal = 80
    # Act
    budget = STATUSLINE_MAX_WIDTH
    # Assert
    assert budget <= standard_terminal


def test_the_live_payload_renders_within_the_width_budget(named_agent):
    """The 131-character line that prompted the request must now fit."""
    # Arrange
    data = dict(_LIVE)
    # Act
    line = _render(data)
    # Assert
    assert len(line) <= STATUSLINE_MAX_WIDTH


def test_the_live_payload_keeps_every_field_including_the_model(named_agent):
    """Fitting must not be achieved by quietly dropping fields.

    The operator asked for 全部見えるように — everything visible. A budget tight
    enough to force the sacrifice path on the ORDINARY case would satisfy the
    width assertion above while failing the actual request; at 78 this line lost
    its model. This pins the common case as whole.
    """
    # Arrange
    data = dict(_LIVE)
    # Act
    line = _render(data)
    # Assert
    assert "Opus 5 1M" in line


def test_an_absurdly_long_agent_name_still_fits(env_save_restore):
    """The budget is a guarantee, not a hope — clamping is ours, not the terminal's."""
    # Arrange
    env_save_restore.set("SAC_AGENT", "a" * 200)
    # Act
    line = _render(dict(_LIVE))
    # Assert
    assert len(line) <= STATUSLINE_MAX_WIDTH


def test_the_numbers_survive_when_the_identity_is_clamped(env_save_restore):
    """Identity is what gets sacrificed under pressure — never the data.

    The whole defect was that truncation discarded the volatile numbers and
    kept the guessable name. A clamp that repeated that would be no fix.
    """
    # Arrange
    env_save_restore.set("SAC_AGENT", "a" * 200)
    # Act
    line = _render(dict(_LIVE))
    # Assert
    assert "7d:21%" in line


def test_three_digit_percentages_still_fit(env_save_restore):
    """A capped account reads 100 — the widest the numbers ever get."""
    # Arrange
    env_save_restore.set("SAC_AGENT", "scitex-agent-container")
    data = dict(_LIVE)
    data["context_window"] = {"used_percentage": 100}
    data["rate_limits"] = {
        "five_hour": {"used_percentage": 100},
        "seven_day": {"used_percentage": 100},
    }
    # Act
    line = _render(data)
    # Assert
    assert len(line) <= STATUSLINE_MAX_WIDTH


# ---------------------------------------------------------------------------
# 7d — the field the incident was about.
# ---------------------------------------------------------------------------


def test_the_seven_day_quota_is_rendered(named_agent):
    """hub was at 7d=100% while 5h read low. The pane must show it."""
    # Arrange
    data = dict(_LIVE)
    # Act
    line = _render(data)
    # Assert
    assert "7d:21%" in line


def test_seven_day_is_omitted_when_the_payload_lacks_it(named_agent):
    """Absent is a third value: render nothing rather than a fabricated 0%."""
    # Arrange
    data = dict(_LIVE)
    data["rate_limits"] = {"five_hour": {"used_percentage": 6}}
    # Act
    line = _render(data)
    # Assert
    assert "7d" not in line


def test_a_payload_with_no_rate_limits_still_renders_context(named_agent):
    """Degrade cleanly: a missing section must not cost the whole line."""
    # Arrange
    data = {"model": {"display_name": "M"}, "context_window": {"used_percentage": 3}}
    # Act
    line = _render(data)
    # Assert
    assert "ctx:3%" in line


# ---------------------------------------------------------------------------
# The redundancies that were paying for the truncation.
# ---------------------------------------------------------------------------


def test_the_workdir_is_dropped_when_it_repeats_the_agent_name(named_agent):
    """sac repos are named after their agent — printing both costs 24 columns."""
    # Arrange
    data = dict(_LIVE)  # workspace basename == the agent name
    # Act
    occurrences = _render(data).count("scitex-agent-container")
    # Assert
    assert occurrences == 1


def test_a_workdir_that_differs_from_the_agent_is_kept_when_there_is_room(
    env_save_restore,
):
    """Dropping it UNCONDITIONALLY would hide a genuinely different location.

    Asserted against a minimal payload on purpose, and this is a real limit
    rather than a weakened test: under a FULL payload the same case does not
    fit 78 columns (it measures 84), so the workdir is sacrificed first by
    design — see the sacrifice order in ``_render``. The distinction this test
    locks is "dropped only under budget pressure" versus "never rendered at
    all", and only the second would be a bug. The companion test below pins
    the pressure case so both halves are stated.
    """
    # Arrange
    env_save_restore.set("SAC_AGENT", "some-agent")
    data = {
        "model": {"display_name": "M"},
        "context_window": {"used_percentage": 5},
        "workspace": {"current_dir": "/home/ywatanabe/proj/scitex-scholar"},
    }
    # Act
    line = _render(data)
    # Assert
    assert "scitex-scholar" in line


def test_under_pressure_the_workdir_goes_before_the_numbers(env_save_restore):
    """The sacrifice order, asserted from the losing side.

    A full payload plus a differing workdir exceeds the budget. What must
    survive is the data — the exact thing the terminal was discarding before.
    """
    # Arrange
    env_save_restore.set("SAC_AGENT", "some-agent")
    data = dict(_LIVE)
    data["workspace"] = {"current_dir": "/home/ywatanabe/proj/scitex-scholar"}
    # Act
    line = _render(data)
    # Assert
    assert "7d:21%" in line


# ---------------------------------------------------------------------------
# The shorteners — each must stay unambiguous within the fleet.
# ---------------------------------------------------------------------------


def test_the_shared_scitex_host_prefix_is_dropped():
    """Every host carries it, so it distinguishes nothing and costs 7 columns."""
    # Arrange
    host = "scitex-compute-04"
    # Act
    short = _short_host(host)
    # Assert
    assert short == "compute-04"


def test_a_host_without_the_shared_prefix_is_untouched():
    """ywata-note-win and mba must not be mangled by a prefix rule."""
    # Arrange
    host = "ywata-note-win"
    # Act
    short = _short_host(host)
    # Assert
    assert short == "ywata-note-win"


def test_the_model_parenthetical_is_compacted_but_the_size_is_kept():
    """`(1M context)` is prose; `1M` is the part that could surprise someone."""
    # Arrange
    model = "Opus 5 (1M context)"
    # Act
    short = _short_model(model)
    # Assert
    assert short == "Opus 5 1M"


def test_a_model_name_without_a_parenthetical_passes_through_verbatim():
    """Locks the existing contract — `claude-opus-4-7` must not be rewritten."""
    # Arrange
    model = "claude-opus-4-7"
    # Act
    short = _short_model(model)
    # Assert
    assert short == "claude-opus-4-7"


def test_the_account_is_shortened_to_the_fleets_own_key():
    """Not an invention: the quota cache already indexes accounts by this segment.

    It calls it `short`, and the four stored accounts are distinct in it
    (scitex, ywatanabe, wyusuuke, ywata1989), so the pane and the quota cache
    name an account the same way.
    """
    # Arrange
    account = "wyusuuke-gmail-com"
    # Act
    short = _short_account(account)
    # Assert
    assert short == "wyusuuke"


# ---------------------------------------------------------------------------
# Never raise — the pane must survive a payload we did not anticipate.
# ---------------------------------------------------------------------------


def test_display_stays_silent_on_garbage_rather_than_raising(capsys):
    # Arrange
    payload = b"{not json"
    # Act
    sl_mod._display(payload)
    # Assert
    assert capsys.readouterr().out == ""


def test_display_prints_the_rendered_line_for_a_real_payload(named_agent, capsys):
    # Arrange
    payload = json.dumps(_LIVE).encode()
    # Act
    sl_mod._display(payload)
    # Assert
    assert "ctx:47%" in capsys.readouterr().out
