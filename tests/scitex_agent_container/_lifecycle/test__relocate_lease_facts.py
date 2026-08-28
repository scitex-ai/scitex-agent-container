"""An unreadable store must not look like an empty one, and an idle host must not be probed.

The adapter behind the lease check. Two things it must never do, both of which
would be easy and quiet:

    * collapse "I could not open the db" into ``lease=None``. That is a REAL
      answer meaning "this store has never held a row", and it BOOTSTRAPS a
      lease and proceeds — so folding a broken store into it turns a fault into
      a green light at the one gate standing between one live agent and two.
    * probe a third host when the answer does not depend on it. A row naming the
      source, or an expired row, decides without anyone observed; dialling a
      machine anyway lets an ssh timeout refuse a relocation that was fine.

The seams take and return REAL values — a real ``Lease``, a real three-valued
liveness pair — so nothing about the decision is mocked.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_lease import Lease
from scitex_agent_container._lifecycle._relocate_lease_facts import gather_lease_facts

AGENT = "canary-resume-test"
A = "scitex-compute-04"
B = "ywata-note-win"
NOW = 1_786_500_000.0
TTL = 86_400.0


def _row(holder: str, *, expires_at: float = NOW + TTL) -> Lease:
    return Lease(
        agent=AGENT, holder=holder, token="tok", expires_at=expires_at, fence=1
    )


class _Watcher:
    """A real callable that records who it was asked about. No mocking library."""

    def __init__(self, answer: tuple[bool | None, str]) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def __call__(self, holder: str, agent: str) -> tuple[bool | None, str]:
        self.asked.append(holder)
        return self.answer


@pytest.fixture
def absent_watcher() -> _Watcher:
    """Answers the way tmux does when the server is up and holds no session."""
    return _Watcher((False, "tmux on scitex-compute-04 answered and has NO session"))


def test_an_unreadable_store_is_not_read(absent_watcher: _Watcher) -> None:
    # Arrange: a loader that fails the way a locked or missing db does.
    def explode(agent: str):
        raise OSError("unable to open database file")

    # Act
    facts = gather_lease_facts(
        AGENT, from_host=B, now=NOW, load=explode, observe=absent_watcher
    )
    # Assert
    assert facts.read is False


def test_an_unreadable_store_does_not_report_an_absent_row() -> None:
    # Arrange: THE failure this guards. ``lease=None`` bootstraps and proceeds.
    def explode(agent: str):
        raise OSError("unable to open database file")

    # Act
    facts = gather_lease_facts(AGENT, from_host=B, now=NOW, load=explode)
    # Assert
    assert facts.lease is None and facts.read is False


def test_an_unreadable_store_says_why() -> None:
    # Arrange
    def explode(agent: str):
        raise OSError("unable to open database file")

    # Act
    facts = gather_lease_facts(AGENT, from_host=B, now=NOW, load=explode)
    # Assert
    assert "unable to open database file" in facts.recorded_holder_evidence


def test_the_default_reader_reaches_the_real_store_and_can_fail_loudly() -> None:
    # Arrange — NO injected loader, so this drives the path the preflight
    # actually uses: the default reader, which since 2026-08-28 opens the
    # PostgreSQL lease store. The autouse isolation points SCITEX_STORE_DSN at
    # a port nothing listens on, so the read fails — and what must NOT happen
    # is that the failure becomes ``lease=None``, which bootstraps a lease and
    # proceeds at the one gate standing between one live agent and two.
    # Act
    facts = gather_lease_facts(AGENT, from_host=B, now=NOW)
    # Assert
    assert facts.read is False


def test_a_store_with_no_row_is_read_and_empty() -> None:
    # Arrange
    # Act
    facts = gather_lease_facts(AGENT, from_host=B, now=NOW, load=lambda a: None)
    # Assert
    assert facts.read is True


def test_no_row_asks_no_host_anything(absent_watcher: _Watcher) -> None:
    # Arrange
    # Act
    gather_lease_facts(
        AGENT, from_host=B, now=NOW, load=lambda a: None, observe=absent_watcher
    )
    # Assert
    assert absent_watcher.asked == []


def test_a_row_naming_the_source_asks_no_host_anything(
    absent_watcher: _Watcher,
) -> None:
    # Arrange: the ordinary second move — nobody else is involved.
    # Act
    gather_lease_facts(
        AGENT, from_host=B, now=NOW, load=lambda a: _row(B), observe=absent_watcher
    )
    # Assert
    assert absent_watcher.asked == []


def test_an_expired_row_asks_no_host_anything(absent_watcher: _Watcher) -> None:
    # Arrange: the fence already settles this; a probe would only add a way to fail.
    # Act
    gather_lease_facts(
        AGENT,
        from_host=B,
        now=NOW,
        load=lambda a: _row(A, expires_at=NOW - 1.0),
        observe=absent_watcher,
    )
    # Assert
    assert absent_watcher.asked == []


def test_a_live_row_naming_another_host_asks_THAT_host(
    absent_watcher: _Watcher,
) -> None:
    # Arrange: the canary's return-leg input.
    # Act
    gather_lease_facts(
        AGENT, from_host=B, now=NOW, load=lambda a: _row(A), observe=absent_watcher
    )
    # Assert
    assert absent_watcher.asked == [A]


def test_the_observation_travels_onto_the_facts(absent_watcher: _Watcher) -> None:
    # Arrange
    # Act
    facts = gather_lease_facts(
        AGENT, from_host=B, now=NOW, load=lambda a: _row(A), observe=absent_watcher
    )
    # Assert
    assert facts.recorded_holder_running is False


def test_the_evidence_travels_with_it(absent_watcher: _Watcher) -> None:
    # Arrange: a verdict built on an observation must be able to show it.
    # Act
    facts = gather_lease_facts(
        AGENT, from_host=B, now=NOW, load=lambda a: _row(A), observe=absent_watcher
    )
    # Assert
    assert "NO session" in facts.recorded_holder_evidence


def test_a_failed_probe_leaves_liveness_unobserved() -> None:
    # Arrange: folding this into "not running" would hand the lease away from a
    # host that may well be live.
    def unreachable(holder: str, agent: str):
        raise TimeoutError("ssh timed out")

    # Act
    facts = gather_lease_facts(
        AGENT, from_host=B, now=NOW, load=lambda a: _row(A), observe=unreachable
    )
    # Assert
    assert facts.recorded_holder_running is None


def test_a_failed_probe_still_reports_the_row_it_read() -> None:
    # Arrange: the row is a good measurement even when the probe was not.
    def unreachable(holder: str, agent: str):
        raise TimeoutError("ssh timed out")

    # Act
    facts = gather_lease_facts(
        AGENT, from_host=B, now=NOW, load=lambda a: _row(A), observe=unreachable
    )
    # Assert
    assert facts.read is True


def test_the_clock_is_recorded_so_expiry_can_be_judged() -> None:
    # Arrange
    # Act
    facts = gather_lease_facts(AGENT, from_host=B, now=NOW, load=lambda a: _row(B))
    # Assert
    assert facts.now == NOW


def test_an_unnamed_source_asks_no_host_anything(absent_watcher: _Watcher) -> None:
    # Arrange: the check refuses on an unnamed source alone, so an ssh spent
    # here would buy nothing and could only add a way to fail.
    # Act
    gather_lease_facts(
        AGENT, from_host="", now=NOW, load=lambda a: _row(A), observe=absent_watcher
    )
    # Assert
    assert absent_watcher.asked == []


def test_the_store_that_was_read_is_named() -> None:
    # Arrange: one store per host, no sync — which one answered is part of the
    # answer. Since 2026-08-28 that is the per-host PostgreSQL locator, not a
    # state.db path, and it is resolved WITHOUT connecting, so it is named even
    # when the store turns out to be unreachable — the case a reader most needs
    # named. The locator names the ENDPOINT rather than the table, so what this
    # pins is that a resolved locator is reported at all, rather than the old
    # file path or the empty string the fallback returns.
    from scitex_dev.store import host_store

    from scitex_agent_container._state.relocation_pg import LEASE_STORE

    expected = str(host_store(pkg="scitex_agent_container", name=LEASE_STORE).locator)
    # Act
    facts = gather_lease_facts(AGENT, from_host=B, now=NOW, load=lambda a: None)
    # Assert
    assert facts.store == expected
