"""Tests for the fleet fan-out and its mandatory reachability header.

No mocks (PA-306), and no ``monkeypatch``: every seam is a REAL callable —
``local_lister`` is a plain function returning rows, ``peer_probe`` is a plain
function returning ``(HostReport, rows)`` — and the local hostname is pinned
through the documented ``SCITEX_AGENT_CONTAINER_HOSTNAME`` env var that
``resolve_hostname`` already honours. That matters more than usual here: the
whole feature is about not confusing "I could not observe" with "there is
nothing", and a fixture that answers every call cannot exercise that.
"""

from __future__ import annotations

import time

import pytest

from scitex_agent_container.cli_pkg._helpers._agent_list_fleet import (
    RESPONDED,
    TIMED_OUT,
    UNREACHABLE,
    HostReport,
    HostTarget,
    UnknownHostFilter,
    collect_fleet,
    resolve_host_filter,
)
from scitex_agent_container.cli_pkg._helpers._agent_list_fleet_model import (
    INSTRUMENT_LOCAL_REGISTRY,
    INSTRUMENT_SSH,
    NOT_QUERIED,
)
from scitex_agent_container.cli_pkg._helpers._agent_list_fleet_render import (
    fleet_header_lines,
    hosts_payload,
    summary_line,
)

LOCAL = "test-local-host"


@pytest.fixture(autouse=True)
def fleet_env(env_save_restore):
    """Pin this machine's name and lift the suite-wide local-only floor.

    ``tests/conftest.py`` force-sets ``SAC_AGENTS_LIST_NO_FANOUT=1`` so no test
    ever ssh's into the operator's real fleet by accident. Everything here
    drives the peer leg through explicit ``targets=`` / ``peer_probe=`` seams,
    which never touch the network, so the floor is deliberately lifted.
    """
    env_save_restore.delete("SAC_AGENTS_LIST_NO_FANOUT")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_AGENTS_LIST_NO_FANOUT")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", LOCAL)
    return env_save_restore


def _targets(*peers: str) -> list[HostTarget]:
    return [HostTarget(name=LOCAL, ssh="", local=True)] + [
        HostTarget(name=p, ssh=p) for p in peers
    ]


def _row(name: str, host: str = "local") -> dict:
    return {"name": name, "status": "running", "host": host, "host_display": host}


def _no_local() -> list[dict]:
    return []


def _one_local() -> list[dict]:
    return [_row("local-agent")]


def _responded(host: str, rows: list[dict]) -> tuple[HostReport, list[dict]]:
    return (
        HostReport(
            host=host,
            status=RESPONDED,
            instrument=INSTRUMENT_SSH,
            detail="sac agents list --json over ssh",
            agents=len(rows),
        ),
        rows,
    )


def _timed_out(host: str, seconds: float = 8.0) -> tuple[HostReport, list[dict]]:
    return (
        HostReport(
            host=host,
            status=TIMED_OUT,
            instrument=INSTRUMENT_SSH,
            detail=f"ssh timed out after {seconds:g}s",
        ),
        [],
    )


def _probe_pair(answers: dict[str, tuple[HostReport, list[dict]]]):
    def probe(target: HostTarget, timeout_s: float):
        return answers[target.name]

    return probe


@pytest.fixture
def five_of_six():
    """One local host plus five peers, exactly one of which never answers."""
    return collect_fleet(
        targets=_targets("mba", "spartan", "nas-03", "compute-01", "compute-02"),
        local_lister=_one_local,
        peer_probe=_probe_pair(
            {
                "mba": _responded("mba", [_row("mba-agent", "mba")]),
                "spartan": _timed_out("spartan"),
                "nas-03": _responded("nas-03", []),
                "compute-01": _responded("compute-01", []),
                "compute-02": _responded("compute-02", []),
            }
        ),
    )


def _instrument_line(listing) -> str:
    return next(ln for ln in fleet_header_lines(listing) if ln.startswith("instruments:"))


# ===========================================================================
# The header: the responded / unresponsive split, with the reason
# ===========================================================================


def test_summary_line_counts_responded_over_total(five_of_six):
    # Arrange
    listing = five_of_six
    # Act
    line = summary_line(listing)
    # Assert
    assert line.startswith("5/6 hosts responded")


def test_summary_line_names_the_host_that_did_not_answer(five_of_six):
    # Arrange
    listing = five_of_six
    # Act
    line = summary_line(listing)
    # Assert
    assert "spartan" in line


def test_summary_line_carries_the_reason_not_just_the_name(five_of_six):
    # Arrange -- a bare name would tell the operator nothing actionable.
    listing = five_of_six
    # Act
    line = summary_line(listing)
    # Assert
    assert "ssh timed out after 8s" in line


