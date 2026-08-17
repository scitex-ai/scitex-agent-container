"""Tests for the optional ``env_preamble`` field on peer entries.

Carved into its own file (rather than appended to
``test_host_config.py``) because the parent test module is already at
the project's per-file line ceiling. Sister-test file convention
matches the existing ``test__`` / ``test_`` siblings under this dir.

Covers:
- ``PeerSpec.env_preamble`` defaults to ``()`` so peers without the
  field stay byte-identical to the pre-preamble shape.
- ``load`` parses the YAML scalar (``|`` literal block) form, the list
  form, and tolerates absence.
- ``build_ssh_argv`` wraps the dispatched command in ``bash -c
  '<preamble> && <cmd>'`` when the peer carries a preamble, and emits
  the command unwrapped when it doesn't.
- Both branches quote the command EXACTLY ONCE (2026-08-17): each
  renders the whole remote command line into ONE trailing argv
  element, so a whitespace-bearing argument survives as a single token
  whichever branch a peer happens to take.
- Backwards compat: a multi-hop peer without a preamble still emits
  ``-J <chain>`` and dispatches the command with no ``bash -c`` wrapper.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from scitex_agent_container._state.host_config import (
    PeerSpec,
    build_ssh_argv,
    load,
)


def _remote_command(argv: list[str]) -> str:
    """The command line the REMOTE shell receives, from either branch.

    ssh word-joins everything after the host and hands the result to the
    remote user's shell, so ``build_ssh_argv`` puts the entire remote
    command line into ONE trailing argv element: ``shlex.join(command)``
    on the bare branch, and ``bash -c '<preamble> && <cmd>'`` on the
    preamble branch.

    Tests that care about WHAT RUNS REMOTELY assert on this string rather
    than on token positions in the argv. The token count is an artifact of
    the local rendering — it has already changed twice while the remote
    command line stayed the thing that had to be right. Assertions about
    ssh OPTIONS, the ``-J`` chain or the host target still read the argv
    directly; those parts are unaffected.
    """
    tail = argv[-1]
    if tail.startswith("bash -c "):
        return shlex.split(tail)[2]
    return tail


@pytest.fixture
def cfg_path(tmp_path: Path, env_save_restore) -> Path:
    """Real config.yaml at tmp_path, surfaced via the env override."""
    p = tmp_path / "config.yaml"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


# ---------------------------------------------------------------------------
# Schema / load
# ---------------------------------------------------------------------------


def test_peer_spec_env_preamble_defaults_to_empty_tuple():
    # Arrange
    peer = PeerSpec(name="mba", ssh="user@mba.local")
    # Act
    out = peer.env_preamble
    # Assert
    assert out == ()


def test_peer_spec_joined_preamble_is_empty_when_unset():
    # Arrange
    peer = PeerSpec(name="mba", ssh="user@mba.local")
    # Act
    joined = peer.joined_preamble()
    # Assert
    assert joined == ""


def test_peer_spec_joined_preamble_joins_with_double_ampersand():
    # Arrange
    peer = PeerSpec(
        name="spartan-bm152",
        ssh="spartan-bm152",
        env_preamble=("module load GCCcore/11.3.0", "module load Apptainer/1.3.3"),
    )
    # Act
    joined = peer.joined_preamble()
    # Assert
    assert joined == "module load GCCcore/11.3.0 && module load Apptainer/1.3.3"


def test_load_parses_env_preamble_from_yaml_literal_block(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  spartan-bm152:
    ssh: spartan-bm152
    env_preamble: |
      module load GCCcore/11.3.0
      module load Apptainer/1.3.3
"""
    )
    # Act
    cfg = load()
    # Assert
    assert cfg.peers["spartan-bm152"].env_preamble == (
        "module load GCCcore/11.3.0",
        "module load Apptainer/1.3.3",
    )


