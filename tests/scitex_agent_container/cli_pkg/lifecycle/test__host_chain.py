"""Tests for ``cli_pkg.lifecycle._host_chain`` — ``host:`` as a real chain.

The type has promised "priority order; first available host wins (fallback
chain)" since v3; every reduction site took ``host[0]`` unchecked. These tests
pin the resolver that closes that gap, and — just as hard — pin that a plain
string ``host: <name>`` did NOT change: it is never probed, so a chain feature
cannot regress a pinned placement.

No-mocks: the resolver is pure and exercised directly, the reachability oracle
is an injected callable (the seam the module exists to provide), and the ssh
oracle is driven through its ``runner`` seam with a real ``build_ssh_argv``
and real ``PeerSpec`` rows. Nothing here touches the network. Conforms to
STX-TQ002 (AAA markers), STX-TQ003 (descriptive names), STX-TQ007 (one assert
per test).
"""

from __future__ import annotations

from scitex_agent_container._state.host_config import PeerSpec
from scitex_agent_container.cli_pkg.lifecycle._host_chain import (
    LOCAL,
    REACHABLE,
    REMOTE,
    UNKNOWN,
    UNREACHABLE,
    UNROUTABLE,
    HostCandidate,
    HostChainRoute,
    chain_hosts,
    format_unroutable_chain_error,
    is_remote_placement,
    resolve_host_chain,
    ssh_reachability_oracle,
)

_PEERS = {
    "peer-a": PeerSpec(name="peer-a", ssh="peer-a"),
    "peer-b": PeerSpec(name="peer-b", ssh="peer-b"),
}
_HERE = "this-host"
_LOCAL_NAMES = {"this-host", "this-host-alias"}


def _oracle(**verdicts: str):
    """Build a recording reachability oracle; unlisted hosts answer UNKNOWN."""
    calls: list[str] = []

    def _fn(host: str) -> str:
        calls.append(host)
        return verdicts.get(host.replace("-", "_"), UNKNOWN)

    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


def _resolve(spec_host, oracle=None) -> HostChainRoute:
    """Resolve against the shared fixture topology."""
    return resolve_host_chain(
        spec_host, _HERE, _PEERS, local_names=_LOCAL_NAMES, reachability=oracle
    )


# ---------------------------------------------------------------------------
# chain_hosts — the one normalizer every reduction site shares.
# ---------------------------------------------------------------------------


def test_chain_hosts_wraps_a_string_in_a_single_entry_list():
    # Arrange
    spec_host = "peer-a"
    # Act
    hosts = chain_hosts(spec_host)
    # Assert
    assert hosts == ["peer-a"]


def test_chain_hosts_treats_empty_string_as_unpinned():
    # Arrange
    spec_host = ""
    # Act
    hosts = chain_hosts(spec_host)
    # Assert
    assert hosts == []


def test_chain_hosts_treats_none_as_unpinned():
    # Arrange
    spec_host = None
    # Act
    hosts = chain_hosts(spec_host)
    # Assert
    assert hosts == []


def test_chain_hosts_drops_empty_entries_from_a_list():
    # Arrange — a hand-edited spec can leave a blank list entry behind.
    spec_host = ["peer-a", "", "peer-b"]
    # Act
    hosts = chain_hosts(spec_host)
    # Assert
    assert hosts == ["peer-a", "peer-b"]


# ---------------------------------------------------------------------------
# A PLAIN STRING IS NOT A CHAIN — the hard no-regression requirement.
# ---------------------------------------------------------------------------


def test_string_host_naming_this_machine_routes_local():
    # Arrange
    spec_host = "this-host"
    # Act
    route = _resolve(spec_host)
    # Assert
    assert route.kind == LOCAL


def test_string_host_naming_a_local_alias_routes_local():
    # Arrange — an alias spelling of this machine is still this machine.
    spec_host = "this-host-alias"
    # Act
    route = _resolve(spec_host)
    # Assert
    assert route.kind == LOCAL