def test_header_names_the_instrument_for_the_local_host(five_of_six):
    # Arrange
    listing = five_of_six
    # Act
    line = _instrument_line(listing)
    # Assert
    assert f"{LOCAL}={INSTRUMENT_LOCAL_REGISTRY}" in line


def test_header_names_the_instrument_for_a_peer(five_of_six):
    # Arrange
    listing = five_of_six
    # Act
    line = _instrument_line(listing)
    # Assert
    assert "mba=ssh" in line


def test_header_marks_the_unanswered_host_in_the_instrument_line(five_of_six):
    # Arrange
    listing = five_of_six
    # Act
    line = _instrument_line(listing)
    # Assert
    assert "spartan=ssh (no answer)" in line


def test_json_payload_reports_the_responded_over_total_split(five_of_six):
    # Arrange
    listing = five_of_six
    # Act
    payload = hosts_payload(listing)
    # Assert
    assert (payload["responded"], payload["total"]) == (5, 6)


def test_json_payload_repeats_the_header_lines_verbatim(five_of_six):
    # Arrange -- a log of the JSON must read like the terminal did.
    listing = five_of_six
    # Act
    payload = hosts_payload(listing)
    # Assert
    assert payload["header"] == fleet_header_lines(listing)


def test_json_report_for_a_responding_host_counts_its_agents(five_of_six):
    # Arrange
    payload = hosts_payload(five_of_six)
    # Act
    mba = next(r for r in payload["reports"] if r["host"] == "mba")
    # Assert
    assert mba["agents"] == 1


def test_json_report_for_a_timed_out_host_leaves_the_count_unknown(five_of_six):
    # Arrange -- None, NEVER 0: zero is the same lie as omitting the row.
    payload = hosts_payload(five_of_six)
    # Act
    spartan = next(r for r in payload["reports"] if r["host"] == "spartan")
    # Assert
    assert spartan["agents"] is None


def test_json_report_keeps_the_timed_out_status_distinct(five_of_six):
    # Arrange
    payload = hosts_payload(five_of_six)
    # Act
    spartan = next(r for r in payload["reports"] if r["host"] == "spartan")
    # Assert
    assert spartan["status"] == TIMED_OUT


# ===========================================================================
# UNKNOWN is not EMPTY -- the two must be distinguishable
# ===========================================================================


def test_a_timed_out_host_is_present_in_the_reports_not_absent():
    # Arrange
    probe = _probe_pair({"spartan": _timed_out("spartan")})
    # Act
    listing = collect_fleet(
        targets=_targets("spartan"), local_lister=_no_local, peer_probe=probe
    )
    # Assert
    assert [r.host for r in listing.reports] == [LOCAL, "spartan"]


@pytest.fixture
def empty_beside_silent():
    """Two peers contributing zero rows: one answered, one never did."""
    return collect_fleet(
        targets=_targets("empty-host", "silent-host"),
        local_lister=_no_local,
        peer_probe=_probe_pair(
            {
                "empty-host": _responded("empty-host", []),
                "silent-host": _timed_out("silent-host"),
            }
        ),
    )


def test_an_answered_but_empty_host_reports_zero_agents(empty_beside_silent):
    # Arrange
    by_host = {r.host: r for r in empty_beside_silent.reports}
    # Act
    empty = by_host["empty-host"]
    # Assert
    assert empty.agents == 0


def test_a_silent_host_reports_an_unknown_agent_count(empty_beside_silent):
    # Arrange -- same zero rows in the table; only the report tells them apart.
    by_host = {r.host: r for r in empty_beside_silent.reports}
    # Act
    silent = by_host["silent-host"]
    # Assert
    assert silent.agents is None


def test_an_empty_fleet_reports_every_host_as_answered():
    # Arrange
    probe = _probe_pair({"peer": _responded("peer", [])})
    # Act
    payload = hosts_payload(
        collect_fleet(targets=_targets("peer"), local_lister=_no_local, peer_probe=probe)
    )
    # Assert
    assert (payload["responded"], payload["total"]) == (2, 2)


def test_an_unobserved_fleet_reports_a_short_count_on_the_same_empty_rows():
    # Arrange -- byte-identical `agents: []`; the header is the only difference.
    probe = _probe_pair({"peer": _timed_out("peer")})
    # Act
    payload = hosts_payload(
        collect_fleet(targets=_targets("peer"), local_lister=_no_local, peer_probe=probe)
    )
    # Assert
    assert (payload["responded"], payload["total"]) == (1, 2)


def test_a_probe_that_raises_becomes_a_reported_host_not_a_crash():
    # Arrange
    def exploding(target: HostTarget, timeout_s: float):
        raise RuntimeError("probe blew up")

    # Act
    listing = collect_fleet(
        targets=_targets("boom"), local_lister=_no_local, peer_probe=exploding
    )
    # Assert
    assert next(r for r in listing.reports if r.host == "boom").status == UNREACHABLE


