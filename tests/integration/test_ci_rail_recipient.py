"""The CI rail must not record a guess as a measurement (ADR-0024).

THE DEFECT THESE TESTS PIN. ``resolve_recipient`` has always had two very
different sources for "who should hear this verdict": a MEASUREMENT taken by
``pre-push`` from the pushing process's own environment, and an INFERENCE
drawn from the repository name when no such measurement exists. It has always
returned WHICH of the two it used. ``record_verdict`` then threw that away and
wrote ``agent=recipient, assignee=recipient`` either way -- into the fields
that mean "this is who did it". Once written, an inference was
indistinguishable from an observation, and self-reinforcing besides: the next
verdict for the same sha read the guess back out of the card and reported it
as the recorded pusher.

MEASURED ON THE LIVE STORE 2026-08-15: of 121 rail-shaped cards
(``ci-<repo>-<12 hex>``), 118 -- 97.5% -- were created by the verdict half
(``created_by = "ci"``, so no pusher was ever recorded) and every one of those
carried an inferred ``agent``. 117 named this repo's owning agent.

WHY THE RESOLVER IS NOT THE SUBJECT. The resolver was never the liar; six
tests in ``test_ci_card_rail.py`` already cover it, and every one of them
passes on the broken code. The defect is in what the rail DOES with the
provenance, which is why these tests target :func:`attribution` -- the
decision extracted from ``record_verdict`` -- and then assert that
``record_verdict`` is actually wired to it.

The happy path (a pusher the hook really recorded) is deliberately kept as a
CONTROL, so that "attribute nothing, ever" cannot pass for a fix.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_DIR = REPO_ROOT / ".github" / "ci"
REPO = "scitex-ai/sac"
OWNER = "repo-owning-agent"
PUSHER = "the-actual-pusher"


def _load(module_name: str):
    """Import a rail module by path, the same way the runner does.

    The rail is deliberately NOT an installed package: it runs under
    ``uv run --with scitex-cards`` on a runner with no sac install, and from
    a git hook inside an agent container.
    """
    sys.path.insert(0, str(CI_DIR))
    spec = importlib.util.spec_from_file_location(
        module_name, CI_DIR / f"{module_name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rail():
    return _load("ci_card_rail")


@pytest.fixture(scope="module")
def rail_cards():
    return _load("ci_rail_cards")


@pytest.fixture(scope="module")
def verdict_half() -> str:
    """The source of ``record_verdict`` — for the WIRING assertions.

    The behavioural tests below prove :func:`attribution` decides correctly.
    These prove ``record_verdict`` asks it, rather than keeping a second copy
    of the old rule. Same idiom the neighbouring suite already uses for
    ``blocker=BLOCKER_CLEARED`` and ``create_only=``: an invariant about a
    call this suite cannot make without a postgres, a bus and a runner.
    """
    source = (CI_DIR / "ci_card_rail.py").read_text(encoding="utf-8")
    return source.split("def record_verdict", 1)[1]


@pytest.fixture(scope="module")
def card_write(verdict_half) -> str:
    """Just the ``upsert_card`` call — the CARD WRITE, and nothing else.

    Scoping matters here and the first draft of this test got it wrong. A
    naive search of the whole function for ``agent=recipient`` also matches
    ``notify_agent(token, agent=recipient, ...)``, which is CORRECT and must
    stay: the recipient is exactly who we notify. The defect is writing that
    same name into the CARD. Testing the two together produced a test that
    could never pass, which is its own kind of dishonest assertion.
    """
    return verdict_half.split("upsert_card(", 1)[1].split("pkg.comment_task", 1)[0]


def _agent(name: str, *, project: str = "sac", reachable: bool = True) -> dict:
    return {
        "name": name,
        "project": project,
        "inbox_reachable": "reachable" if reachable else "unreachable",
        "inbox_subscribers": 1 if reachable else 0,
        "started_at": "2026-08-14",
    }


# ---------------------------------------------------------------------------
# THE DEFECT: an unknown pusher must not be recorded as a known one
# ---------------------------------------------------------------------------
def test_an_unknown_pusher_is_not_recorded_as_the_repo_owner(rail) -> None:
    """THE regression test — fails on the pre-fix code.

    No card exists, so no pusher was ever recorded. The rail still has to
    route the verdict somewhere and the only candidate is the agent whose
    spec claims this repo. Routing there is defensible; RECORDING it states
    a fact nobody measured.
    """
    # Arrange
    how = rail.HOW_FALLBACK
    # Act
    owner, _routing = rail.attribution(recipient=OWNER, how=how, repo=REPO)
    # Assert
    assert owner != OWNER


def test_an_unknown_pusher_is_recorded_as_unclaimed(rail, rail_cards) -> None:
    """The third value, written down.

    Not blank — ``add_task`` refuses an owner-less card ("assignee is
    required ... an owner-less card is rejected") — but a name that is
    visibly not an agent, so the card reads as unattributed rather than as
    attributed to whoever happens to own the repo.
    """
    # Arrange
    how = rail.HOW_FALLBACK
    # Act
    owner, _routing = rail.attribution(recipient=OWNER, how=how, repo=REPO)
    # Assert
    assert owner == rail_cards.UNCLAIMED_OWNER


def test_an_unknown_pusher_produces_a_routing_disclaimer(rail) -> None:
    """Only the message reaches a person; the card cannot say this."""
    # Arrange
    how = rail.HOW_FALLBACK
    # Act
    _owner, routing = rail.attribution(recipient=OWNER, how=how, repo=REPO)
    # Assert
    assert "UNKNOWN" in routing


def test_the_disclaimer_says_why_it_arrived(rail) -> None:
    """"Because your spec claims this repo" — not "because you pushed"."""
    # Arrange
    how = rail.HOW_FALLBACK
    # Act
    _owner, routing = rail.attribution(recipient=OWNER, how=how, repo=REPO)
    # Assert
    assert "sac" in routing and "not because you pushed" in routing.lower()


def test_an_unresolved_provenance_is_also_never_attributed(rail, rail_cards) -> None:
    """Anything that is not a RECORD is a guess. Only one value is a record."""
    # Arrange
    how = rail.HOW_NONE
    # Act
    owner, _routing = rail.attribution(recipient=OWNER, how=how, repo=REPO)
    # Assert
    assert owner == rail_cards.UNCLAIMED_OWNER


# ---------------------------------------------------------------------------
# THE CONTROL: a pusher that WAS measured must still be attributed
# ---------------------------------------------------------------------------
def test_a_recorded_pusher_is_still_recorded_as_the_owner(rail) -> None:
    """Never attributing anything is not a fix, it is the opposite failure."""
    # Arrange
    how = rail.HOW_RECORDED
    # Act
    owner, _routing = rail.attribution(recipient=PUSHER, how=how, repo=REPO)
    # Assert
    assert owner == PUSHER


def test_a_recorded_pusher_gets_no_routing_disclaimer(rail) -> None:
    """The disclaimer must be absent exactly when "you pushed" is the truth."""
    # Arrange
    how = rail.HOW_RECORDED
    # Act
    _owner, routing = rail.attribution(recipient=PUSHER, how=how, repo=REPO)
    # Assert
    assert routing == ""


# ---------------------------------------------------------------------------
# THE LOOP: the rail must not read its own guess back as a fact
# ---------------------------------------------------------------------------
def test_the_unclaimed_sentinel_is_not_read_back_as_a_recorded_pusher(
    rail, rail_cards
) -> None:
    """The laundering, closed.

    The same sha is judged twice whenever a branch has an open PR — the
    ``push`` and ``pull_request`` events each fire the gate — so the second
    run reads exactly what the first one wrote. If the sentinel counted as a
    recorded pusher, one run's guess would become the next run's evidence,
    and the verdict would be addressed to a name that owns no inbox at all.
    """
    # Arrange
    card = {"agent": rail_cards.UNCLAIMED_OWNER}
    # Act
    who, how = rail.resolve_recipient(card=card, repo=REPO, agents=[_agent(OWNER)])
    # Assert
    assert (who, how) == (OWNER, rail.HOW_FALLBACK)


def test_the_ci_actor_is_not_read_back_as_a_recorded_pusher(rail, rail_cards) -> None:
    """``ci`` is the rail naming ITSELF as the filer, never a pusher."""
    # Arrange
    card = {"agent": rail_cards.VERDICT_ACTOR}
    # Act
    _who, how = rail.resolve_recipient(card=card, repo=REPO, agents=[_agent(OWNER)])
    # Assert
    assert how == rail.HOW_FALLBACK


def test_a_real_pusher_on_the_card_is_still_a_record(rail) -> None:
    """The sentinel check must reject sentinels, not identities."""
    # Arrange
    card = {"agent": PUSHER}
    # Act
    who, how = rail.resolve_recipient(card=card, repo=REPO, agents=[_agent(OWNER)])
    # Assert
    assert (who, how) == (PUSHER, rail.HOW_RECORDED)


# ---------------------------------------------------------------------------
# WIRING: record_verdict must ASK, not keep a second copy of the old rule
# ---------------------------------------------------------------------------
def test_the_verdict_half_asks_for_the_attribution(verdict_half) -> None:
    # Arrange
    source = verdict_half
    # Act
    asks = "attribution(recipient=recipient, how=how, repo=repo)" in source
    # Assert
    assert asks


def test_the_verdict_half_records_the_owner_not_the_recipient(card_write) -> None:
    """THE line that was wrong: ``agent=recipient, assignee=recipient``.

    Writing the RECIPIENT into the CARD is the defect in one expression — it
    is what turned a routing choice into a recorded fact on 118 cards.
    """
    # Arrange
    source = card_write
    # Act
    launders = "agent=recipient" in source or "assignee=recipient" in source
    # Assert
    assert not launders


def test_the_verdict_half_writes_the_resolved_owner(card_write) -> None:
    # Arrange
    source = card_write
    # Act
    writes_owner = "agent=owner" in source and "assignee=owner" in source
    # Assert
    assert writes_owner


def test_the_fallback_recipient_is_subscribed_rather_than_assigned(
    verdict_half,
) -> None:
    """"Tell me, but this is not my job" — the right weight for a guess.

    Without this the honest card would also be a SILENT one: nobody owns it,
    so nobody is told. Subscribing keeps delivery while refusing the false
    claim of authorship.
    """
    # Arrange
    source = verdict_half
    # Act
    subscribes = "set_subscriber(task_id=card_id, who=recipient" in source
    # Assert
    assert subscribes


def test_the_verdict_is_still_delivered_when_the_pusher_is_unknown(
    verdict_half,
) -> None:
    """Honesty must not cost delivery.

    An unrouted verdict beats a misrouted one, but a DROPPED verdict beats
    neither — and 97.5% of pushes take this path, so refusing to deliver
    here would silence the rail rather than correct it.
    """
    # Arrange
    source = verdict_half
    # Act
    still_notifies = "notify_agent(token, agent=recipient" in source
    # Assert
    assert still_notifies


def test_the_run_log_names_the_owner_and_the_recipient(verdict_half) -> None:
    """A log printing only one of the two is how this went unnoticed."""
    # Arrange
    source = verdict_half
    # Act
    prints_both = "card owner={owner}" in source and "recipient {recipient}" in source
    # Assert
    assert prints_both


# ---------------------------------------------------------------------------
# the three states are distinguishable, which is the whole point
# ---------------------------------------------------------------------------
def test_the_three_provenance_values_are_distinct(rail) -> None:
    """Collapsing them into one field IS the bug being fixed."""
    # Arrange
    values = (rail.HOW_RECORDED, rail.HOW_FALLBACK, rail.HOW_NONE)
    # Act
    distinct = set(values)
    # Assert
    assert len(distinct) == 3


def test_only_the_recorded_provenance_yields_an_attribution(rail, rail_cards) -> None:
    """Exactly ONE of the three may be written down as authorship."""
    # Arrange
    everything = (rail.HOW_RECORDED, rail.HOW_FALLBACK, rail.HOW_NONE)
    # Act
    attributed = [
        how
        for how in everything
        if rail.attribution(recipient=PUSHER, how=how, repo=REPO)[0] == PUSHER
    ]
    # Assert
    assert attributed == [rail.HOW_RECORDED]


def test_the_unclaimed_owner_is_not_a_name_an_agent_could_have(rail_cards) -> None:
    """It must be unmistakable, not merely unlikely."""
    # Arrange
    sentinel = rail_cards.UNCLAIMED_OWNER
    # Act
    looks_like_a_marker = sentinel.startswith("ci-") and "unclaimed" in sentinel
    # Assert
    assert looks_like_a_marker


def test_the_unclaimed_owner_is_not_the_filer(rail_cards) -> None:
    """Who FILED it (``ci``) and who OWNS it are different questions."""
    # Arrange
    sentinel, filer = rail_cards.UNCLAIMED_OWNER, rail_cards.VERDICT_ACTOR
    # Act
    distinct = sentinel != filer
    # Assert
    assert distinct


# EOF