def test_string_host_naming_a_registered_peer_routes_remote():
    # Arrange
    spec_host = "peer-a"
    # Act
    route = _resolve(spec_host)
    # Assert
    assert (route.kind, route.peer) == (REMOTE, "peer-a")


def test_string_host_naming_nothing_is_unroutable():
    # Arrange
    spec_host = "spartn-typo"
    # Act
    route = _resolve(spec_host)
    # Assert
    assert route.kind == UNROUTABLE


def test_string_host_is_never_handed_to_the_reachability_oracle():
    # Arrange — a string has nothing to fall back TO, so probing it could
    # only turn a working dispatch into a refusal.
    oracle = _oracle(peer_a=UNREACHABLE)
    # Act
    _resolve("peer-a", oracle)
    # Assert
    assert oracle.calls == []


def test_unreachable_string_host_still_routes_remote_unchanged():
    # Arrange — even with the oracle screaming, a pinned string behaves
    # byte-identically to the pre-chain resolver.
    oracle = _oracle(peer_a=UNREACHABLE)
    # Act
    route = _resolve("peer-a", oracle)
    # Assert
    assert (route.kind, route.peer) == (REMOTE, "peer-a")


# ---------------------------------------------------------------------------
# Unpinned placement.
# ---------------------------------------------------------------------------


def test_absent_host_routes_local():
    # Arrange
    spec_host = None
    # Act
    route = _resolve(spec_host)
    # Assert
    assert route.kind == LOCAL


def test_empty_string_host_routes_local():
    # Arrange
    spec_host = ""
    # Act
    route = _resolve(spec_host)
    # Assert
    assert route.kind == LOCAL


def test_empty_list_host_routes_local():
    # Arrange
    spec_host: list[str] = []
    # Act
    route = _resolve(spec_host)
    # Assert
    assert route.kind == LOCAL


# ---------------------------------------------------------------------------
# The chain itself — first NOT-REJECTED candidate wins.
# ---------------------------------------------------------------------------


def test_chain_with_reachable_head_routes_to_the_head():
    # Arrange
    oracle = _oracle(peer_a=REACHABLE, peer_b=REACHABLE)
    # Act
    route = _resolve(["peer-a", "peer-b"], oracle)
    # Assert
    assert (route.kind, route.peer) == (REMOTE, "peer-a")


def test_chain_skips_an_unreachable_head_for_the_reachable_next():
    # Arrange — the 2026-08-09 shape: the head answers Permission denied.
    oracle = _oracle(peer_a=UNREACHABLE, peer_b=REACHABLE)
    # Act
    route = _resolve(["peer-a", "peer-b"], oracle)
    # Assert
    assert (route.kind, route.peer) == (REMOTE, "peer-b")


def test_chain_skips_an_unreachable_head_for_a_local_tail():
    # Arrange — the degradation the operator actually wants: run here.
    oracle = _oracle(peer_a=UNREACHABLE)
    # Act
    route = _resolve(["peer-a", "this-host"], oracle)
    # Assert
    assert route.kind == LOCAL


def test_chain_headed_by_this_machine_routes_local():
    # Arrange
    oracle = _oracle()
    # Act
    route = _resolve(["this-host", "peer-a"], oracle)
    # Assert
    assert route.kind == LOCAL


def test_chain_headed_by_this_machine_never_yields_a_dispatch_peer():
    # Arrange — never ssh-dispatch to self, even when this machine is ALSO
    # registered as a peer (the ``ssh: localhost`` shape that took the
    # fleet down).
    peers = dict(_PEERS)
    peers["this-host"] = PeerSpec(name="this-host", ssh="localhost")
    # Act
    route = resolve_host_chain(
        ["this-host", "peer-a"], _HERE, peers, local_names=_LOCAL_NAMES
    )
    # Assert
    assert route.peer is None


def test_local_chain_entry_is_never_probed():
    # Arrange — we ARE this machine; its availability needs no ssh.
    oracle = _oracle()
    # Act
    _resolve(["this-host", "peer-a"], oracle)
    # Assert
    assert oracle.calls == []