def test_a_local_read_that_fails_is_reported_rather_than_rendered_as_empty():
    # Arrange
    def broken() -> list[dict]:
        raise OSError("registry is gone")

    # Act
    listing = collect_fleet(targets=_targets(), local_lister=broken)
    # Assert
    assert "registry is gone" in listing.reports[0].detail


# ===========================================================================
# Fan-out is CONCURRENT: bounded by the slowest peer, not by their sum
# ===========================================================================


def test_fan_out_wall_clock_is_bounded_by_the_slowest_peer_not_their_sum():
    # Arrange -- five peers, each taking 0.30s. Sequentially that is 1.50s.
    delay = 0.30
    peers = [f"peer-{i}" for i in range(5)]

    def slow_probe(target: HostTarget, timeout_s: float):
        time.sleep(delay)
        return _responded(target.name, [])

    # Act
    started = time.monotonic()
    collect_fleet(
        targets=_targets(*peers), local_lister=_no_local, peer_probe=slow_probe
    )
    elapsed = time.monotonic() - started
    # Assert
    assert elapsed < delay * len(peers) / 2, f"took {elapsed:.2f}s, not concurrent"


def test_every_peer_still_answers_when_the_fan_out_is_concurrent():
    # Arrange
    peers = [f"peer-{i}" for i in range(5)]

    def slow_probe(target: HostTarget, timeout_s: float):
        time.sleep(0.05)
        return _responded(target.name, [])

    # Act
    listing = collect_fleet(
        targets=_targets(*peers), local_lister=_no_local, peer_probe=slow_probe
    )
    # Assert
    assert listing.responded == len(peers) + 1


def _stalling_probe(target: HostTarget, timeout_s: float):
    if target.name == "stalled":
        time.sleep(5.0)
    return _responded(target.name, [])


def _stalled_listing():
    return collect_fleet(
        targets=_targets("stalled", "quick-1", "quick-2"),
        local_lister=_no_local,
        peer_probe=_stalling_probe,
        host_timeout_s=0.2,
    )


def test_one_stalled_peer_does_not_hold_the_batch_past_the_shared_budget():
    # Arrange
    started = time.monotonic()
    # Act
    _stalled_listing()
    elapsed = time.monotonic() - started
    # Assert
    assert elapsed < 3.5, f"the stalled peer held the batch for {elapsed:.2f}s"


def test_a_stalled_peer_is_reported_as_timed_out_rather_than_dropped():
    # Arrange
    listing = _stalled_listing()
    # Act
    stalled = next(r for r in listing.reports if r.host == "stalled")
    # Assert
    assert stalled.status == TIMED_OUT


# ===========================================================================
# --host: exact match, localhost resolved at parse time, unknown fails loud
# ===========================================================================


def test_host_localhost_selects_this_machine():
    # Arrange
    targets = _targets("mba")
    # Act
    selected, _ = resolve_host_filter(["localhost"], targets, LOCAL)
    # Assert
    assert [t.name for t in selected] == [LOCAL]


def test_host_localhost_is_recorded_as_a_resolution():
    # Arrange -- the OUTPUT must never keep the ambiguous word.
    targets = _targets("mba")
    # Act
    _, resolutions = resolve_host_filter(["localhost"], targets, LOCAL)
    # Assert
    assert resolutions == (("localhost", LOCAL),)


def test_host_local_is_accepted_as_the_same_alias():
    # Arrange
    targets = _targets("mba")
    # Act
    _, resolutions = resolve_host_filter(["local"], targets, LOCAL)
    # Assert
    assert resolutions == (("local", LOCAL),)


def test_a_concrete_hostname_is_not_recorded_as_a_resolution():
    # Arrange -- nothing was rewritten, so nothing is echoed.
    targets = _targets("mba")
    # Act
    _, resolutions = resolve_host_filter(["mba"], targets, LOCAL)
    # Assert
    assert resolutions == ()


def test_host_is_repeatable_and_selects_exactly_those_hosts():
    # Arrange
    targets = _targets("mba", "spartan", "nas-03")
    # Act
    selected, _ = resolve_host_filter(["mba", "spartan"], targets, LOCAL)
    # Assert
    assert [t.name for t in selected] == ["mba", "spartan"]


def test_an_unknown_host_fails_loud_rather_than_returning_nothing():
    # Arrange -- silence would render "no such host" exactly like
    # "that host has no agents".
    targets = _targets("mba", "spartan")
    # Act
    attempt = lambda: resolve_host_filter(["nope"], targets, LOCAL)  # noqa: E731
    # Assert
    with pytest.raises(UnknownHostFilter):
        attempt()


