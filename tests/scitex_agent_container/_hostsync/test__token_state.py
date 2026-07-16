"""Tests for the token-state verdicts (ADR-0021 PR-B, the READ half).

PA-306: no ``unittest.mock``. Classification is driven with REAL
:class:`RemoteTokenRead` values and real digests computed by the real
:func:`sha12`; ``check_tokens_peer`` runs over the injectable runner seam
with real ``CompletedProcess`` replies and real token files on disk.

The verdicts are three-state honest, and the tests pin the two properties
that make them worth having: UNDETERMINED never reads as clean, and an
ambiguous peer (several DIFFERENT listen tokens) is UNDETERMINED rather
than a guess. Each test: AAA (TQ002), one assertion (TQ007),
behaviour-shaped name (TQ003).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scitex_agent_container._hostsync._push_tokens_io import RemoteTokenRead
from scitex_agent_container._hostsync._token_state import (
    TokenVerdict,
    check_tokens_peer,
    classify_token_state,
    mint_bearer,
    peer_listen_token_rel_paths,
    sha12,
    stable_listen_token_name,
)

_MASTER = "master-x"
_MASTER_BEARER = "master-bearer-value"
_PEER_BEARER = "peer-bearer-value"


def _full(value: str) -> str:
    """The FULL sha256 a peer would report for ``value``."""
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def _remote(
    *,
    hostname: str = "spartan-login1",
    listen: dict[str, str] | None = None,
    peer: dict[str, str] | None = None,
) -> RemoteTokenRead:
    return RemoteTokenRead(
        ok=True,
        hostname=hostname,
        listen_tokens=(
            listen
            if listen is not None
            else {"listen-spartan-login1.token": _full(_PEER_BEARER)}
        ),
        peer_tokens=(
            peer if peer is not None else {f"{_MASTER}.token": _full(_MASTER_BEARER)}
        ),
    )


def _classify(
    remote, *, master_holds: str = _PEER_BEARER, master_bearer=_MASTER_BEARER
):
    return classify_token_state(
        "spartan",
        master_name=_MASTER,
        master_bearer=master_bearer,
        master_holds_peer=master_holds,
        remote=remote,
    )


# ---------------------------------------------------------------------------
# sha12 — the only token shape that may ever be printed
# ---------------------------------------------------------------------------


def test_sha12_is_twelve_hex_chars():
    # Arrange
    # Act
    digest = sha12("anything")
    # Assert
    assert len(digest) == 12


def test_sha12_never_returns_the_value():
    # Arrange
    secret = "super-secret-bearer"
    # Act
    digest = sha12(secret)
    # Assert
    assert secret not in digest


def test_sha12_of_empty_is_empty_not_a_digest():
    # Arrange — digesting "" would yield a real-looking constant that
    # silently MATCHES every other missing token.
    # Act
    digest = sha12("")
    # Assert
    assert digest == ""


def test_mint_bearer_is_not_reproducible():
    # Arrange
    # Act
    first, second = mint_bearer(), mint_bearer()
    # Assert
    assert first != second


def test_stable_token_name_is_keyed_on_the_canonical_name():
    # Arrange — the whole FQDN fix in one string.
    # Act
    name = stable_listen_token_name("spartan")
    # Assert
    assert name == "listen-spartan.token"


def test_rel_paths_seed_both_the_hostname_and_stable_paths():
    # Arrange — a rotation must make the two indistinguishable.
    # Act
    paths = peer_listen_token_rel_paths("spartan", "spartan-login1")
    # Assert
    assert paths == [
        "tokens/listen-spartan-login1.token",
        "tokens/listen-spartan.token",
    ]


def test_rel_paths_dedupe_when_hostname_is_the_canonical_name():
    # Arrange — on mba the two paths are one; writing it twice is noise.
    # Act
    paths = peer_listen_token_rel_paths("mba", "mba")
    # Assert
    assert paths == ["tokens/listen-mba.token"]


# ---------------------------------------------------------------------------
# classify_token_state — the verdicts
# ---------------------------------------------------------------------------


def test_classify_both_legs_matching_as_current():
    # Arrange
    # Act
    result = _classify(_remote())
    # Assert
    assert result.verdict is TokenVerdict.TOKENS_CURRENT


def test_classify_failed_read_as_undetermined():
    # Arrange — "I could not look" must never read as anything else.
    remote = RemoteTokenRead(ok=False, detail="ssh exit 255")
    # Act
    result = _classify(remote)
    # Assert
    assert result.verdict is TokenVerdict.UNDETERMINED


def test_classify_outbound_mismatch_as_drifted():
    # Arrange — the peer holds a STALE copy of the master's bearer.
    remote = _remote(peer={f"{_MASTER}.token": _full("stale-master-bearer")})
    # Act
    result = _classify(remote)
    # Assert
    assert result.verdict is TokenVerdict.TOKENS_DRIFTED


def test_classify_outbound_mismatch_names_the_broken_direction():
    # Arrange — a verdict nobody can act on is not a verdict.
    remote = _remote(peer={f"{_MASTER}.token": _full("stale-master-bearer")})
    # Act
    result = _classify(remote)
    # Assert
    assert "OUTBOUND" in result.detail


def test_classify_inbound_mismatch_as_drifted():
    # Arrange — the master's copy of the peer's bearer is stale.
    # Act
    result = _classify(_remote(), master_holds="stale-peer-bearer")
    # Assert
    assert result.verdict is TokenVerdict.TOKENS_DRIFTED


def test_classify_inbound_mismatch_names_the_broken_direction():
    # Arrange
    # Act
    result = _classify(_remote(), master_holds="stale-peer-bearer")
    # Assert
    assert "INBOUND" in result.detail


def test_classify_missing_peer_copy_of_master_bearer_as_absent():
    # Arrange — the peer never got the master's bearer.
    # Act
    result = _classify(_remote(peer={}))
    # Assert
    assert result.verdict is TokenVerdict.TOKENS_ABSENT


def test_classify_peer_with_no_listen_token_as_absent():
    # Arrange — the peer's listen has never run.
    # Act
    result = _classify(_remote(listen={}), master_holds="")
    # Assert
    assert result.verdict is TokenVerdict.TOKENS_ABSENT


def test_classify_missing_master_copy_as_absent():
    # Arrange — the master never registered the peer's bearer.
    # Act
    result = _classify(_remote(), master_holds="")
    # Assert
    assert result.verdict is TokenVerdict.TOKENS_ABSENT


def test_classify_master_without_a_bearer_as_undetermined():
    # Arrange — with no bearer of its own the master cannot say what the
    # peer SHOULD hold; that is unknown, not drift.
    # Act
    result = _classify(_remote(), master_bearer="")
    # Assert
    assert result.verdict is TokenVerdict.UNDETERMINED


def test_classify_disagreeing_listen_tokens_as_undetermined():
    # Arrange — THE FQDN hazard: two login nodes, two different bearers.
    # Which one the running listen serves is not knowable from here.
    remote = _remote(
        listen={
            "listen-spartan-login1.token": _full(_PEER_BEARER),
            "listen-spartan-login2.token": _full("other-bearer"),
        }
    )
    # Act
    result = _classify(remote)
    # Assert
    assert result.verdict is TokenVerdict.UNDETERMINED


def test_disagreeing_listen_tokens_never_pick_the_flattering_match():
    # Arrange — one of the two DOES match the master's copy. Choosing it
    # would be an answer selected to agree with us.
    remote = _remote(
        listen={
            "listen-spartan-login1.token": _full(_PEER_BEARER),
            "listen-spartan-login2.token": _full("other-bearer"),
        }
    )
    # Act
    result = _classify(remote)
    # Assert
    assert result.verdict is not TokenVerdict.TOKENS_CURRENT


def test_classify_agreeing_listen_tokens_is_decidable():
    # Arrange — what a rotation leaves behind: same value at both paths,
    # so whichever the listen read, we know what it holds.
    remote = _remote(
        listen={
            "listen-spartan-login1.token": _full(_PEER_BEARER),
            "listen-spartan.token": _full(_PEER_BEARER),
        }
    )
    # Act
    result = _classify(remote)
    # Assert
    assert result.verdict is TokenVerdict.TOKENS_CURRENT


def test_classify_undigestable_token_as_undetermined():
    # Arrange — a peer with no sha256 tool reported an empty digest.
    remote = _remote(listen={"listen-spartan-login1.token": ""})
    # Act
    result = _classify(remote)
    # Assert
    assert result.verdict is TokenVerdict.UNDETERMINED


def test_classify_warns_when_this_node_has_no_token_file():
    # Arrange — the ssh landed on login2, which has no token: a listen
    # restarted HERE would mint a fresh bearer and desync the master.
    remote = _remote(
        hostname="spartan-login2",
        listen={"listen-spartan-login1.token": _full(_PEER_BEARER)},
    )
    # Act
    result = _classify(remote)
    # Assert
    assert "would MINT a fresh bearer" in result.detail


def test_current_verdict_exits_zero():
    # Arrange
    # Act
    result = _classify(_remote())
    # Assert
    assert result.exit_code == 0


def test_drifted_verdict_exits_one():
    # Arrange
    # Act
    result = _classify(_remote(), master_holds="stale-peer-bearer")
    # Assert — an alarm that exits 0 on drift is not an alarm.
    assert result.exit_code == 1


def test_undetermined_verdict_exits_two():
    # Arrange
    remote = RemoteTokenRead(ok=False, detail="ssh exit 255")
    # Act
    result = _classify(remote)
    # Assert
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# SECRECY — the rail the whole module exists behind
# ---------------------------------------------------------------------------


def test_classified_result_never_carries_a_token_value():
    # Arrange — real values on both sides of the comparison.
    # Act
    result = _classify(_remote())
    rendered = repr(result.to_dict())
    # Assert
    assert _MASTER_BEARER not in rendered and _PEER_BEARER not in rendered


def test_drifted_detail_never_leaks_the_stale_value():
    # Arrange — a failure path is where secrets usually escape.
    # Act
    result = _classify(_remote(), master_holds="stale-peer-bearer-value")
    # Assert
    assert "stale-peer-bearer-value" not in result.detail


def test_result_digests_are_twelve_chars_not_full_sha():
    # Arrange — a full sha256 is not a secret, but the contract is sha12.
    # Act
    result = _classify(_remote())
    # Assert
    assert len(result.master_bearer_sha12) == 12


# ---------------------------------------------------------------------------
# check_tokens_peer — real files, injectable runner
# ---------------------------------------------------------------------------


@pytest.fixture
def master_cfg(tmp_path: Path):
    """A real master config (real file, real canonical name)."""
    from scitex_agent_container._state.host_config import load

    p = tmp_path / "config.yaml"
    p.write_text(f"host:\n  canonical: {_MASTER}\npeers:\n  spartan:\n    ssh: sp\n")
    return load(p)


@pytest.fixture
def master_token(tmp_path: Path) -> Path:
    """The master's OWN listen bearer, as a real 0600 file."""
    p = tmp_path / "listen-master.token"
    p.write_text(_MASTER_BEARER)
    p.chmod(0o600)
    return p


