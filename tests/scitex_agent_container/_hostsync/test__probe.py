"""Tests for the READ-ONLY remote probe.

PA-306: no mocks. The ssh round-trip runs through the real
``subprocess.run`` against a PATH-installed ``ssh`` shim that prints a
canned ``SAC_SYNC`` marker block — the same shape a real peer emits.
Resilience paths use real injected ``runner`` callables.

The sharpest test in this file is
``test_truncated_probe_output_is_unreachable_not_clean``: a probe that
did not finish has told us NOTHING, and rendering that as "clean" is
precisely how a false-green ships stale code.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scitex_agent_container._hostsync import GraphState, probe_peer
from scitex_agent_container._hostsync._probe import render_probe_snippet
from scitex_agent_container._state.host_config import PeerSpec

_REPO = "/data/gpfs/projects/punim0264/ywatanabe/scitex-agent-container"
_MODULE = f"{_REPO}/src/scitex_agent_container/__init__.py"


def _peers(*names: str) -> dict[str, PeerSpec]:
    return {n: PeerSpec(name=n, ssh=f"user@{n}") for n in names}


def _marker_block(
    *,
    head: str = "aaaa111",
    target_sha: str = "aaaa111",
    ahead: int = 0,
    behind: int = 0,
    dirty: tuple[str, ...] = (),
    ahead_commits: tuple[str, ...] = (),
    symbol: str = "['agent_name', 'preferred']",
    end: bool = True,
) -> str:
    lines = [
        "SAC_SYNC interpreter=/home/ywatanabe/.env-3.11/bin/python3",
        f"SAC_SYNC module={_MODULE}",
        f"SAC_SYNC repo={_REPO}",
        "SAC_SYNC target=origin/develop",
        f"SAC_SYNC target_sha={target_sha}",
        f"SAC_SYNC head={head}",
        f"SAC_SYNC ahead={ahead}",
        f"SAC_SYNC behind={behind}",
    ]
    lines += [f"SAC_SYNC ahead_commit={c}" for c in ahead_commits]
    lines += [f"SAC_SYNC dirty={d}" for d in dirty]
    lines.append(f"SAC_SYNC symbol={symbol}")
    if end:
        lines.append("SAC_SYNC end")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# parsing the object-graph verdict
# ---------------------------------------------------------------------------


def test_behind_peer_parses_as_behind(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout=_marker_block(behind=5, target_sha="bbbb222"))
    # Act
    report = probe_peer("spartan", _peers("spartan"))
    # Assert
    assert report.state is GraphState.BEHIND


def test_behind_peer_parses_the_behind_count(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout=_marker_block(behind=5, target_sha="bbbb222"))
    # Act
    report = probe_peer("spartan", _peers("spartan"))
    # Assert
    assert report.behind == 5


def test_ahead_peer_parses_as_ahead(subprocess_shim):
    # Arrange
    subprocess_shim.install(
        "ssh",
        stdout=_marker_block(ahead=2, ahead_commits=("abc123 wip", "def456 hack")),
    )
    # Act
    report = probe_peer("spartan", _peers("spartan"))
    # Assert
    assert report.state is GraphState.AHEAD


def test_ahead_commits_are_carried_back_for_printing(subprocess_shim):
    # Arrange — these are what a human must SEE before deciding.
    subprocess_shim.install(
        "ssh", stdout=_marker_block(ahead=1, ahead_commits=("abc123 emergency patch",))
    )
    # Act
    report = probe_peer("spartan", _peers("spartan"))
    # Assert
    assert report.ahead_commits == ("abc123 emergency patch",)


def test_diverged_peer_parses_as_diverged(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout=_marker_block(ahead=2, behind=3))
    # Act
    report = probe_peer("spartan", _peers("spartan"))
    # Assert
    assert report.state is GraphState.DIVERGED


def test_clean_matching_peer_parses_as_current(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout=_marker_block())
    # Act
    report = probe_peer("spartan", _peers("spartan"))
    # Assert
    assert report.state is GraphState.CURRENT


def test_dirty_files_are_carried_back(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout=_marker_block(dirty=(" M src/foo.py",)))
    # Act
    report = probe_peer("spartan", _peers("spartan"))
    # Assert
    assert report.dirty_files == (" M src/foo.py",)


def test_loaded_module_path_is_reported(subprocess_shim):
    # Arrange — the module PATH is evidence; a version string is not.
    subprocess_shim.install("ssh", stdout=_marker_block())
    # Act
    report = probe_peer("spartan", _peers("spartan"))
    # Assert
    assert report.module == _MODULE


def test_symbol_signature_is_reported(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout=_marker_block(symbol="['agent_name']"))
    # Act
    report = probe_peer("spartan", _peers("spartan"))
    # Assert
    assert report.symbol == "['agent_name']"


def test_marker_after_motd_noise_is_still_parsed(subprocess_shim):
    # Arrange — login shells print a motd before our markers.
    subprocess_shim.install(
        "ssh", stdout="Welcome to Spartan\nLast login: ...\n" + _marker_block(behind=1)
    )
    # Act
    report = probe_peer("spartan", _peers("spartan"))
    # Assert
    assert report.state is GraphState.BEHIND


# ---------------------------------------------------------------------------
# UNKNOWN is not clean — the ternary rule
# ---------------------------------------------------------------------------


def test_truncated_probe_output_is_unreachable_not_clean(subprocess_shim):
    # Arrange — output cut off mid-flight: no `end` sentinel.
    subprocess_shim.install("ssh", stdout=_marker_block(end=False))
    # Act
    report = probe_peer("spartan", _peers("spartan"))
    # Assert — a probe that did not finish is UNKNOWN, never CURRENT.
    assert report.state is GraphState.UNREACHABLE


def test_empty_ssh_output_is_unreachable(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", exit=255, stderr="ssh: connect: refused\n")
    # Act
    report = probe_peer("spartan", _peers("spartan"))
    # Assert
    assert report.state is GraphState.UNREACHABLE


def test_missing_module_is_reported_as_no_module(subprocess_shim):
    # Arrange
    subprocess_shim.install(
        "ssh",
        stdout="SAC_SYNC interpreter=/usr/bin/python3\nSAC_SYNC state=no-module\nSAC_SYNC end\n",
    )
    # Act
    report = probe_peer("mba", _peers("mba"))
    # Assert
    assert report.state is GraphState.NO_MODULE


def test_wheel_install_is_reported_as_not_a_checkout(subprocess_shim):
    # Arrange — importable, but not from a git tree: nothing to reconcile.
    subprocess_shim.install(
        "ssh",
        stdout=(
            "SAC_SYNC module=/venv/lib/python3.11/site-packages/"
            "scitex_agent_container/__init__.py\n"
            "SAC_SYNC state=not-a-checkout\nSAC_SYNC end\n"
        ),
    )
    # Act
    report = probe_peer("nas", _peers("nas"))
    # Assert
    assert report.state is GraphState.NOT_A_CHECKOUT


def test_failed_remote_fetch_is_unreachable(subprocess_shim):
    # Arrange — cannot compare the graph, so the verdict is UNKNOWN.
    subprocess_shim.install(
        "ssh",
        stdout=f"SAC_SYNC module={_MODULE}\nSAC_SYNC repo={_REPO}\n"
        "SAC_SYNC state=fetch-failed\nSAC_SYNC end\n",
    )
    # Act
    report = probe_peer("spartan", _peers("spartan"))
    # Assert
    assert report.state is GraphState.UNREACHABLE


def test_ssh_timeout_is_unreachable():
    # Arrange — a real callable that raises exactly as ssh would.
    def timing_out_runner(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=5)

    # Act
    report = probe_peer("spartan", _peers("spartan"), runner=timing_out_runner)
    # Assert
    assert report.state is GraphState.UNREACHABLE


def test_undefined_peer_is_unreachable():
    # Arrange — 'ghost' is not in the peers map.
    # Act
    report = probe_peer("ghost", _peers("spartan"))
    # Assert
    assert report.state is GraphState.UNREACHABLE


# ---------------------------------------------------------------------------
# the `~` footgun: a remote path must NEVER be expanded on the centre
# ---------------------------------------------------------------------------


def test_probe_resolves_checkout_from_the_loaded_module():
    # Arrange — the checkout is found by asking the interpreter, not guessed.
    # Act
    snippet = render_probe_snippet()
    # Assert
    assert "scitex_agent_container as s; print(s.__file__)" in snippet


def test_probe_takes_git_toplevel_containing_the_module():
    # Arrange
    # Act
    snippet = render_probe_snippet()
    # Assert — the tree we sync is the tree that backs the running code.
    assert 'git -C "$(dirname "$mod")" rev-parse --show-toplevel' in snippet


def test_probe_snippet_never_bakes_in_the_centres_home():
    # Arrange — expanding `~` locally yields the CENTRE's home, not the
    # peer's; that bug put Spartan's fleet state inside a paper project.
    home = str(Path.home())
    # Act
    snippet = render_probe_snippet()
    # Assert
    assert home not in snippet


def test_probe_snippet_uses_the_object_graph_not_mtimes():
    # Arrange — mtimes are rewritten by a plain pull and skewed on GPFS.
    # Act
    snippet = render_probe_snippet()
    # Assert
    assert "rev-list --count" in snippet


def test_ssh_argv_carries_the_probe_snippet(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout=_marker_block())
    # Act
    probe_peer("spartan", _peers("spartan"))
    argv = subprocess_shim.argv_for("ssh")
    # Assert — dispatched through build_ssh_argv, sac's single ssh path.
    assert "SAC_SYNC" in " ".join(argv)
