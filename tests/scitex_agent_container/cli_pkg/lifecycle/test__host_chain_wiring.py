"""The reduction sites actually ROUTE through the chain resolver.

``_host_chain`` being correct is worth nothing if the control plane still
reads ``spec.host[0]``. These tests drive the real call sites — the start
dispatcher and the singleton-skip liveness gate — and pin the behaviours a
chain is supposed to buy: a down head degrades, a chain that names this
machine runs locally, and a chain with nothing usable fails LOUD naming every
candidate.

No-mocks: real ``AgentConfig`` / ``HostsSpec`` / ``PeerSpec`` objects, and the
functions' own injection seams (``reachability``, ``dispatcher``,
``liveness_oracle``) instead of patching. Nothing here touches the network or
PATH. Conforms to STX-TQ002 (AAA markers), STX-TQ003 (descriptive names),
STX-TQ007 (one assert per test).
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state.host_config import PeerSpec
from scitex_agent_container.cli_pkg.lifecycle._common import (
    _bound_hosts,
    _resolve_singleton_skip,
)
from scitex_agent_container.cli_pkg.lifecycle._dispatch import try_dispatch
from scitex_agent_container.cli_pkg.lifecycle._host_chain import (
    REACHABLE,
    UNKNOWN,
    UNREACHABLE,
)
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import HostsSpec, SchedulingSpec

_HERE = "this-host"
_PEERS = {
    "peer-a": PeerSpec(name="peer-a", ssh="peer-a"),
    "peer-b": PeerSpec(name="peer-b", ssh="peer-b"),
}


def _cfg(host, hosts=None, sched_mode="per-host", pref="") -> AgentConfig:
    """AgentConfig carrying a v3 ``spec.host`` pin (str / list / '')."""
    c = AgentConfig(name="alpha")
    c.hosts_spec = HostsSpec(host=host, hosts=hosts or [])
    c.scheduling = SchedulingSpec(mode=sched_mode, preferred_host=pref)
    return c


def _oracle(**verdicts: str):
    """Recording reachability oracle; unlisted hosts answer UNKNOWN."""
    calls: list[str] = []

    def _fn(host: str) -> str:
        calls.append(host)
        return verdicts.get(host.replace("-", "_"), UNKNOWN)

    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


def _recorder():
    """Recording stand-in for ``_dispatch_remote_start`` (the ssh handoff)."""
    seen: list[str] = []

    def _fn(*, name: str, peer: str, dry_run: bool, force: bool) -> int:
        seen.append(peer)
        return 0

    _fn.seen = seen  # type: ignore[attr-defined]
    return _fn


def _write_peer_config(home, env_save_restore):
    """Register ``peer-a`` in a REAL config.yaml and pin this machine's name.

    Both hostname env forms are set so the override never conflicts with a
    pre-set ``SAC_HOSTNAME`` in the runner env (the pattern the sibling
    routing tests already use).
    """
    cfg = home / "config.yaml"
    cfg.write_text(
        "host:\n  fallback: hostname-short\npeers:\n  peer-a:\n    ssh: peer-a\n"
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    env_save_restore.set("SAC_HOSTNAME", _HERE)
    env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", _HERE)
    return cfg


def _dispatch(config, oracle, dispatcher):
    return try_dispatch(
        config,
        _HERE,
        _PEERS,
        dry_run=False,
        force=False,
        local_names={_HERE},
        reachability=oracle,
        dispatcher=dispatcher,
    )


# ---------------------------------------------------------------------------
# try_dispatch — the start-side route. A chain must DEGRADE.
# ---------------------------------------------------------------------------


def test_start_dispatches_to_a_reachable_chain_head():
    # Arrange
    oracle = _oracle(peer_a=REACHABLE, peer_b=REACHABLE)
    dispatcher = _recorder()
    # Act
    _dispatch(_cfg(["peer-a", "peer-b"]), oracle, dispatcher)
    # Assert
    assert dispatcher.seen == ["peer-a"]


def test_start_degrades_past_an_unreachable_chain_head():
    # Arrange — the 2026-08-09 shape: the head answers Permission denied.
    oracle = _oracle(peer_a=UNREACHABLE, peer_b=REACHABLE)
    dispatcher = _recorder()
    # Act
    _dispatch(_cfg(["peer-a", "peer-b"]), oracle, dispatcher)
    # Assert
    assert dispatcher.seen == ["peer-b"]


def test_start_returns_true_when_the_chain_degrades_to_a_peer():
    # Arrange
    oracle = _oracle(peer_a=UNREACHABLE, peer_b=REACHABLE)
    dispatcher = _recorder()
    # Act
    out = _dispatch(_cfg(["peer-a", "peer-b"]), oracle, dispatcher)
    # Assert
    assert out is True


def test_start_degrades_to_a_local_chain_tail_without_dispatching():
    # Arrange — the degradation the operator actually wants: run HERE.
    oracle = _oracle(peer_a=UNREACHABLE)
    dispatcher = _recorder()
    # Act
    out = _dispatch(_cfg(["peer-a", _HERE]), oracle, dispatcher)
    # Assert — False means "caller proceeds with the local launch".
    assert out is False


def test_start_never_ssh_dispatches_when_the_chain_degrades_local():
    # Arrange
    oracle = _oracle(peer_a=UNREACHABLE)
    dispatcher = _recorder()
    # Act
    _dispatch(_cfg(["peer-a", _HERE]), oracle, dispatcher)
    # Assert
    assert dispatcher.seen == []


def test_start_raises_when_every_chain_candidate_is_rejected():
    # Arrange — no silent local start: an unusable placement is an ERROR.
    oracle = _oracle(peer_a=UNREACHABLE, peer_b=UNREACHABLE)
    dispatcher = _recorder()

    # Act
    def _do() -> None:
        _dispatch(_cfg(["peer-a", "peer-b"]), oracle, dispatcher)

    # Assert
    with pytest.raises(RuntimeError):
        _do()


def _rejected_chain_message(chain, oracle) -> str:
    """Message raised when ``chain`` has no usable candidate left."""
    try:
        _dispatch(_cfg(chain), oracle, _recorder())
    except RuntimeError as exc:
        return str(exc)
    return ""


def test_start_failure_names_the_rejected_chain_head():
    # Arrange
    oracle = _oracle(peer_a=UNREACHABLE, peer_b=UNREACHABLE)
    # Act
    msg = _rejected_chain_message(["peer-a", "peer-b"], oracle)
    # Assert
    assert "peer-a" in msg


def test_start_failure_names_the_rejected_chain_tail():
    # Arrange — the tail is the entry a head-only message would omit.
    oracle = _oracle(peer_a=UNREACHABLE, peer_b=UNREACHABLE)
    # Act
    msg = _rejected_chain_message(["peer-a", "peer-b"], oracle)
    # Assert
    assert "peer-b" in msg


def test_start_failure_reports_the_unreachable_reason():
    # Arrange — "down" and "typo" have different fixes, so the message must
    # separate them rather than lumping both under "unknown host".
    oracle = _oracle(peer_a=UNREACHABLE)
    # Act
    msg = _rejected_chain_message(["peer-a", "spartn-typo"], oracle)
    # Assert
    assert "UNREACHABLE" in msg


def test_start_failure_reports_the_unknown_host_reason():
    # Arrange
    oracle = _oracle(peer_a=UNREACHABLE)
    # Act
    msg = _rejected_chain_message(["peer-a", "spartn-typo"], oracle)
    # Assert
    assert "UNKNOWN HOST" in msg


def test_start_never_dispatches_when_the_whole_chain_is_rejected():
    # Arrange — negative safety: the raise happens BEFORE any ssh handoff
    # (the raise itself is asserted by a sibling test and only absorbed
    # here so this test's single assert stays the dispatch log).
    oracle = _oracle(peer_a=UNREACHABLE, peer_b=UNREACHABLE)
    dispatcher = _recorder()
    # Act
    try:
        _dispatch(_cfg(["peer-a", "peer-b"]), oracle, dispatcher)
    except RuntimeError:
        pass
    # Assert
    assert dispatcher.seen == []


# ---------------------------------------------------------------------------
# try_dispatch — a STRING placement is untouched by all of the above.
# ---------------------------------------------------------------------------


def test_start_with_a_string_host_never_consults_the_oracle():
    # Arrange — a string has nowhere to fall back to, so probing it could
    # only turn a working dispatch into a refusal.
    oracle = _oracle(peer_a=UNREACHABLE)
    dispatcher = _recorder()
    # Act
    _dispatch(_cfg("peer-a"), oracle, dispatcher)
    # Assert
    assert oracle.calls == []


def test_start_with_an_unreachable_string_host_still_dispatches():
    # Arrange — byte-identical to the pre-chain behaviour.
    oracle = _oracle(peer_a=UNREACHABLE)
    dispatcher = _recorder()
    # Act
    _dispatch(_cfg("peer-a"), oracle, dispatcher)
    # Assert
    assert dispatcher.seen == ["peer-a"]


def test_start_with_a_local_string_host_stays_local():
    # Arrange
    oracle = _oracle()
    dispatcher = _recorder()
    # Act
    out = _dispatch(_cfg(_HERE), oracle, dispatcher)
    # Assert
    assert out is False


def test_start_with_an_absent_host_stays_local():
    # Arrange
    oracle = _oracle()
    dispatcher = _recorder()
    # Act
    out = _dispatch(_cfg(""), oracle, dispatcher)
    # Assert
    assert out is False


def test_start_with_a_typo_string_host_still_raises():
    # Arrange — the single-bad-name path is unchanged.
    oracle = _oracle()
    dispatcher = _recorder()

    # Act
    def _do() -> None:
        _dispatch(_cfg("spartn-typo"), oracle, dispatcher)

    # Assert
    with pytest.raises(RuntimeError, match="peer-a"):
        _do()


# ---------------------------------------------------------------------------
# _bound_hosts — the singleton pin, now plural.
# ---------------------------------------------------------------------------


def test_bound_hosts_returns_the_whole_chain_in_priority_order():
    # Arrange
    config = _cfg(["peer-a", "peer-b", _HERE])
    # Act
    hosts = _bound_hosts(config)
    # Assert
    assert hosts == ["peer-a", "peer-b", _HERE]


def test_bound_hosts_wraps_a_string_pin_in_one_entry():
    # Arrange
    config = _cfg("peer-a")
    # Act
    hosts = _bound_hosts(config)
    # Assert
    assert hosts == ["peer-a"]


def test_bound_hosts_is_empty_for_a_multi_instance_config():
    # Arrange
    config = _cfg("", hosts=["peer-a", "peer-b"])
    # Act
    hosts = _bound_hosts(config)
    # Assert
    assert hosts == []


def test_bound_hosts_falls_back_to_the_v2_preferred_host():
    # Arrange
    config = _cfg("", sched_mode="singleton", pref="peer-a")
    # Act
    hosts = _bound_hosts(config)
    # Assert
    assert hosts == ["peer-a"]


# ---------------------------------------------------------------------------
# _resolve_singleton_skip — liveness across the WHOLE chain.
#
# The skip means "it is already running over there, defer". For a chain the
# agent may legitimately be on entry 2, so asking only about the head answers
# "not live", releases the pin, and starts a SECOND copy beside the running
# one.
# ---------------------------------------------------------------------------


def test_singleton_defers_when_the_agent_lives_on_a_chain_fallback():
    # Arrange — pinned to [peer-a, peer-b]; we are on this-host; the live
    # row is on peer-b, the FALLBACK entry.
    config = _cfg(["peer-a", "peer-b"])

    def _live_on_peer_b(name: str, host: str) -> bool:
        return host == "peer-b"

    # Act
    reason = _resolve_singleton_skip(
        config, _HERE, no_redispatch=False, liveness_oracle=_live_on_peer_b
    )
    # Assert — defer, do not start a duplicate here.
    assert reason is not None


def test_singleton_falls_through_when_no_chain_host_has_a_live_row():
    # Arrange — nothing running anywhere: the pin is stale, start locally.
    config = _cfg(["peer-a", "peer-b"])

    def _dead(name: str, host: str) -> bool:
        return False

    # Act
    reason = _resolve_singleton_skip(
        config, _HERE, no_redispatch=False, liveness_oracle=_dead
    )
    # Assert
    assert reason is None


def test_singleton_liveness_probe_stops_at_the_first_live_chain_host():
    # Arrange — a state.db read per host is not free; stop once satisfied.
    config = _cfg(["peer-a", "peer-b"])
    asked: list[str] = []

    def _live(name: str, host: str) -> bool:
        asked.append(host)
        return True

    # Act
    _resolve_singleton_skip(
        config, _HERE, no_redispatch=False, liveness_oracle=_live
    )
    # Assert
    assert asked == ["peer-a"]


def test_verdict_remote_skips_a_typo_head_for_the_next_chain_entry(
    tmp_path, env_save_restore
):
    # Arrange — chain led by a name that routes nowhere, real peer at entry
    # 2. Head-only reduction answered "not remote", so the resolver probed
    # the LOCAL tmux and a live remote agent rendered DEAD in listings.
    _write_peer_config(tmp_path, env_save_restore)
    from scitex_agent_container._lifecycle._verdict_remote import (
        _remote_peer_for_config,
    )

    # Act
    peer = _remote_peer_for_config(_cfg(["spartn-typo", "peer-a"]))
    # Assert
    assert peer == "peer-a"


def test_verdict_remote_still_resolves_a_plain_string_peer(
    tmp_path, env_save_restore
):
    # Arrange — the unchanged single-pin path.
    _write_peer_config(tmp_path, env_save_restore)
    from scitex_agent_container._lifecycle._verdict_remote import (
        _remote_peer_for_config,
    )

    # Act
    peer = _remote_peer_for_config(_cfg("peer-a"))
    # Assert
    assert peer == "peer-a"


def test_verdict_remote_reports_no_peer_for_a_local_placement(
    tmp_path, env_save_restore
):
    # Arrange
    _write_peer_config(tmp_path, env_save_restore)
    from scitex_agent_container._lifecycle._verdict_remote import (
        _remote_peer_for_config,
    )

    # Act
    peer = _remote_peer_for_config(_cfg(_HERE))
    # Assert
    assert peer is None


def test_singleton_liveness_probe_covers_every_chain_host_when_none_are_live():
    # Arrange
    config = _cfg(["peer-a", "peer-b"])
    asked: list[str] = []

    def _dead(name: str, host: str) -> bool:
        asked.append(host)
        return False

    # Act
    _resolve_singleton_skip(
        config, _HERE, no_redispatch=False, liveness_oracle=_dead
    )
    # Assert
    assert asked == ["peer-a", "peer-b"]