def test_load_parses_env_preamble_from_yaml_list(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  spartan-bm152:
    ssh: spartan-bm152
    env_preamble:
      - module load GCCcore/11.3.0
      - module load Apptainer/1.3.3
"""
    )
    # Act
    cfg = load()
    # Assert
    assert cfg.peers["spartan-bm152"].env_preamble == (
        "module load GCCcore/11.3.0",
        "module load Apptainer/1.3.3",
    )


def test_load_omits_blank_and_comment_lines_in_preamble(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  spartan:
    ssh: spartan
    env_preamble: |
      # lmod chain — see skill 02_04 for why this needs two lines

      module load GCCcore/11.3.0
      module load Apptainer/1.3.3
"""
    )
    # Act
    cfg = load()
    # Assert
    assert cfg.peers["spartan"].env_preamble == (
        "module load GCCcore/11.3.0",
        "module load Apptainer/1.3.3",
    )


def test_load_peer_without_preamble_field_yields_empty_tuple(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    # Act
    cfg = load()
    # Assert
    assert cfg.peers["mba"].env_preamble == ()


def test_load_rejects_non_string_preamble_entry(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  weird:
    ssh: weird
    env_preamble:
      - 42
"""
    )
    # Act
    raised = pytest.raises(ValueError, match="env_preamble")
    # Assert
    with raised:
        load()


def test_load_rejects_dict_preamble(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  weird:
    ssh: weird
    env_preamble:
      a: b
"""
    )
    # Act
    raised = pytest.raises(ValueError, match="env_preamble")
    # Assert
    with raised:
        load()


# ---------------------------------------------------------------------------
# build_ssh_argv — wrapping behaviour
# ---------------------------------------------------------------------------


def test_build_ssh_argv_without_preamble_dispatches_the_command_unwrapped():
    """A peer with no preamble gets no ``bash -c`` wrapper interposed.

    RESHAPED 2026-08-17 (was ``..._is_unchanged``, pinning the five raw
    tail tokens ``[host, --, sac, agent, list]``). The quote-once fix
    collapses the command into ONE trailing element, so the literal token
    count is gone — but the property that test existed for is not: a peer
    without a preamble is still dispatched to ITS ssh target, after the
    ``--`` separator, with the command going straight to the remote login
    shell and nothing wrapped around it. That is what is asserted now, and
    it still fails loudly if the preamble branch ever leaks into a peer
    that carries no preamble.
    """
    # Arrange
    peers = {"mba": PeerSpec(name="mba", ssh="ywatanabe@mba.local")}
    # Act
    argv = build_ssh_argv("mba", ["sac", "agent", "list"], peers)
    # Assert — ssh target, separator, then the bare remote command line.
    assert argv[-3:] == ["ywatanabe@mba.local", "--", "sac agent list"]


@pytest.fixture
def spartan_argv() -> list[str]:
    """Render the ssh argv for a single-hop Spartan-style peer.

    Shared by the trio of single-assert tests below that each pin one
    facet of the wrapping shape (binary, flag, joined inner string).
    """
    peers = {
        "spartan-bm152": PeerSpec(
            name="spartan-bm152",
            ssh="spartan-bm152",
            env_preamble=(
                "module load GCCcore/11.3.0",
                "module load Apptainer/1.3.3",
            ),
        )
    }
    return build_ssh_argv("spartan-bm152", ["which", "apptainer"], peers)


def test_build_ssh_argv_appends_single_bash_lc_token_when_preamble_present(
    spartan_argv: list[str],
):
    # Arrange: fixture builds the argv for a Spartan-style peer.
    argv = spartan_argv
    # Act: read the last argv token (the collapsed `bash -c '...'` blob).
    final = argv[-1]
    # Assert
    assert final.startswith("bash -c '")


def test_build_ssh_argv_inner_string_starts_with_full_preamble(
    spartan_argv: list[str],
):
    # Arrange: fixture builds the argv for a Spartan-style peer.
    argv = spartan_argv
    # Act: read the last argv token (the collapsed `bash -c '...'` blob).
    final = argv[-1]
    # Assert
    assert "module load GCCcore/11.3.0 && module load Apptainer/1.3.3 && " in final


def test_build_ssh_argv_inner_string_appends_user_command_after_preamble(
    spartan_argv: list[str],
):
    # Arrange: fixture builds the argv for a Spartan-style peer.
    argv = spartan_argv
    # Act: read the last argv token (the collapsed `bash -c '...'` blob).
    final = argv[-1]
    # Assert
    assert final.endswith("which apptainer'")


def test_build_ssh_argv_preamble_peer_keeps_whitespace_argument_as_one_token():
    """A whitespace-bearing argument survives as ONE token on a preamble peer.

    REWRITTEN 2026-08-17, twice-measured, and the earlier versions of this
    test were each part of an outage rather than a guard against one.

    v1 pinned the preamble branch to ``shlex.join`` while the bare branch
    appended raw already-quoted tokens. Two incompatible meanings for one
    parameter, selected by a branch the caller cannot see: ``_spec_handoff``
    pre-quoted its script to satisfy the bare branch, the preamble branch
    quoted it a SECOND time, and the remote bash looked for a FILE named
    ``sh -c '...'`` — rc=127 on every preamble peer, which took scitex-hub
    down and unstartable by any path.

    v2 (mine) made the preamble branch SPACE-JOIN to match the bare one and
    asserted here that ``echo 'hello world'`` reflows into two remote
    arguments. Consistent, and consistently wrong: it exported the bare
    branch's inability to carry a quoted argument onto the preamble peers
    and broke a live caller passing ``["python3", "-c", "<one-liner>"]``
    (A/B on scitex-compute-03: parent rc=0, that fix rc=2 syntax error).

    THE DECIDING MEASUREMENT was the bare peer nas-03 in that same A/B: it
    failed on BOTH renderings. The bare branch had never once carried a
    whitespace-bearing argument — nothing exercised it, so a contract defect
    read as a preamble-only problem. ``command`` now means one thing in both
    branches (a real argv list whose quoting ``build_ssh_argv`` owns), and
    THIS is the property that makes the contract worth having.
    """
    # Arrange
    peers = {
        "spartan-bm152": PeerSpec(
            name="spartan-bm152",
            ssh="spartan-bm152",
            env_preamble=("module load Apptainer/1.3.3",),
        )
    }
    # Act
    argv = build_ssh_argv("spartan-bm152", ["echo", "hello world"], peers)
    # Assert — re-parse the remote command line the way the remote shell
    # will; it must yield back the argv we handed in, not a reflowed one.
    assert shlex.split(_remote_command(argv))[-2:] == ["echo", "hello world"]


def test_build_ssh_argv_bare_peer_keeps_whitespace_argument_as_one_token():
    """The same guarantee on a peer with no preamble — the branch that lacked it.

    Companion to the preamble case above, and the one the fix actually
    ADDED behaviour to. nas-03 (a bare peer) was the control in the
    2026-08-17 A/B and failed on both candidate renderings, because this
    branch used to append raw tokens for ssh to word-join: ``hello world``
    arrived as two remote arguments. Nothing in the fleet had exercised
    it, so the defect sat here silently.

    Pinning it on BOTH peer kinds is deliberate — a peer's dispatch must
    not depend on whether it happens to carry an ``env_preamble``.
    """
    # Arrange
    peers = {"nas-03": PeerSpec(name="nas-03", ssh="nas-03")}
    # Act
    argv = build_ssh_argv("nas-03", ["echo", "hello world"], peers)
    # Assert — re-parse the remote command line the way the remote shell
    # will; it must yield back the argv we handed in, not a reflowed one.
    assert shlex.split(_remote_command(argv))[-2:] == ["echo", "hello world"]


@pytest.fixture
def multihop_preamble_argv() -> list[str]:
    """Render the ssh argv for a multi-hop peer that also carries a preamble.

    Shared by the trio of single-assert tests below that pin (a) the
    ProxyJump flag, (b) the jump-host target, and (c) the wrapped inner
    command — all three must coexist when both ``via`` and
    ``env_preamble`` are set.
    """
    peers = {
        "spartan": PeerSpec(name="spartan", ssh="spartan"),
        "spartan-bm152": PeerSpec(
            name="spartan-bm152",
            ssh="spartan-bm152",
            via=("spartan",),
            env_preamble=("module load Apptainer/1.3.3",),
        ),
    }
    return build_ssh_argv("spartan-bm152", ["sac", "agent", "list"], peers)


def test_build_ssh_argv_multihop_with_preamble_emits_proxy_jump_flag(
    multihop_preamble_argv: list[str],
):
    # Arrange: fixture builds the argv for a multi-hop preamble peer.
    argv = multihop_preamble_argv
    # Act: check whether the -J flag is present.
    has_proxy_jump = "-J" in argv
    # Assert
    assert has_proxy_jump


def test_build_ssh_argv_multihop_with_preamble_uses_correct_jump_target(
    multihop_preamble_argv: list[str],
):
    # Arrange: fixture builds the argv for a multi-hop preamble peer.
    argv = multihop_preamble_argv
    # Act: read the token after -J (the ProxyJump target list).
    jump_target = argv[argv.index("-J") + 1]
    # Assert
    assert jump_target == "spartan"


def test_build_ssh_argv_multihop_with_preamble_still_wraps_inner_command(
    multihop_preamble_argv: list[str],
):
    # Arrange: fixture builds the argv for a multi-hop preamble peer.
    argv = multihop_preamble_argv
    # Act: read the collapsed `bash -c '...'` blob at the tail of argv.
    final = argv[-1]
    # Assert
    assert final.startswith("bash -c 'module load Apptainer/1.3.3 && ")


@pytest.fixture
def empty_preamble_argv() -> list[str]:
    """Render the ssh argv for a peer with an explicit empty preamble.

    Shared by the pair of single-assert tests below that pin (a) no
    bash wrapper is inserted, and (b) the command reaches the remote
    directly, right after the ``--`` separator.
    """
    peers = {"mba": PeerSpec(name="mba", ssh="mba", env_preamble=())}
    return build_ssh_argv("mba", ["echo", "hi"], peers)


def test_build_ssh_argv_empty_preamble_does_not_insert_bash_wrapper(
    empty_preamble_argv: list[str],
):
    # Arrange: fixture builds the argv for a peer with an empty preamble.
    argv = empty_preamble_argv
    # Act: check whether `bash` appears anywhere in the argv.
    has_bash = "bash" in argv
    # Assert
    assert not has_bash


def test_build_ssh_argv_empty_preamble_sends_command_after_the_separator(
    empty_preamble_argv: list[str],
):
    """An EMPTY preamble takes the bare branch, command intact after ``--``.

    RESHAPED 2026-08-17 (was ``..._preserves_raw_command_tail``, pinning
    ``["--", "echo", "hi"]``). The quote-once fix renders the command as one
    joined element, so "raw tokens" is no longer the shape — but the property
    is untouched and is the one this test was written for: an ``env_preamble``
    that is present-but-empty must be treated as ABSENT, i.e. the command
    goes straight to the remote shell after the separator with no wrapper
    layer between. The sibling test above pins the no-``bash`` half.
    """
    # Arrange: fixture builds the argv for a peer with an empty preamble.
    argv = empty_preamble_argv
    # Act: read the last two tokens (separator + remote command line).
    tail = argv[-2:]
    # Assert
    assert tail == ["--", "echo hi"]


# ---------------------------------------------------------------------------
# TOFU policy: build_ssh_argv pre-bakes ``StrictHostKeyChecking=accept-new``
# so the first-touch dispatch to a freshly-registered peer adds the host
# key on first connect (then refuses changes). Without this, BatchMode=yes
# turns the missing host-key into a silent rc-1 with no actionable error
# for the operator.
# ---------------------------------------------------------------------------


def test_build_ssh_argv_includes_accept_new_strict_host_key_option():
    # Arrange
    peers = {"mba": PeerSpec(name="mba", ssh="mba")}
    # Act
    argv = build_ssh_argv("mba", ["echo", "hi"], peers)
    # Assert — TOFU flag is in the rendered argv.
    joined = " ".join(argv)
    assert "StrictHostKeyChecking=accept-new" in joined


def test_build_ssh_argv_keeps_batchmode_with_accept_new_policy():
    # Arrange — the accept-new + BatchMode combo is the explicit TOFU
    # contract: accept a NEW key, but never prompt for one.
    peers = {"mba": PeerSpec(name="mba", ssh="mba")}
    # Act
    argv = build_ssh_argv("mba", ["echo", "hi"], peers)
    # Assert
    joined = " ".join(argv)
    assert "BatchMode=yes" in joined and "StrictHostKeyChecking=accept-new" in joined