@pytest.fixture
def peer_tokens_dir(tmp_path: Path) -> Path:
    """The master's peer-tokens/ registry, as real files."""
    d = tmp_path / "peer-tokens"
    d.mkdir()
    (d / "spartan.token").write_text(_PEER_BEARER)
    return d


class _ScriptedRunner:
    def __init__(self, *results):
        self._results = list(results)
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _read_block(listen_digest: str, peer_digest: str) -> str:
    return (
        "SAC_PUSHTOK hostname=spartan-login1\n"
        f"SAC_PUSHTOK listen=listen-spartan-login1.token {listen_digest}\n"
        f"SAC_PUSHTOK peer={_MASTER}.token {peer_digest}\n"
        "SAC_PUSHTOK end\n"
    )


def _proc(stdout: str = "", rc: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=rc, stdout=stdout, stderr=stderr
    )


def test_check_tokens_on_matching_peer_exits_zero(
    master_cfg, master_token, peer_tokens_dir
):
    # Arrange — the peer reports digests matching the master's real files.
    runner = _ScriptedRunner(
        _proc(stdout=_read_block(_full(_PEER_BEARER), _full(_MASTER_BEARER)))
    )
    # Act
    result = check_tokens_peer(
        "spartan",
        master_cfg,
        runner=runner,
        tokens_dir=peer_tokens_dir,
        master_token_path=master_token,
    )
    # Assert
    assert result.exit_code == 0