def test_chain_stops_probing_once_a_candidate_wins():
    # Arrange — each probe is an ssh round-trip, so the walk must be lazy.
    oracle = _oracle(peer_a=REACHABLE, peer_b=REACHABLE)
    # Act
    _resolve(["peer-a", "peer-b"], oracle)
    # Assert
    assert oracle.calls == ["peer-a"]


def test_chain_skips_an_unroutable_name_for_the_next_entry():
    # Arrange — a typo mid-chain must not sink the whole placement.
    oracle = _oracle(peer_b=REACHABLE)
    # Act
    route = _resolve(["spartn-typo", "peer-b"], oracle)
    # Assert
    assert (route.kind, route.peer) == (REMOTE, "peer-b")


def test_route_records_the_winning_chain_entry():
    # Arrange
    oracle = _oracle(peer_a=UNREACHABLE, peer_b=REACHABLE)
    # Act
    route = _resolve(["peer-a", "peer-b"], oracle)
    # Assert
    assert route.host == "peer-b"


# ---------------------------------------------------------------------------
# THREE-VALUED — UNKNOWN is neither pole, and rejects nothing.
# ---------------------------------------------------------------------------


def test_chain_without_an_oracle_keeps_the_head():
    # Arrange — no oracle means no evidence, and no evidence rejects
    # nothing: this is the historical host[0] answer, preserved.
    spec_host = ["peer-a", "peer-b"]
    # Act
    route = _resolve(spec_host)
    # Assert
    assert (route.kind, route.peer) == (REMOTE, "peer-a")


def test_unknown_reachability_does_not_reject_the_head():
    # Arrange — probed, but the probe could not reach a verdict.
    oracle = _oracle(peer_a=UNKNOWN, peer_b=REACHABLE)
    # Act
    route = _resolve(["peer-a", "peer-b"], oracle)
    # Assert — "I could not tell" must not be read as "it is down".
    assert (route.kind, route.peer) == (REMOTE, "peer-a")


def test_unknown_reachability_is_recorded_distinctly_from_reachable():
    # Arrange
    oracle = _oracle(peer_a=UNKNOWN)
    # Act
    route = _resolve(["peer-a", "peer-b"], oracle)
    # Assert — the verdict survives into the record; it is not laundered.
    assert route.candidates[0].reachability == UNKNOWN


def test_raising_oracle_degrades_to_unknown_not_unreachable():
    # Arrange — an oracle bug must not eject a healthy host from the chain.
    def _boom(host: str) -> str:
        raise OSError("probe exploded")

    # Act
    route = _resolve(["peer-a", "peer-b"], _boom)
    # Assert
    assert (route.kind, route.peer) == (REMOTE, "peer-a")


def test_oracle_answering_outside_the_vocabulary_degrades_to_unknown():
    # Arrange — a stray truthy string is not a licence to reject.
    oracle = _oracle(peer_a="probably?")
    # Act
    route = _resolve(["peer-a", "peer-b"], oracle)
    # Assert
    assert route.candidates[0].reachability == UNKNOWN


# ---------------------------------------------------------------------------
# HostCandidate.rejected — only EVIDENCE rejects.
# ---------------------------------------------------------------------------


def test_unreachable_candidate_is_rejected():
    # Arrange
    candidate = HostCandidate("peer-a", "remote", UNREACHABLE)
    # Act
    rejected = candidate.rejected
    # Assert
    assert rejected is True


def test_unknown_reachability_candidate_is_not_rejected():
    # Arrange
    candidate = HostCandidate("peer-a", "remote", UNKNOWN)
    # Act
    rejected = candidate.rejected
    # Assert
    assert rejected is False


def test_unroutable_name_candidate_is_rejected():
    # Arrange
    candidate = HostCandidate("spartn-typo", "unknown")
    # Act
    rejected = candidate.rejected
    # Assert
    assert rejected is True


def test_local_candidate_is_not_rejected():
    # Arrange
    candidate = HostCandidate("this-host", "local")
    # Act
    rejected = candidate.rejected
    # Assert
    assert rejected is False


