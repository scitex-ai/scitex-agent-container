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
  '<preamble> && <cmd>'`` when the peer carries a preamble, and is
  byte-identical to the pre-preamble shape when it doesn't.
- Backwards compat: a multi-hop peer without a preamble still emits
  ``-J <chain>`` and the raw command tail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state.host_config import (
    PeerSpec,
    build_ssh_argv,
    load,
)


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


def test_build_ssh_argv_without_preamble_is_unchanged():
    # Arrange — single-hop, no preamble: argv tail must be `[--, cmd...]`
    # exactly as before this feature landed.
    peers = {"mba": PeerSpec(name="mba", ssh="ywatanabe@mba.local")}
    # Act
    argv = build_ssh_argv("mba", ["sac", "agent", "list"], peers)
    # Assert — last 4 tokens are the ssh target + raw command tail.
    assert argv[-5:] == [
        "ywatanabe@mba.local",
        "--",
        "sac",
        "agent",
        "list",
    ]


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


def test_build_ssh_argv_joins_the_command_the_same_way_both_branches_do():
    """The preamble branch must treat ``command`` exactly as the bare one does.

    CHANGED 2026-08-17, and the previous assertion here was the reason a P1
    outage existed. It pinned the preamble branch to ``shlex.join``, i.e. to
    ``command`` being a REAL ARGV LIST — while the bare branch appends raw
    tokens for ssh to word-join, i.e. ``command`` being ALREADY SHELL-QUOTED.

    Two incompatible meanings for one parameter, selected by a branch the
    caller cannot see. NO CALLER COULD SATISFY BOTH: ``_spec_handoff``
    pre-quoted its script to make the bare branch work, the preamble branch
    quoted that a second time, and the remote bash looked for a FILE named
    ``sh -c '...'`` — rc=127 on every preamble peer, which is what made
    scitex-hub unstartable by any path (measured on compute-02 and -03, with
    the bare peer nas-03 returning 0 as the control).

    So the contract is now stateable in one sentence, and it is the bare
    branch's: ``command`` is a token list that will be SPACE-JOINED, and a
    caller wanting one remote token pre-quotes it. Every caller in the repo
    already satisfies that.

    THE COST, stated rather than hidden: an argument containing whitespace
    reflows into two on a preamble peer, where it previously survived. That is
    not a new hazard — it is what the bare branch has always done to the same
    input — but it IS a behaviour change for preamble peers, and the property
    this test used to guarantee is genuinely gone.

    Preserving quoting on BOTH branches is strictly better and is the
    follow-up: make the bare branch emit one ``shlex.join``ed element too.
    That is not bundled here because it changes the rendered argv SHAPE and
    breaks 13 tests that deliberately pin it (including
    ``test_build_ssh_argv_without_preamble_is_unchanged`` and
    ``test_home_rooted_peer_argv_is_byte_identical``, which exist to prove
    registry pinning leaves an unpinned peer's argv untouched). Rewriting a
    documented byte-identity invariant deserves its own review, not a hurried
    edit while an agent is down.
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
    # Assert
    assert argv[-1] == "bash -c 'module load Apptainer/1.3.3 && echo hello world'"


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
    bash wrapper is inserted, and (b) the command tail is byte-identical
    to the pre-preamble argv shape.
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


def test_build_ssh_argv_empty_preamble_preserves_raw_command_tail(
    empty_preamble_argv: list[str],
):
    # Arrange: fixture builds the argv for a peer with an empty preamble.
    argv = empty_preamble_argv
    # Act: read the last three tokens (separator + raw command).
    tail = argv[-3:]
    # Assert
    assert tail == ["--", "echo", "hi"]


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
