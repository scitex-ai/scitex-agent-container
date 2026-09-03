"""Tests for ``_network/_reachability`` — the three-valued cross-host probe.

The properties that must never regress:

* the exit-code mapping is THREE-valued: UNKNOWN is never reachable and never
  unreachable, and a pass with nothing measured is 3, not 0;
* per-host resolution takes the alias from THE forwarder's own resolver
  (``_listen._node_channel_forwarders._resolve_ssh_peer`` — the same
  callable, not a look-alike), lists alias-less registry rows as UNKNOWN
  rather than dropping them, and never probes THIS host — even when this
  host has an alias AND a peer token, i.e. when nothing but the local skip
  stands between the probe and an ssh to itself;
* the probe's refusals are UNKNOWN with the file to fix named, and only a
  DISPATCHED leg can produce True or False;
* the ``local`` decision is carried into the row, so the alarm can skip the
  self row by the same decision instead of by name.

No mocks. The registry is a real ``hosts.yaml`` under a real ``$SCITEX_DIR``;
config.yaml is a real file (or a real absence) under
``SCITEX_AGENT_CONTAINER_CONFIG``; the ssh leg is a fake ``ssh`` binary on
PATH (``subprocess_shim``) so the real ``subprocess.run`` runs; peer tokens
are real files under a temp dir.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from scitex_agent_container._listen import _node_channel_forwarders as _fwd
from scitex_agent_container._listen.peer_tokens import write_peer_token
from scitex_agent_container._network import _reachability as _probe
from scitex_agent_container._network._reachability import (
    EXIT_ALL_REACHABLE,
    EXIT_NOTHING_MEASURABLE,
    EXIT_UNREACHABLE,
    TRANSPORT_NONE,
    TRANSPORT_SSH,
    HostReachability,
    ReachabilityReport,
    Target,
    exit_code_for,
    probe_target,
    read_report,
    resolve_targets,
    write_report,
)
from scitex_agent_container._network._ssh_curl import STATUS_MARKER

_HOSTS_YAML = textwrap.dedent(
    """\
    hosts:
      probe-box:
        kind: workstation
        ssh_alias: probe-box
        scitex_root: "~/.scitex"
      peer-with-alias:
        kind: workstation
        ssh_alias: peer-with-alias-ssh
        scitex_root: "~/.scitex"
      peer-without-alias:
        kind: workstation
        ssh_alias: null
        scitex_root: "~/.scitex"
    """
)


@pytest.fixture
def registry(tmp_path: Path, env_save_restore) -> Path:
    """A real hosts.yaml at the real resolved location ($SCITEX_DIR/dev/),
    and NO config.yaml — ``SCITEX_AGENT_CONTAINER_CONFIG`` names a file that
    does not exist (``host_config.load`` is missing-tolerant → no peers), so
    the operator's real config is never read. ``config()`` below writes one.
    """
    hosts_dir = tmp_path / "scitex" / "dev"
    hosts_dir.mkdir(parents=True)
    (hosts_dir / "hosts.yaml").write_text(_HOSTS_YAML)
    env_save_restore.set("SCITEX_DIR", str(tmp_path / "scitex"))
    env_save_restore.delete("SCITEX_DEV_HOSTS_YAML")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(tmp_path / "config.yaml"))
    return hosts_dir / "hosts.yaml"


@pytest.fixture
def config(tmp_path: Path, registry):
    """Write a real config.yaml at the path the ``registry`` fixture pinned."""

    def _write(peers_block: str) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text("host:\n  canonical: probe-box\npeers:\n" + peers_block)
        return path

    return _write


def _row(
    host: str, reachable, transport: str = TRANSPORT_SSH, *, local: bool = False
) -> HostReachability:
    return HostReachability(
        host=host,
        ssh_alias=None if transport == TRANSPORT_NONE else host,
        transport=transport,
        reachable=reachable,
        elapsed_ms=None if reachable is None else 12,
        error=None if reachable is True else "why",
        local=local,
    )


# ---------------------------------------------------------------------------
# exit codes — three values, never two
# ---------------------------------------------------------------------------


def test_exit_code_is_nothing_measurable_when_there_are_no_rows():
    # Arrange
    rows: list[HostReachability] = []
    # Act
    code = exit_code_for(rows)
    # Assert
    assert code == EXIT_NOTHING_MEASURABLE


def test_exit_code_is_nothing_measurable_when_every_row_is_unknown():
    # Arrange — THE rule: unknown is not reachable. A pass that could not
    # look must not read as a pass that looked and found nothing wrong.
    rows = [_row("a", None, TRANSPORT_NONE), _row("b", None)]
    # Act
    code = exit_code_for(rows)
    # Assert
    assert code == EXIT_NOTHING_MEASURABLE


def test_exit_code_is_zero_when_every_measured_row_is_reachable():
    # Arrange
    rows = [_row("a", True), _row("b", True)]
    # Act
    code = exit_code_for(rows)
    # Assert
    assert code == EXIT_ALL_REACHABLE


def test_exit_code_stays_zero_when_unknown_rows_sit_beside_reachable_ones():
    # Arrange — the measured hosts are a real answer; unknown ones are listed.
    rows = [_row("a", True), _row("me", None, TRANSPORT_NONE)]
    # Act
    code = exit_code_for(rows)
    # Assert
    assert code == EXIT_ALL_REACHABLE


def test_exit_code_is_one_when_any_row_is_unreachable():
    # Arrange — one bad host is never hidden behind good ones.
    rows = [_row("a", True), _row("b", False), _row("c", None)]
    # Act
    code = exit_code_for(rows)
    # Assert
    assert code == EXIT_UNREACHABLE


def test_exit_code_never_uses_two():
    # Arrange — 2 is Click's usage-error code and carries no domain meaning.
    combos = [[], [_row("a", None)], [_row("a", True)], [_row("a", False)]]
    # Act
    codes = {exit_code_for(rows) for rows in combos}
    # Assert
    assert 2 not in codes


# ---------------------------------------------------------------------------
# the fixed answer shape
# ---------------------------------------------------------------------------


def test_row_to_dict_carries_exactly_the_seven_declared_fields():
    # Arrange
    row = _row("a", True)
    # Act
    keys = set(row.to_dict())
    # Assert
    assert keys == {
        "host",
        "ssh_alias",
        "transport",
        "reachable",
        "elapsed_ms",
        "error",
        "local",
    }


def test_local_row_round_trips_its_flag_through_the_dict_shape():
    # Arrange — the alarm reads this flag back from --record'ed reports too.
    row = _row("me", None, TRANSPORT_NONE, local=True)
    # Act
    loaded = HostReachability.from_dict(row.to_dict())
    # Assert
    assert loaded.local is True


def test_a_local_row_cannot_claim_the_ssh_transport():
    # Arrange — this host has no leg; a local row that says "ssh" is a lie.
    kwargs = dict(host="me", ssh_alias="me", reachable=None, elapsed_ms=None)

    # Act
    def _build():
        return HostReachability(
            transport=TRANSPORT_SSH, error="why", local=True, **kwargs
        )

    # Assert
    with pytest.raises(ValueError):
        _build()


def test_row_with_no_transport_cannot_claim_a_measured_verdict():
    # Arrange — nothing was dispatched, so nothing can have been measured.
    kwargs = dict(host="a", ssh_alias=None, transport=TRANSPORT_NONE, elapsed_ms=None)

    # Act
    def _build():
        return HostReachability(reachable=True, error=None, **kwargs)

    # Assert
    with pytest.raises(ValueError):
        _build()


def test_unknown_row_must_state_its_reason():
    # Arrange — an UNKNOWN with no reason is the silence this shape forbids.
    kwargs = dict(host="a", ssh_alias=None, transport=TRANSPORT_NONE, elapsed_ms=None)

    # Act
    def _build():
        return HostReachability(reachable=None, error=None, **kwargs)

    # Assert
    with pytest.raises(ValueError):
        _build()


def test_report_round_trips_through_its_json_file(tmp_path: Path):
    # Arrange
    report = ReachabilityReport(
        probed_from="probe-box",
        port=7878,
        started_at_utc="2026-09-02T00:00:00+00:00",
        elapsed_ms=5,
        rows=(_row("a", False), _row("me", None, TRANSPORT_NONE, local=True)),
    )
    target = tmp_path / "a2a-reachability.json"
    # Act
    write_report(report, path=target)
    loaded = read_report(path=target)
    # Assert
    assert loaded == report


def test_read_report_is_none_when_nothing_was_recorded(tmp_path: Path):
    # Arrange
    target = tmp_path / "absent.json"
    # Act
    loaded = read_report(path=target)
    # Assert
    assert loaded is None


# ---------------------------------------------------------------------------
# per-host resolution — alias present / absent / local host
# ---------------------------------------------------------------------------


def _targets(**kwargs) -> dict[str, Target]:
    return {t.host: t for t in resolve_targets(local_names=set(), **kwargs)}


def test_registry_alias_is_the_ssh_target_when_config_is_absent(registry):
    # Arrange — THE measured gap: hosts with no config.yaml still know their
    # peers through the registry the forwarder now routes by.
    # Act
    targets = _targets()
    # Assert
    assert targets["peer-with-alias"].ssh_alias == "peer-with-alias-ssh"


def test_registry_row_without_alias_is_listed_with_no_ssh_target(registry):
    # Arrange — it must SURFACE (as unknown), not vanish from the report.
    # Act
    targets = _targets()
    # Assert
    assert targets["peer-without-alias"].ssh_alias is None


def test_config_peer_wins_over_the_registry_alias(config):
    # Arrange — config.yaml carries via:/env_preamble the registry cannot.
    config("  peer-with-alias:\n    ssh: cfg-route\n")
    # Act
    targets = _targets()
    # Assert
    assert targets["peer-with-alias"].ssh_alias == "cfg-route"


def test_glob_peer_templates_are_not_hosts(config):
    # Arrange — `spartan-*` is a template for ephemeral nodes, not a host.
    config("  spartan-*:\n    ssh: ''\n    via: [spartan]\n")
    # Act
    names = [t.host for t in resolve_targets(local_names=set())]
    # Assert
    assert "spartan-*" not in names


def test_local_host_is_flagged_local(registry):
    # Arrange
    local_names = {"probe-box"}
    # Act
    targets = {t.host: t for t in resolve_targets(local_names=local_names)}
    # Assert
    assert targets["probe-box"].local is True


def test_only_with_an_unknown_name_fails_loudly(registry):
    # Arrange — a typo that probed nothing would exit 3 and read as
    # "the fleet is unknown"; it must name the misspelling instead.
    only = ["peer-typo"]

    # Act
    def _resolve():
        return resolve_targets(local_names=set(), only=only)

    # Assert
    with pytest.raises(KeyError):
        _resolve()


# ---------------------------------------------------------------------------
# PARITY with the forwarder — the same resolver, not a look-alike
# ---------------------------------------------------------------------------
#
# Review round 2 of PR #1285: the first cut resolved aliases through the
# merged peer map while the forwarder on develop read the RAW config block,
# so the probe could report "reachable" for a peer a real send could not
# route. The fix is structural — the probe IMPORTS the forwarder's resolver —
# and these tests pin both the identity of the callable and, on a fixture
# where config.yaml and the registry disagree, the answer.


def test_probe_resolves_aliases_through_the_forwarders_own_callable():
    # Arrange — identity, not equivalence: a faithful re-implementation would
    # pass an answer-comparison today and drift tomorrow.
    forwarder_resolver = _fwd._resolve_ssh_peer
    # Act
    probe_resolver = _probe._resolve_ssh_peer
    # Assert
    assert probe_resolver is forwarder_resolver


def test_probe_alias_equals_the_forwarders_route_for_every_host(config):
    # Arrange — config overrides one registry alias; the registry supplies
    # another; a third row has none. All three must answer identically.
    config("  peer-with-alias:\n    ssh: cfg-route\n")
    hosts = ["peer-with-alias", "peer-without-alias", "probe-box"]
    # Act
    probe_answers = {t.host: t.ssh_alias for t in resolve_targets(local_names=set())}
    forwarder_answers = {h: _fwd._resolve_ssh_peer(h).ssh_target for h in hosts}
    # Assert
    assert {h: probe_answers[h] for h in hosts} == forwarder_answers


def test_unroutable_host_carries_the_forwarders_own_refusal_reason(registry):
    # Arrange — the UNKNOWN row must say what a real send would 502 with.
    target = _targets()["peer-without-alias"]
    expected_reason = _fwd._resolve_ssh_peer("peer-without-alias").reason
    # Act
    row = probe_target(target)
    # Assert
    assert expected_reason and expected_reason in (row.error or "")


# ---------------------------------------------------------------------------
# probe_target — refusals are UNKNOWN; only a dispatched leg measures
# ---------------------------------------------------------------------------


@pytest.fixture
def tokens_dir(tmp_path: Path) -> Path:
    return tmp_path / "peer-tokens"


def _pin_ssh_env(tmp_path, env_save_restore) -> None:
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")


_PEER = Target(host="peer-with-alias", ssh_alias="peer-with-alias-ssh", local=False)
_HEALTHY = f'{{"ok": true, "service": "sac-listen", "v": 1}}\n{STATUS_MARKER}200\n'


def test_local_host_is_unknown_and_never_dispatched(
    tmp_path, env_save_restore, subprocess_shim, tokens_dir
):
    # Arrange — this host has an alias AND a peer token, so NOTHING but the
    # local skip stands between the probe and an ssh to itself; the ssh on
    # PATH would answer as a healthy listen and record the dispatch. (Review
    # round 2: without the token the row went unknown for the wrong reason
    # and the test stayed green with the skip removed.)
    _pin_ssh_env(tmp_path, env_save_restore)
    write_peer_token(peer_host="probe-box", token="t0k3n", tokens_dir=tokens_dir)
    subprocess_shim.install("ssh", exit=0, stdout=_HEALTHY)
    target = Target(host="probe-box", ssh_alias="probe-box", local=True)
    # Act
    row = probe_target(target, tokens_dir=tokens_dir)
    # Assert — unknown over NO transport, and the leg was never run.
    assert (row.reachable, row.transport, subprocess_shim.call_count("ssh")) == (
        None,
        TRANSPORT_NONE,
        0,
    )


def test_local_host_row_carries_the_local_flag(tokens_dir):
    # Arrange — the alarm skips the self row by this flag, not by name.
    target = Target(host="probe-box", ssh_alias="probe-box", local=True)
    # Act
    row = probe_target(target, tokens_dir=tokens_dir)
    # Assert
    assert row.local is True


def test_a_peer_row_does_not_carry_the_local_flag(tokens_dir):
    # Arrange
    # Act
    row = probe_target(_PEER, tokens_dir=tokens_dir)
    # Assert
    assert row.local is False


def test_host_without_alias_is_unknown_over_no_transport(tokens_dir):
    # Arrange
    target = Target(host="peer-without-alias", ssh_alias=None, local=False)
    # Act
    row = probe_target(target, tokens_dir=tokens_dir)
    # Assert
    assert (row.reachable, row.transport) == (None, TRANSPORT_NONE)


def test_host_without_peer_token_is_unknown_and_names_the_fix(tokens_dir):
    # Arrange — the forwarder refuses to send without one; so does the probe.
    # Act
    row = probe_target(_PEER, tokens_dir=tokens_dir)
    # Assert
    assert row.reachable is None and "sac host add-peer" in (row.error or "")


def test_failed_ssh_leg_is_unreachable_not_unknown(
    tmp_path, env_save_restore, subprocess_shim, tokens_dir
):
    # Arrange — a dispatched leg that fails is a MEASURED False.
    _pin_ssh_env(tmp_path, env_save_restore)
    write_peer_token(peer_host=_PEER.host, token="t0k3n", tokens_dir=tokens_dir)
    subprocess_shim.install("ssh", exit=255, stderr="ssh: connect: refused")
    # Act
    row = probe_target(_PEER, tokens_dir=tokens_dir)
    # Assert
    assert row.reachable is False


def test_failed_ssh_leg_names_the_alias_it_dialled(
    tmp_path, env_save_restore, subprocess_shim, tokens_dir
):
    # Arrange
    _pin_ssh_env(tmp_path, env_save_restore)
    write_peer_token(peer_host=_PEER.host, token="t0k3n", tokens_dir=tokens_dir)
    subprocess_shim.install("ssh", exit=255, stderr="ssh: connect: refused")
    # Act
    row = probe_target(_PEER, tokens_dir=tokens_dir)
    # Assert
    assert "ssh://peer-with-alias-ssh" in (row.error or "")


def test_non_200_answer_is_unreachable(
    tmp_path, env_save_restore, subprocess_shim, tokens_dir
):
    # Arrange — something answered on the port, but not a healthy listen.
    _pin_ssh_env(tmp_path, env_save_restore)
    write_peer_token(peer_host=_PEER.host, token="t0k3n", tokens_dir=tokens_dir)
    subprocess_shim.install("ssh", exit=0, stdout=f"not found\n{STATUS_MARKER}404\n")
    # Act
    row = probe_target(_PEER, tokens_dir=tokens_dir)
    # Assert
    assert row.reachable is False


def test_a_200_that_is_not_sac_listen_is_unreachable(
    tmp_path, env_save_restore, subprocess_shim, tokens_dir
):
    # Arrange — a stray service on 7878 must not pass as the listen.
    _pin_ssh_env(tmp_path, env_save_restore)
    write_peer_token(peer_host=_PEER.host, token="t0k3n", tokens_dir=tokens_dir)
    subprocess_shim.install(
        "ssh", exit=0, stdout=f"<html>hi</html>\n{STATUS_MARKER}200\n"
    )
    # Act
    row = probe_target(_PEER, tokens_dir=tokens_dir)
    # Assert
    assert row.reachable is False


def test_a_sac_listen_health_answer_is_reachable(
    tmp_path, env_save_restore, subprocess_shim, tokens_dir
):
    # Arrange
    _pin_ssh_env(tmp_path, env_save_restore)
    write_peer_token(peer_host=_PEER.host, token="t0k3n", tokens_dir=tokens_dir)
    subprocess_shim.install("ssh", exit=0, stdout=_HEALTHY)
    # Act
    row = probe_target(_PEER, tokens_dir=tokens_dir)
    # Assert
    assert row.reachable is True


def test_a_reachable_row_records_how_long_the_leg_took(
    tmp_path, env_save_restore, subprocess_shim, tokens_dir
):
    # Arrange
    _pin_ssh_env(tmp_path, env_save_restore)
    write_peer_token(peer_host=_PEER.host, token="t0k3n", tokens_dir=tokens_dir)
    subprocess_shim.install("ssh", exit=0, stdout=_HEALTHY)
    # Act
    row = probe_target(_PEER, tokens_dir=tokens_dir)
    # Assert
    assert isinstance(row.elapsed_ms, int) and row.elapsed_ms >= 0