# ---------------------------------------------------------------------------
# The whole chain unusable — LOUD, never a silent local start.
# ---------------------------------------------------------------------------


def test_fully_unreachable_chain_is_unroutable():
    # Arrange
    oracle = _oracle(peer_a=UNREACHABLE, peer_b=UNREACHABLE)
    # Act
    route = _resolve(["peer-a", "peer-b"], oracle)
    # Assert
    assert route.kind == UNROUTABLE


def test_fully_unreachable_chain_never_yields_a_peer():
    # Arrange
    oracle = _oracle(peer_a=UNREACHABLE, peer_b=UNREACHABLE)
    # Act
    route = _resolve(["peer-a", "peer-b"], oracle)
    # Assert — an unusable chain must not leak an ssh target.
    assert route.peer is None


def test_chain_of_only_unroutable_names_is_unroutable():
    # Arrange
    spec_host = ["spartn-typo", "other-typo"]
    # Act
    route = _resolve(spec_host)
    # Assert
    assert route.kind == UNROUTABLE


def test_unroutable_route_records_every_candidate_it_examined():
    # Arrange
    oracle = _oracle(peer_a=UNREACHABLE, peer_b=UNREACHABLE)
    # Act
    route = _resolve(["peer-a", "spartn-typo", "peer-b"], oracle)
    # Assert
    assert [c.host for c in route.candidates] == [
        "peer-a",
        "spartn-typo",
        "peer-b",
    ]


def test_unroutable_route_distinguishes_unreachable_from_unknown_name():
    # Arrange
    oracle = _oracle(peer_a=UNREACHABLE)
    # Act
    route = _resolve(["peer-a", "spartn-typo"], oracle)
    # Assert
    assert [c.reachability for c in route.candidates] == [UNREACHABLE, ""]


# ---------------------------------------------------------------------------
# format_unroutable_chain_error — name every candidate and the fix.
# ---------------------------------------------------------------------------


def _unroutable_message(verb: str = "start") -> str:
    oracle = _oracle(peer_a=UNREACHABLE)
    route = _resolve(["peer-a", "spartn-typo"], oracle)
    return format_unroutable_chain_error(
        "alpha", route, _PEERS, verb=verb, current_host=_HERE
    )


def test_unroutable_message_names_the_unreachable_candidate():
    # Arrange
    verb = "start"
    # Act
    msg = _unroutable_message(verb)
    # Assert
    assert "peer-a — UNREACHABLE" in msg


def test_unroutable_message_names_the_unroutable_name_candidate():
    # Arrange
    verb = "start"
    # Act
    msg = _unroutable_message(verb)
    # Assert
    assert "spartn-typo — UNKNOWN HOST" in msg


def test_unroutable_message_names_the_agent():
    # Arrange
    verb = "start"
    # Act
    msg = _unroutable_message(verb)
    # Assert
    assert "'alpha'" in msg


def test_unroutable_message_lists_the_registered_peers():
    # Arrange
    verb = "start"
    # Act
    msg = _unroutable_message(verb)
    # Assert
    assert "Registered peers: peer-a, peer-b" in msg


def test_unroutable_message_points_at_sac_host_list():
    # Arrange
    verb = "start"
    # Act
    msg = _unroutable_message(verb)
    # Assert
    assert "sac host list" in msg


def test_unroutable_message_names_this_machine_as_a_chain_fix():
    # Arrange
    verb = "start"
    # Act
    msg = _unroutable_message(verb)
    # Assert — the actionable fix is "append me to the chain".
    assert "host: [..., this-host]" in msg


def test_unroutable_start_message_offers_the_no_redispatch_escape():
    # Arrange
    verb = "start"
    # Act
    msg = _unroutable_message(verb)
    # Assert
    assert "--no-redispatch" in msg


def test_unroutable_stop_message_offers_the_on_peer_escape():
    # Arrange
    verb = "stop"
    # Act
    msg = _unroutable_message(verb)
    # Assert
    assert "sac --on" in msg