def test_the_unknown_host_error_names_every_host_that_would_have_worked():
    # Arrange
    targets = _targets("mba", "spartan")
    # Act
    try:
        resolve_host_filter(["nope"], targets, LOCAL)
        message = ""
    except UnknownHostFilter as exc:
        message = str(exc)
    # Assert
    assert all(name in message for name in (LOCAL, "mba", "spartan"))


def test_an_alias_of_a_deduped_host_still_selects_it():
    # Arrange -- one machine reached through two peer keys.
    targets = [
        HostTarget(name=LOCAL, ssh="", local=True),
        HostTarget(name="nas-03", ssh="scitex-nas-03", aliases=("scitex-nas-03",)),
    ]
    # Act
    selected, _ = resolve_host_filter(["scitex-nas-03"], targets, LOCAL)
    # Assert
    assert [t.name for t in selected] == ["nas-03"]


def test_the_header_echoes_the_localhost_resolution():
    # Arrange
    listing = collect_fleet(
        targets=_targets("mba"), local_lister=_one_local, hosts=["localhost"]
    )
    # Act
    first = fleet_header_lines(listing)[0]
    # Assert
    assert first == f"--host localhost → {LOCAL}"


def test_the_json_filter_block_records_the_resolution():
    # Arrange
    listing = collect_fleet(
        targets=_targets("mba"), local_lister=_one_local, hosts=["localhost"]
    )
    # Act
    payload = hosts_payload(listing)
    # Assert
    assert payload["filter"]["resolutions"] == [
        {"requested": "localhost", "resolved": LOCAL}
    ]


def test_filtering_to_one_host_queries_only_that_host():
    # Arrange
    asked: list[str] = []

    def probe(target: HostTarget, timeout_s: float):
        asked.append(target.name)
        return _responded(target.name, [])

    # Act
    collect_fleet(
        targets=_targets("mba", "spartan"),
        local_lister=_no_local,
        hosts=["spartan"],
        peer_probe=probe,
    )
    # Assert
    assert asked == ["spartan"]


def test_filtering_to_one_host_reports_only_that_host():
    # Arrange
    probe = _probe_pair({"spartan": _responded("spartan", [])})
    # Act
    listing = collect_fleet(
        targets=_targets("mba", "spartan"),
        local_lister=_no_local,
        hosts=["spartan"],
        peer_probe=probe,
    )
    # Assert
    assert [r.host for r in listing.reports] == ["spartan"]


# ===========================================================================
# Fan-out suppression is announced, never silent
# ===========================================================================


def test_suppressed_fanout_says_so_in_the_header():
    # Arrange
    listing = collect_fleet(
        targets=_targets("mba", "spartan"), local_lister=_one_local, no_fanout=True
    )
    # Act
    line = summary_line(listing)
    # Assert
    assert "2 peers NOT queried (--no-fanout)" in line


def test_a_localhost_only_listing_does_not_mention_the_peers_it_excluded():
    # Arrange -- the caller asked for one host; the others are not news, and a
    # note about them would train him to skim the line that matters.
    listing = collect_fleet(
        targets=_targets("mba", "spartan"),
        local_lister=_one_local,
        hosts=["localhost"],
        no_fanout=True,
    )
    # Act
    line = summary_line(listing)
    # Assert
    assert "NOT queried" not in line


def test_a_named_peer_is_not_also_counted_in_the_aggregate_note():
    # Arrange -- it already has its own not_queried row; saying it twice makes
    # the header noisier without making it truer.
    listing = collect_fleet(
        targets=_targets("mba", "spartan"),
        local_lister=_one_local,
        hosts=["localhost", "spartan"],
        no_fanout=True,
    )
    # Act
    line = summary_line(listing)
    # Assert
    assert "NOT queried" not in line


def test_a_named_host_that_was_not_queried_gets_its_own_row():
    # Arrange -- the operator ASKED for spartan; silence would read "empty".
    listing = collect_fleet(
        targets=_targets("mba", "spartan"),
        local_lister=_one_local,
        hosts=["spartan"],
        no_fanout=True,
    )
    # Act
    spartan = next(r for r in listing.reports if r.host == "spartan")
    # Assert
    assert spartan.status == NOT_QUERIED


def test_a_host_that_was_not_queried_has_an_unknown_agent_count():
    # Arrange
    listing = collect_fleet(
        targets=_targets("spartan"),
        local_lister=_one_local,
        hosts=["spartan"],
        no_fanout=True,
    )
    # Act
    spartan = next(r for r in listing.reports if r.host == "spartan")
    # Assert
    assert spartan.agents is None


def test_the_env_switch_is_named_in_the_header_so_it_is_never_silent(fleet_env):
    # Arrange
    fleet_env.set("SAC_AGENTS_LIST_NO_FANOUT", "1")
    # Act
    line = summary_line(collect_fleet(targets=_targets("mba"), local_lister=_one_local))
    # Assert
    assert "SAC_AGENTS_LIST_NO_FANOUT" in line