def test_check_tokens_on_drifted_peer_exits_one(
    master_cfg, master_token, peer_tokens_dir
):
    # Arrange — the peer holds a stale copy of the master's bearer.
    runner = _ScriptedRunner(
        _proc(stdout=_read_block(_full(_PEER_BEARER), _full("stale")))
    )
    # Act
    result = check_tokens_peer(
        "spartan",
        master_cfg,
        runner=runner,
        tokens_dir=peer_tokens_dir,
        master_token_path=master_token,
    )
    # Assert
    assert result.exit_code == 1


def test_check_tokens_never_dispatches_a_write(
    master_cfg, master_token, peer_tokens_dir
):
    # Arrange — a drifted peer under --check must stay untouched.
    runner = _ScriptedRunner(
        _proc(stdout=_read_block(_full(_PEER_BEARER), _full("stale")))
    )
    # Act
    check_tokens_peer(
        "spartan",
        master_cfg,
        runner=runner,
        tokens_dir=peer_tokens_dir,
        master_token_path=master_token,
    )
    # Assert — structurally read-only: exactly one (read) dispatch.
    assert len(runner.calls) == 1


def test_check_tokens_on_unreachable_peer_exits_two(
    master_cfg, master_token, peer_tokens_dir
):
    # Arrange
    runner = _ScriptedRunner(_proc(stdout="", rc=255, stderr="refused\n"))
    # Act
    result = check_tokens_peer(
        "spartan",
        master_cfg,
        runner=runner,
        tokens_dir=peer_tokens_dir,
        master_token_path=master_token,
    )
    # Assert
    assert result.exit_code == 2
