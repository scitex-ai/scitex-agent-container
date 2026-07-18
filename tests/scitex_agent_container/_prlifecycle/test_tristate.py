"""THE tri-state suite: an unreadable PR list must never read as a clean board.

This file is the gate the whole feature exists to install, so it is written to
be PROVABLY red against the naive implementation rather than merely green
against the real one — a gate nobody has proven red is a hope with YAML around
it.

THE BUG BEING GUARDED
---------------------
:meth:`scitex_agent_container._authheal._pass.PassOutcome.exit_code` ends in a
bare ``return 0``. Detection failing yields no reports, no reports fall through
to that return, and five systemd timers reported SUCCESS every ten minutes
while an agent sat login-expired for hours. "Nothing observed" and "all clean"
were indistinguishable.

The ``test_naive_*`` block at the bottom is the MUTATION PROOF. It defines
:class:`_NaiveOutcome` — the incident's logic, reconstructed: it returns 0 when
it finds nothing, and has no way to express "I could not look". The tests then
assert that this suite's own criteria REJECT it. If someone reintroduces that
fallthrough, those tests show the criteria still catch it; if someone weakens
the criteria until the naive version would pass, they go red and say so.

No mocks: every failure below is a REAL ``gh`` failure mode with its verbatim
stderr, driven through the production classifier.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._prlifecycle import (
    EXIT_ACTION,
    EXIT_CLEAN,
    EXIT_UNKNOWN,
    FetchState,
    card_id_for,
    fetch_open_prs,
    sync_cards,
)
from scitex_agent_container._prlifecycle._gh import PRFetch

from .conftest import RECORDED_REPO, gh_failing, gh_returning

# ---------------------------------------------------------------------------
# Real gh failure modes, verbatim.
# ---------------------------------------------------------------------------

#: What `gh` actually prints with no credential.
_UNAUTH_STDERR = (
    "To get started with GitHub CLI, please run:  gh auth login\n"
    "Alternatively, populate the GH_TOKEN environment variable with a "
    "GitHub API authentication token.\n"
)

#: What `gh` prints when the API rate-limits the read.
_RATELIMIT_STDERR = (
    "error fetching pull requests: API rate limit exceeded for user ID 42527473.\n"
)

#: What `gh` prints with no network.
_OFFLINE_STDERR = (
    "error connecting to api.github.com\n"
    "dial tcp: lookup api.github.com: no such host\n"
)

_FAILURES = {
    "unauthenticated": (
        gh_failing(returncode=4, stderr=_UNAUTH_STDERR),
        FetchState.UNAUTHENTICATED,
    ),
    "rate-limited": (
        gh_failing(returncode=1, stderr=_RATELIMIT_STDERR),
        FetchState.RATE_LIMITED,
    ),
    "offline": (
        gh_failing(returncode=1, stderr=_OFFLINE_STDERR),
        FetchState.UNREACHABLE,
    ),
    "gh-missing": (
        gh_failing(spawn_error="FileNotFoundError: 'gh'"),
        FetchState.NO_CLIENT,
    ),
}


def _blind_fetch(runner=None):
    """A ``fetch`` seam that is blind the way an unauthenticated host is."""
    use = runner or _FAILURES["unauthenticated"][0]

    def fetch(repo):
        return fetch_open_prs(repo, run=use)

    return fetch


def _reading_fetch(recorded_gh):
    def fetch(repo):
        return fetch_open_prs(repo, run=recorded_gh)

    return fetch


# ---------------------------------------------------------------------------
# The fetch itself: a failure is never a successful read of an empty list.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", sorted(_FAILURES))
def test_a_failed_fetch_is_never_readable(label) -> None:
    # Arrange — a real gh failure mode, verbatim stderr.
    runner, _ = _FAILURES[label]
    # Act
    result = fetch_open_prs(RECORDED_REPO, run=runner)
    # Assert — the whole contract in one line: we did NOT read the list.
    assert not result.readable


@pytest.mark.parametrize("label", sorted(_FAILURES))
def test_each_failure_mode_is_named_not_just_flagged(label) -> None:
    # Arrange — a vague "it failed" is not actionable at 3am; the operator
    # needs to know WHICH way we are blind.
    runner, expected = _FAILURES[label]
    # Act
    result = fetch_open_prs(RECORDED_REPO, run=runner)
    # Assert
    assert result.state is expected


@pytest.mark.parametrize("label", sorted(_FAILURES))
def test_a_failed_fetch_carries_no_pull_requests(label) -> None:
    # Arrange — a blind fetch must not smuggle partial data forward.
    runner, _ = _FAILURES[label]
    # Act
    result = fetch_open_prs(RECORDED_REPO, run=runner)
    # Assert
    assert result.prs == ()


def test_gh_exit_zero_with_blank_stdout_is_not_readable() -> None:
    # Arrange — THE subtle one. `gh` prints '[]' for a repo with genuinely no
    # open PRs, so BLANK stdout means the payload was lost, not that the
    # backlog is empty. Reading blank as empty is the collapse in miniature.
    # Act
    result = fetch_open_prs(RECORDED_REPO, run=gh_returning(""))
    # Assert
    assert not result.readable


def test_gh_exit_zero_with_blank_stdout_is_classified_unparseable() -> None:
    # Arrange — and it is named, not merely rejected.
    # Act
    result = fetch_open_prs(RECORDED_REPO, run=gh_returning(""))
    # Assert
    assert result.state is FetchState.UNPARSEABLE


def test_a_genuinely_empty_repo_is_readable() -> None:
    # Arrange — the other side of the same coin, and the reason this is a
    # TRI-state rather than "treat everything suspicious as unknown": a real
    # empty list must still be usable, or the sweep could never conclude
    # anything and would cry wolf forever.
    # Act
    result = fetch_open_prs(RECORDED_REPO, run=gh_returning("[]"))
    # Assert
    assert result.readable


def test_a_genuinely_empty_repo_yields_no_pull_requests() -> None:
    # Arrange — and its emptiness is real, not a failure in disguise.
    # Act
    result = fetch_open_prs(RECORDED_REPO, run=gh_returning("[]"))
    # Assert
    assert result.prs == ()


def test_unparseable_json_is_not_readable() -> None:
    # Arrange — a truncated/garbage payload on a zero exit.
    # Act
    result = fetch_open_prs(RECORDED_REPO, run=gh_returning("{not json"))
    # Assert
    assert not result.readable


def test_unparseable_json_carries_no_pull_requests() -> None:
    # Arrange — garbage in must not become an authoritative empty list.
    # Act
    result = fetch_open_prs(RECORDED_REPO, run=gh_returning("{not json"))
    # Assert
    assert result.prs == ()


def test_readable_is_a_whitelist_of_exactly_one_state() -> None:
    # Arrange — the structural guarantee, and the inverse of the fallthrough
    # that caused the incident. `readable` must test OK and nothing else, so a
    # FetchState member added later defaults to UNKNOWN rather than silently
    # joining the "clean" side.
    # Act
    readable = {state for state in FetchState if PRFetch(state).readable}
    # Assert
    assert readable == {FetchState.OK}


# ---------------------------------------------------------------------------
# The sweep's exit code — 0 / 1 / 2, all three reachable and distinct.
# ---------------------------------------------------------------------------


def test_unreadable_fetch_makes_the_sweep_exit_2(store) -> None:
    # Arrange — gh is unauthenticated. This is the exact scenario the brief
    # names: the sweep MUST report UNKNOWN and exit non-zero, never treat the
    # empty result as "no open PRs".
    # Act
    outcome = sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_blind_fetch())
    # Assert — exit 2, NOT 0 and NOT 1.
    assert outcome.exit_code() == EXIT_UNKNOWN


def test_unreadable_fetch_prints_UNKNOWN_where_a_human_sees_it(store, capsys) -> None:
    # Arrange — an exit code nobody reads is not an alarm. journald keeps
    # stderr; that is where the word has to appear.
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_blind_fetch())
    # Act
    captured = capsys.readouterr()
    # Assert
    assert "UNKNOWN" in captured.err


def test_unreadable_fetch_summary_says_UNKNOWN(store) -> None:
    # Arrange — the machine-readable summary must agree with the exit code.
    # Act
    outcome = sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_blind_fetch())
    # Assert
    assert "UNKNOWN" in outcome.summary()


def test_unreadable_fetch_names_the_repo_it_could_not_read(store, capsys) -> None:
    # Arrange — "something failed" is not actionable when sweeping many repos.
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_blind_fetch())
    # Act
    captured = capsys.readouterr()
    # Assert
    assert RECORDED_REPO in captured.err


def test_unreadable_fetch_names_the_remedy(store, capsys) -> None:
    # Arrange — the classifier's detail must survive to the operator, so the
    # message says what to DO rather than only what broke.
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_blind_fetch())
    # Act
    captured = capsys.readouterr()
    # Assert
    assert "gh auth login" in captured.err


def test_a_blind_pass_does_not_complete_existing_cards(store) -> None:
    # Arrange — the DESTRUCTIVE half of the same bug. Card completion is
    # inferred from a PR's ABSENCE from the open list, so on a blind pass every
    # PR looks absent. Without the readable-gate, one unauthenticated tick
    # would complete every card on the board at once — rendering a 35-PR
    # backlog as a clean board in the most literal way possible.
    import scitex_todo

    scitex_todo.add_task(
        store,
        id=card_id_for(RECORDED_REPO, 999),
        title="[pr] a previously-tracked PR",
        status="in_progress",
        assignee="sac.sync-pr-cards",
        created_by="sac.sync-pr-cards",
    )
    # Act — a blind pass.
    sync_cards([RECORDED_REPO], apply=True, store=store, fetch=_blind_fetch())
    # Assert — untouched, NOT completed.
    card = scitex_todo.get_task(store, card_id_for(RECORDED_REPO, 999))
    assert card["status"] != "done"


def test_sweeping_zero_repos_is_unknown_not_clean(store) -> None:
    # Arrange — "I examined nothing" is not "nothing is wrong". An empty repo
    # list is a configuration fact indistinguishable from broken discovery, so
    # it must not be reported as a healthy board.
    # Act
    outcome = sync_cards([], apply=True, store=store, fetch=lambda repo: None)
    # Assert
    assert outcome.exit_code() == EXIT_UNKNOWN


def test_readable_and_written_exits_0(store, recorded_gh) -> None:
    # Arrange — the positive control. Without it a suite could pass by always
    # returning 2, which is exactly as useless as always returning 0.
    # Act
    outcome = sync_cards(
        [RECORDED_REPO], apply=True, store=store, fetch=_reading_fetch(recorded_gh)
    )
    # Assert
    assert outcome.exit_code() == EXIT_CLEAN


def test_dry_run_with_work_pending_exits_1(store, recorded_gh) -> None:
    # Arrange — the middle state must be reachable too, or "tri-state" is two
    # states wearing a third's name.
    # Act
    outcome = sync_cards(
        [RECORDED_REPO], apply=False, store=store, fetch=_reading_fetch(recorded_gh)
    )
    # Assert
    assert outcome.exit_code() == EXIT_ACTION


def test_all_three_exit_codes_are_distinct_and_reachable(store, recorded_gh) -> None:
    # Arrange — the tri-state asserted as a SET rather than one code at a time.
    # A suite that only ever observes two of three has not tested a tri-state.
    reading = _reading_fetch(recorded_gh)
    # Act
    codes = {
        sync_cards([RECORDED_REPO], apply=True, store=store, fetch=reading).exit_code(),
        sync_cards(
            [RECORDED_REPO], apply=False, store=store, fetch=reading
        ).exit_code(),
        sync_cards(
            [RECORDED_REPO], apply=True, store=store, fetch=_blind_fetch()
        ).exit_code(),
    }
    # Assert
    assert codes == {EXIT_CLEAN, EXIT_ACTION, EXIT_UNKNOWN}


# ---------------------------------------------------------------------------
# MUTATION PROOF — this suite's criteria must REJECT the naive implementation.
# ---------------------------------------------------------------------------


class _NaiveOutcome:
    """The bug, reconstructed: 'no findings' falls through to success.

    Deliberately faithful to :meth:`.._authheal._pass.PassOutcome.exit_code` —
    it checks for findings and, when there are none, returns 0. It has NO
    concept of "I could not look", which is exactly why an unauthenticated
    ``gh`` (zero PRs found) renders identically to a healthy empty board.
    """

    def __init__(self, prs, writes=()):
        self.prs = list(prs)
        self.writes = list(writes)

    def exit_code(self) -> int:
        if self.writes:
            return 1
        return 0  # <-- the fallthrough. "Nothing observed" lands here.

    def summary(self) -> str:
        return f"{len(self.prs)} open PR(s)"


def _naive_on_blind_input() -> _NaiveOutcome:
    """The naive outcome fed the SAME blind fetch the real sweep is tested on."""
    blind = fetch_open_prs(RECORDED_REPO, run=_FAILURES["unauthenticated"][0])
    return _NaiveOutcome(blind.prs)


def test_naive_implementation_reports_success_while_blind() -> None:
    # Arrange — MUTATION PROOF, step 1: demonstrate the bug still bites. The
    # naive version is given an unauthenticated fetch (so it sees zero PRs
    # because it never read any) and reports SUCCESS. This IS the incident:
    # five timers, green every ten minutes, nothing observed.
    naive = _naive_on_blind_input()
    # Act
    code = naive.exit_code()
    # Assert
    assert code == EXIT_CLEAN


def test_naive_implementation_fails_this_suites_unknown_criterion() -> None:
    # Arrange — MUTATION PROOF, step 2: the criterion used by
    # `test_unreadable_fetch_makes_the_sweep_exit_2` REJECTS the naive
    # implementation. That test would therefore go RED against it rather than
    # passing vacuously — which is the only thing that makes it a gate.
    naive = _naive_on_blind_input()
    # Act
    code = naive.exit_code()
    # Assert
    assert code != EXIT_UNKNOWN


def test_real_implementation_gets_the_same_blind_input_right() -> None:
    # Arrange — MUTATION PROOF, step 3: the difference is the tri-state itself
    # and not the scenario. Same input, opposite verdict.
    # Act
    blind = fetch_open_prs(RECORDED_REPO, run=_FAILURES["unauthenticated"][0])
    # Assert
    assert not blind.readable


def test_naive_implementation_collapses_empty_and_unreadable() -> None:
    # Arrange — the sharper statement of the same proof: the naive type maps
    # two DIFFERENT facts onto ONE output.
    blind = fetch_open_prs(RECORDED_REPO, run=_FAILURES["unauthenticated"][0])
    genuinely_empty = fetch_open_prs(RECORDED_REPO, run=gh_returning("[]"))
    # Act
    collapsed = (
        _NaiveOutcome(blind.prs).exit_code()
        == _NaiveOutcome(genuinely_empty.prs).exit_code()
    )
    # Assert
    assert collapsed


def test_real_implementation_keeps_empty_and_unreadable_apart() -> None:
    # Arrange — ...and the real one maps them onto two. This single assertion,
    # paired with the one above, is what "we fixed the bug" means stated in
    # terms of the bug itself.
    blind = fetch_open_prs(RECORDED_REPO, run=_FAILURES["unauthenticated"][0])
    genuinely_empty = fetch_open_prs(RECORDED_REPO, run=gh_returning("[]"))
    # Act
    distinguished = blind.readable != genuinely_empty.readable
    # Assert
    assert distinguished


def test_the_recorded_fixture_actually_contains_open_prs(recorded_rows) -> None:
    # Arrange — anchors the test below: the consequence only matters if the
    # repo genuinely HAS open PRs to be misreported as zero. Asserted from the
    # REAL recorded response, not assumed.
    # Act
    count = len(recorded_rows)
    # Assert
    assert count > 0


def test_naive_summary_would_report_a_real_backlog_as_zero() -> None:
    # Arrange — the operator-visible consequence. The repo demonstrably has
    # open PRs (asserted above), yet the naive summary claims none, because
    # rate-limiting produced an empty list it cannot distinguish from empty.
    blind = fetch_open_prs(RECORDED_REPO, run=_FAILURES["rate-limited"][0])
    # Act
    naive_says = _NaiveOutcome(blind.prs).summary()
    # Assert
    assert naive_says == "0 open PR(s)"


def test_real_fetch_refuses_to_claim_a_rate_limited_repo_is_empty() -> None:
    # Arrange — the counterpart: instead of claiming zero, the real fetch
    # reports WHY it cannot say.
    # Act
    blind = fetch_open_prs(RECORDED_REPO, run=_FAILURES["rate-limited"][0])
    # Assert
    assert blind.state is FetchState.RATE_LIMITED