def test_unroutable_message_keeps_the_candidates_in_priority_order():
    # Arrange
    verb = "start"
    # Act
    msg = _unroutable_message(verb)
    # Assert
    assert msg.index("1. peer-a") < msg.index("2. spartn-typo")


# ---------------------------------------------------------------------------
# is_remote_placement — the peer-table-free question (resume preflight).
# ---------------------------------------------------------------------------


def test_string_host_elsewhere_is_a_remote_placement():
    # Arrange
    spec_host = "peer-a"
    # Act
    remote = is_remote_placement(spec_host, _HERE)
    # Assert
    assert remote is True


def test_string_host_naming_this_machine_is_not_remote():
    # Arrange
    spec_host = _HERE
    # Act
    remote = is_remote_placement(spec_host, _HERE)
    # Assert
    assert remote is False


def test_unpinned_placement_is_not_remote():
    # Arrange
    spec_host = ""
    # Act
    remote = is_remote_placement(spec_host, _HERE)
    # Assert
    assert remote is False


def test_chain_containing_this_machine_is_not_a_remote_placement():
    # Arrange — the head is elsewhere, but the chain can degrade to here,
    # so a preflight must not assume another machine.
    spec_host = ["peer-a", _HERE]
    # Act
    remote = is_remote_placement(spec_host, _HERE)
    # Assert
    assert remote is False


def test_chain_without_this_machine_is_a_remote_placement():
    # Arrange
    spec_host = ["peer-a", "peer-b"]
    # Act
    remote = is_remote_placement(spec_host, _HERE)
    # Assert
    assert remote is True


# ---------------------------------------------------------------------------
# ssh_reachability_oracle — the production probe, driven through its runner
# seam (real build_ssh_argv, real PeerSpec, no network).
# ---------------------------------------------------------------------------


def test_ssh_oracle_reports_reachable_on_exit_zero():
    # Arrange
    oracle = ssh_reachability_oracle(_PEERS, runner=lambda argv: 0)
    # Act
    verdict = oracle("peer-a")
    # Assert
    assert verdict == REACHABLE


def test_ssh_oracle_reports_unreachable_on_permission_denied():
    # Arrange — ssh exits 255 on ``Permission denied (publickey)``, the
    # 2026-08-09 failure a chain must degrade past.
    oracle = ssh_reachability_oracle(_PEERS, runner=lambda argv: 255)
    # Act
    verdict = oracle("peer-a")
    # Assert
    assert verdict == UNREACHABLE


def test_ssh_oracle_reports_unknown_when_the_probe_cannot_run():
    # Arrange — no ssh binary / wedged process is "we could not look".
    def _explode(argv: list[str]) -> int:
        raise FileNotFoundError("ssh")

    oracle = ssh_reachability_oracle(_PEERS, runner=_explode)
    # Act
    verdict = oracle("peer-a")
    # Assert
    assert verdict == UNKNOWN


def test_ssh_oracle_reports_unknown_for_an_unregistered_host():
    # Arrange — build_ssh_argv cannot render a hop we have no peer for.
    oracle = ssh_reachability_oracle(_PEERS, runner=lambda argv: 0)
    # Act
    verdict = oracle("spartn-typo")
    # Assert
    assert verdict == UNKNOWN


def test_ssh_oracle_probes_each_host_at_most_once():
    # Arrange
    calls: list[list[str]] = []

    def _runner(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    oracle = ssh_reachability_oracle(_PEERS, runner=_runner)
    # Act
    oracle("peer-a")
    oracle("peer-a")
    # Assert
    assert len(calls) == 1


def test_ssh_oracle_renders_the_probe_through_build_ssh_argv():
    # Arrange — the probe must ride the same primitive as the dispatch, or
    # it is answering about a different route than the one we will take.
    captured: list[list[str]] = []

    def _runner(argv: list[str]) -> int:
        captured.append(argv)
        return 0

    oracle = ssh_reachability_oracle(_PEERS, runner=_runner)
    # Act
    oracle("peer-a")
    # Assert
    assert captured[0][0] == "ssh"
