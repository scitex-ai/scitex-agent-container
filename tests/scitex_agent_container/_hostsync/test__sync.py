"""Tests for the sync orchestration: probe -> decide -> guard -> apply -> VERIFY.

No mocks. ``_Remote`` below is a REAL callable standing in for the two
external systems this module talks to (the peer over ssh, and the GitHub
API via ``gh``). It answers like they do and RECORDS what it was asked,
so a test can assert the thing that actually matters:

    the fast-forward was never even attempted.

That is the assertion behind every refusal test here. A refusal that
still mutated would pass a naive "outcome == REFUSED" check while
destroying the peer.

The other load-bearing test is
``test_symbol_probe_mismatch_fails_instead_of_reporting_success``: a
sync whose result it cannot substantiate must report FAILED. "I
verified" is a claim like any other.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import subprocess

from scitex_agent_container._hostsync import (
    Outcome,
    check_peer,
    exit_code_for,
    sync_peer,
    syncable_peers,
)
from scitex_agent_container._state.host_config import (
    Config,
    HostBlock,
    PeersMap,
    PeerSpec,
)

_REPO = "/data/gpfs/projects/punim0264/ywatanabe/scitex-agent-container"
_MODULE = f"{_REPO}/src/scitex_agent_container/__init__.py"
_OLD = "1111111111111111111111111111111111111111"
_NEW = "2222222222222222222222222222222222222222"
_SYMBOL = "['agent_name', 'preferred']"

_IDLE_RUNNERS = (
    '{"total_count":1,"runners":[{"name":"spartan-cpu-01","busy":false,'
    '"labels":[{"name":"spartan-cpu"}]}]}'
)
_BUSY_RUNNERS = (
    '{"total_count":1,"runners":[{"name":"spartan-cpu-01","busy":true,'
    '"labels":[{"name":"spartan-cpu"}]}]}'
)
_NO_RUNS = '{"total_count":0}'


def _peers(*names: str) -> dict[str, PeerSpec]:
    return {n: PeerSpec(name=n, ssh=f"user@{n}") for n in names}


def _probe(
    *,
    head: str,
    target_sha: str,
    ahead: int = 0,
    behind: int = 0,
    dirty: tuple[str, ...] = (),
    ahead_commits: tuple[str, ...] = (),
    symbol: str = _SYMBOL,
    module: str = _MODULE,
) -> str:
    lines = [
        f"SAC_SYNC module={module}",
        f"SAC_SYNC repo={_REPO}",
        "SAC_SYNC target=origin/develop",
        f"SAC_SYNC target_sha={target_sha}",
        f"SAC_SYNC head={head}",
        f"SAC_SYNC ahead={ahead}",
        f"SAC_SYNC behind={behind}",
    ]
    lines += [f"SAC_SYNC ahead_commit={c}" for c in ahead_commits]
    lines += [f"SAC_SYNC dirty={d}" for d in dirty]
    lines += [f"SAC_SYNC symbol={symbol}", "SAC_SYNC end"]
    return "\n".join(lines) + "\n"


class _Remote:
    """Real callable answering as the peer + GitHub would. Records calls."""

    def __init__(
        self,
        *probes: str,
        runners: str = _IDLE_RUNNERS,
        runs: str = _NO_RUNS,
        merge_exit: int = 0,
    ) -> None:
        self._probes = list(probes)
        self._runners = runners
        self._runs = runs
        self._merge_exit = merge_exit
        self.seen: list[str] = []

    def __call__(self, argv, *_a, **_kw):
        joined = " ".join(argv)
        self.seen.append(joined)
        if argv and argv[0] == "gh":
            payload = self._runners if "actions/runners" in joined else self._runs
            return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")
        if "merge --ff-only" in joined:
            return subprocess.CompletedProcess(
                argv,
                self._merge_exit,
                stdout="Fast-forward\n" if not self._merge_exit else "",
                stderr="" if not self._merge_exit else "fatal: not possible\n",
            )
        out = self._probes.pop(0) if self._probes else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    @property
    def applied(self) -> bool:
        """Did we ever actually try to move the remote tree?"""
        return any("merge --ff-only" in call for call in self.seen)


# ---------------------------------------------------------------------------
# the happy path — and its verification
# ---------------------------------------------------------------------------


def test_behind_and_idle_peer_is_synced():
    # Arrange — stale peer; after the merge it sits on the target sha.
    remote = _Remote(
        _probe(head=_OLD, target_sha=_NEW, behind=3),
        _probe(head=_NEW, target_sha=_NEW),
    )
    # Act
    result = sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert result.outcome is Outcome.SYNCED


def test_sync_lands_the_sha_it_aimed_at():
    # Arrange
    remote = _Remote(
        _probe(head=_OLD, target_sha=_NEW, behind=3),
        _probe(head=_NEW, target_sha=_NEW),
    )
    # Act
    result = sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert result.after.head == _NEW


def test_current_peer_is_a_reported_no_op():
    # Arrange — nothing to do, but it must still say so.
    remote = _Remote(_probe(head=_NEW, target_sha=_NEW))
    # Act
    result = sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert result.outcome is Outcome.CURRENT


def test_current_peer_is_never_mutated():
    # Arrange
    remote = _Remote(_probe(head=_NEW, target_sha=_NEW))
    # Act
    sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert remote.applied is False


# ---------------------------------------------------------------------------
# verification: a success we cannot substantiate is a FAILURE
# ---------------------------------------------------------------------------


def test_symbol_probe_mismatch_fails_instead_of_reporting_success():
    # Arrange — the tree moved, but the symbol does not import out of it.
    remote = _Remote(
        _probe(head=_OLD, target_sha=_NEW, behind=3),
        _probe(head=_NEW, target_sha=_NEW, symbol=""),
    )
    # Act
    result = sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert result.outcome is Outcome.FAILED


def test_module_loaded_outside_the_synced_tree_fails():
    # Arrange — the classic invisible staleness: the checkout is at the
    # right sha, but the interpreter loads sac from a wheel elsewhere.
    remote = _Remote(
        _probe(head=_OLD, target_sha=_NEW, behind=3),
        _probe(
            head=_NEW,
            target_sha=_NEW,
            module="/venv/lib/python3.11/site-packages/scitex_agent_container/__init__.py",
        ),
    )
    # Act
    result = sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert result.outcome is Outcome.FAILED


def test_head_not_at_target_after_merge_fails():
    # Arrange — the merge claimed success but HEAD did not move.
    remote = _Remote(
        _probe(head=_OLD, target_sha=_NEW, behind=3),
        _probe(head=_OLD, target_sha=_NEW, behind=3),
    )
    # Act
    result = sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert result.outcome is Outcome.FAILED


def test_unverifiable_peer_after_merge_fails():
    # Arrange — the post-sync probe cannot read the peer back at all.
    remote = _Remote(_probe(head=_OLD, target_sha=_NEW, behind=3), "")
    # Act
    result = sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert — cannot vouch for it, so it is not a success.
    assert result.outcome is Outcome.FAILED


def test_failed_merge_is_reported_as_failed():
    # Arrange
    remote = _Remote(_probe(head=_OLD, target_sha=_NEW, behind=3), merge_exit=128)
    # Act
    result = sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert result.outcome is Outcome.FAILED


# ---------------------------------------------------------------------------
# refusals — and the proof that nothing was touched
# ---------------------------------------------------------------------------


def test_ahead_peer_is_refused():
    # Arrange — the remote holds commits the centre does not.
    remote = _Remote(
        _probe(head=_OLD, target_sha=_NEW, ahead=2, ahead_commits=("abc123 wip",))
    )
    # Act
    result = sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert result.outcome is Outcome.REFUSED


def test_ahead_peer_is_never_fast_forwarded():
    # Arrange
    remote = _Remote(
        _probe(head=_OLD, target_sha=_NEW, ahead=2, ahead_commits=("abc123 wip",))
    )
    # Act
    sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert — the commits are still there; we only shouted about them.
    assert remote.applied is False


def test_diverged_peer_is_never_fast_forwarded():
    # Arrange
    remote = _Remote(
        _probe(
            head=_OLD,
            target_sha=_NEW,
            ahead=1,
            behind=4,
            ahead_commits=("abc123 wip",),
        )
    )
    # Act
    sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert remote.applied is False


def test_dirty_peer_is_refused():
    # Arrange
    remote = _Remote(
        _probe(head=_OLD, target_sha=_NEW, behind=2, dirty=(" M src/foo.py",))
    )
    # Act
    result = sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert result.outcome is Outcome.REFUSED


def test_dirty_peer_is_never_fast_forwarded():
    # Arrange — sac never stashes and never discards work it did not write.
    remote = _Remote(
        _probe(head=_OLD, target_sha=_NEW, behind=2, dirty=(" M src/foo.py",))
    )
    # Act
    sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert remote.applied is False


def test_unreachable_peer_is_undetermined():
    # Arrange
    remote = _Remote("")
    # Act
    result = sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert result.outcome is Outcome.UNDETERMINED


def test_unreachable_peer_is_never_fast_forwarded():
    # Arrange — an unknown peer is not a clean peer.
    remote = _Remote("")
    # Act
    sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert remote.applied is False


# ---------------------------------------------------------------------------
# the CI guard — Spartan's checkout IS the runner's audit workspace
# ---------------------------------------------------------------------------


def test_busy_ci_refuses_the_sync():
    # Arrange — a job is running in this very checkout.
    remote = _Remote(
        _probe(head=_OLD, target_sha=_NEW, behind=3), runners=_BUSY_RUNNERS
    )
    # Act
    result = sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert result.outcome is Outcome.REFUSED


def test_busy_ci_peer_is_never_fast_forwarded():
    # Arrange — a merge landing under a live audit is the 2026-07-14 bug.
    remote = _Remote(
        _probe(head=_OLD, target_sha=_NEW, behind=3), runners=_BUSY_RUNNERS
    )
    # Act
    sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert remote.applied is False


def test_unknown_ci_state_is_undetermined():
    # Arrange — gh is missing, so CI state cannot be read.
    class _NoGh(_Remote):
        def __call__(self, argv, *a, **kw):
            if argv and argv[0] == "gh":
                raise FileNotFoundError("gh")
            return super().__call__(argv, *a, **kw)

    remote = _NoGh(_probe(head=_OLD, target_sha=_NEW, behind=3))
    # Act
    result = sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert result.outcome is Outcome.UNDETERMINED


def test_unknown_ci_state_blocks_the_fast_forward():
    # Arrange
    class _NoGh(_Remote):
        def __call__(self, argv, *a, **kw):
            if argv and argv[0] == "gh":
                raise FileNotFoundError("gh")
            return super().__call__(argv, *a, **kw)

    remote = _NoGh(_probe(head=_OLD, target_sha=_NEW, behind=3))
    # Act
    sync_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert remote.applied is False


def test_force_overrides_a_busy_ci_guard():
    # Arrange
    remote = _Remote(
        _probe(head=_OLD, target_sha=_NEW, behind=3),
        _probe(head=_NEW, target_sha=_NEW),
        runners=_BUSY_RUNNERS,
    )
    # Act
    result = sync_peer("spartan", _peers("spartan"), force=True, runner=remote)
    # Assert
    assert result.outcome is Outcome.SYNCED


def test_force_records_what_it_overrode():
    # Arrange — an override must never be silent.
    remote = _Remote(
        _probe(head=_OLD, target_sha=_NEW, behind=3),
        _probe(head=_NEW, target_sha=_NEW),
        runners=_BUSY_RUNNERS,
    )
    # Act
    result = sync_peer("spartan", _peers("spartan"), force=True, runner=remote)
    # Assert
    assert any("OVERRODE the CI guard" in note for note in result.notes)


# ---------------------------------------------------------------------------
# --check is READ-ONLY
# ---------------------------------------------------------------------------


def test_check_never_mutates_a_drifted_peer():
    # Arrange — badly stale, but --check must not touch it.
    remote = _Remote(_probe(head=_OLD, target_sha=_NEW, behind=9))
    # Act
    check_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert remote.applied is False


def test_check_reports_drift_on_a_stale_peer():
    # Arrange
    remote = _Remote(_probe(head=_OLD, target_sha=_NEW, behind=9))
    # Act
    result = check_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert result.outcome is Outcome.DRIFTED


def test_check_reports_current_on_a_matching_peer():
    # Arrange
    remote = _Remote(_probe(head=_NEW, target_sha=_NEW))
    # Act
    result = check_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert result.outcome is Outcome.CURRENT


def test_check_never_calls_the_ci_api():
    # Arrange — a read-only check has no reason to touch GitHub.
    remote = _Remote(_probe(head=_OLD, target_sha=_NEW, behind=9))
    # Act
    check_peer("spartan", _peers("spartan"), runner=remote)
    # Assert
    assert not any(call.startswith("gh ") for call in remote.seen)


# ---------------------------------------------------------------------------
# exit codes + peer selection
# ---------------------------------------------------------------------------


def test_drift_exits_non_zero_for_cron():
    # Arrange
    # Act
    code = exit_code_for([Outcome.CURRENT, Outcome.DRIFTED])
    # Assert
    assert code == 1


def test_worst_outcome_wins_across_peers():
    # Arrange — one bad peer must not hide behind good ones.
    # Act
    code = exit_code_for([Outcome.SYNCED, Outcome.DRIFTED, Outcome.FAILED])
    # Assert
    assert code == 2


def test_clean_fleet_exits_zero():
    # Arrange
    # Act
    code = exit_code_for([Outcome.CURRENT, Outcome.SYNCED])
    # Assert
    assert code == 0


def test_all_skips_glob_pattern_peers(env_save_restore):
    # Arrange — `spartan-*` is a template for ephemeral compute nodes.
    env_save_restore.set("SAC_HOST", "ywata-note-win")
    peers = PeersMap()
    for name in ("spartan", "spartan-*", "mba"):
        peers[name] = PeerSpec(name=name, ssh=name)
    cfg = Config(host=HostBlock(), peers=peers)
    # Act
    targets = syncable_peers(cfg)
    # Assert
    assert targets == ["mba", "spartan"]


def test_all_skips_the_centre_itself(env_save_restore):
    # Arrange — you do not sync the brain to itself.
    env_save_restore.set("SAC_HOST", "ywata-note-win")
    peers = PeersMap()
    for name in ("spartan", "ywata-note-win"):
        peers[name] = PeerSpec(name=name, ssh=name)
    cfg = Config(host=HostBlock(), peers=peers)
    # Act
    targets = syncable_peers(cfg)
    # Assert
    assert targets == ["spartan"]
