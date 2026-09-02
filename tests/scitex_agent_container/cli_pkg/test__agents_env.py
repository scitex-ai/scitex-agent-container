"""CLI tests for ``sac agents env set`` / ``sac agents env unset``.

Real ``CliRunner`` against the real Click group, over a real registry
directory built under ``tmp_path`` and pointed at with the same
``SCITEX_AGENT_CONTAINER_AGENTS_DIR`` override the production resolver reads.
No mocks and no monkeypatching: the spec files are files, and what the command
did to them is read back off disk.

WHAT IS NOT EXERCISED HERE, AND WHY IT IS STILL COVERED
    ``--restart`` needs a host, a tmux session and a live agent, none of which
    exist in a test — so the DECISION it depends on was factored out of the
    command into :func:`...config._env_block_line.should_restart` and is
    exercised there. What this file pins instead is the observable side effect
    the decision is made from: a no-op leaves the spec file's mtime and bytes
    untouched, so there is nothing for a restart to be warranted by.

STX-NM002: no mocks, no monkeypatch.
STX-TQ007: one logical assert per test.
"""

from __future__ import annotations

import os

import yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.agent_group import agent_group

_REGISTRY_ENV = "SCITEX_AGENT_CONTAINER_AGENTS_DIR"

_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    role: worker
spec:
  host: ywata-note-win
  apptainer:
    image: /x.sif
    env:
      # the slot the pool could not derive
      CCT_BOT_TOKEN_SLOT: SAC
    post: ''
  claude:
    model: fable
"""


def _registry(tmp_path, name: str = "demo", text: str = _SPEC):
    """Write ``<tmp_path>/<name>/spec.yaml`` and return (registry, spec path)."""
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    spec = agent_dir / "spec.yaml"
    spec.write_text(text, encoding="utf-8")
    return tmp_path, spec


def _run(registry, args):
    """Invoke ``sac agents <args>`` with the registry override in the env."""
    return CliRunner().invoke(agent_group, args, env={_REGISTRY_ENV: str(registry)})


def _env_of(spec):
    return yaml.safe_load(spec.read_text(encoding="utf-8"))["spec"]["apptainer"]["env"]


# ---------------------------------------------------------------------------
# registration


def test_the_env_subgroup_is_registered_on_agents() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["env", "--help"])
    # Assert
    assert result.exit_code == 0


def test_the_env_set_verb_is_reachable_through_the_group() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["env", "set", "--help"])
    # Assert
    assert result.exit_code == 0


def test_the_env_unset_verb_is_reachable_through_the_group() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["env", "unset", "--help"])
    # Assert
    assert result.exit_code == 0


def test_env_is_listed_in_the_agents_group_help() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["--help"])
    # Assert
    assert "env" in result.output


# ---------------------------------------------------------------------------
# set


def test_setting_a_missing_key_writes_it(tmp_path) -> None:
    # Arrange
    registry, spec = _registry(tmp_path)
    # Act
    _run(registry, ["env", "set", "demo", "NEW_VAR=1"])
    # Assert
    assert _env_of(spec)["NEW_VAR"] == "1"


def test_setting_a_missing_key_exits_zero(tmp_path) -> None:
    # Arrange
    registry, _ = _registry(tmp_path)
    # Act
    result = _run(registry, ["env", "set", "demo", "NEW_VAR=1"])
    # Assert
    assert result.exit_code == 0


def test_setting_an_existing_key_replaces_its_value(tmp_path) -> None:
    # Arrange
    registry, spec = _registry(tmp_path)
    # Act
    _run(registry, ["env", "set", "demo", "CCT_BOT_TOKEN_SLOT=OTHER"])
    # Assert
    assert _env_of(spec)["CCT_BOT_TOKEN_SLOT"] == "OTHER"


def test_setting_an_existing_key_keeps_the_rest_of_the_spec(tmp_path) -> None:
    # Arrange — the comment above the key is the operator's record of WHY.
    registry, spec = _registry(tmp_path)
    # Act
    _run(registry, ["env", "set", "demo", "CCT_BOT_TOKEN_SLOT=OTHER"])
    # Assert
    assert "# the slot the pool could not derive" in spec.read_text(encoding="utf-8")


def test_several_keys_are_set_in_one_invocation(tmp_path) -> None:
    # Arrange
    registry, spec = _registry(tmp_path)
    # Act
    _run(registry, ["env", "set", "demo", "A=1", "B=2"])
    # Assert
    assert (_env_of(spec)["A"], _env_of(spec)["B"]) == ("1", "2")


def test_setting_a_key_already_at_that_value_exits_zero(tmp_path) -> None:
    # Arrange — "already correct" is success, not a failure to change.
    registry, _ = _registry(tmp_path)
    # Act
    result = _run(registry, ["env", "set", "demo", "CCT_BOT_TOKEN_SLOT=SAC"])
    # Assert
    assert result.exit_code == 0


def test_setting_a_key_already_at_that_value_does_not_rewrite_the_file(
    tmp_path,
) -> None:
    # Arrange — the mtime is the honest witness: it moves on any write, even
    # one that happens to produce identical bytes.
    registry, spec = _registry(tmp_path)
    os.utime(spec, (1_000_000, 1_000_000))
    # Act
    _run(registry, ["env", "set", "demo", "CCT_BOT_TOKEN_SLOT=SAC"])
    # Assert
    assert spec.stat().st_mtime == 1_000_000


def test_setting_a_key_already_at_that_value_says_so(tmp_path) -> None:
    # Arrange
    registry, _ = _registry(tmp_path)
    # Act
    result = _run(registry, ["env", "set", "demo", "CCT_BOT_TOKEN_SLOT=SAC"])
    # Assert
    assert "already at target" in result.output


def test_a_restart_that_was_asked_for_is_skipped_when_nothing_changed(
    tmp_path,
) -> None:
    # Arrange — the command must SAY it declined, so an operator watching a
    # sweep can tell a skipped restart from a silently missing one.
    registry, _ = _registry(tmp_path)
    # Act
    result = _run(
        registry, ["env", "set", "demo", "CCT_BOT_TOKEN_SLOT=SAC", "--restart"]
    )
    # Assert
    assert "not restarting" in result.output


def test_a_restart_that_was_asked_for_leaves_the_spec_alone_when_nothing_changed(
    tmp_path,
) -> None:
    # Arrange
    registry, spec = _registry(tmp_path)
    before = spec.read_text(encoding="utf-8")
    # Act
    _run(registry, ["env", "set", "demo", "CCT_BOT_TOKEN_SLOT=SAC", "--restart"])
    # Assert
    assert spec.read_text(encoding="utf-8") == before


def test_a_value_containing_a_colon_survives_the_round_trip(tmp_path) -> None:
    # Arrange — unquoted, this splits into two keys.
    registry, spec = _registry(tmp_path)
    # Act
    _run(registry, ["env", "set", "demo", "DSN=postgresql://h:55432/db"])
    # Assert
    assert _env_of(spec)["DSN"] == "postgresql://h:55432/db"


def test_a_value_containing_an_equals_sign_is_kept_whole(tmp_path) -> None:
    # Arrange — a real fleet value.
    registry, spec = _registry(tmp_path)
    # Act
    _run(registry, ["env", "set", "demo", "GIT_SSH_COMMAND=ssh -o A=no"])
    # Assert
    assert _env_of(spec)["GIT_SSH_COMMAND"] == "ssh -o A=no"


def test_a_key_that_also_names_a_field_elsewhere_leaves_that_field_alone(
    tmp_path,
) -> None:
    # Arrange — `spec.claude.model` is the first `^\\s+model:` line in the
    # document; the retired shell script rewrote it.
    registry, spec = _registry(tmp_path)
    # Act
    _run(registry, ["env", "set", "demo", "model=SOMETHING-ELSE"])
    # Assert
    loaded = yaml.safe_load(spec.read_text(encoding="utf-8"))
    assert loaded["spec"]["claude"]["model"] == "fable"


# ---------------------------------------------------------------------------
# unset


def test_unsetting_a_present_key_removes_it(tmp_path) -> None:
    # Arrange
    registry, spec = _registry(tmp_path)
    # Act
    _run(registry, ["env", "unset", "demo", "CCT_BOT_TOKEN_SLOT"])
    # Assert
    assert "CCT_BOT_TOKEN_SLOT" not in _env_of(spec)


def test_unsetting_a_missing_key_exits_zero(tmp_path) -> None:
    # Arrange — the end state is already reached; that is not an error.
    registry, _ = _registry(tmp_path)
    # Act
    result = _run(registry, ["env", "unset", "demo", "NOT_THERE"])
    # Assert
    assert result.exit_code == 0


def test_unsetting_a_missing_key_does_not_rewrite_the_file(tmp_path) -> None:
    # Arrange
    registry, spec = _registry(tmp_path)
    os.utime(spec, (1_000_000, 1_000_000))
    # Act
    _run(registry, ["env", "unset", "demo", "NOT_THERE"])
    # Assert
    assert spec.stat().st_mtime == 1_000_000


def test_unsetting_a_missing_key_reports_it_as_not_set(tmp_path) -> None:
    # Arrange
    registry, _ = _registry(tmp_path)
    # Act
    result = _run(registry, ["env", "unset", "demo", "NOT_THERE"])
    # Assert
    assert "not set" in result.output


# ---------------------------------------------------------------------------
# exit codes


def test_an_unknown_agent_exits_one(tmp_path) -> None:
    # Arrange
    registry, _ = _registry(tmp_path)
    # Act
    result = _run(registry, ["env", "set", "nosuch", "A=1"])
    # Assert
    assert result.exit_code == 1


def test_an_unknown_agent_names_the_path_it_looked_for(tmp_path) -> None:
    # Arrange
    registry, _ = _registry(tmp_path)
    # Act
    result = _run(registry, ["env", "set", "nosuch", "A=1"])
    # Assert
    assert "nosuch/spec.yaml" in result.output


def test_an_argument_with_no_equals_exits_two(tmp_path) -> None:
    # Arrange
    registry, _ = _registry(tmp_path)
    # Act
    result = _run(registry, ["env", "set", "demo", "NOEQUALS"])
    # Assert
    assert result.exit_code == 2


def test_a_malformed_key_exits_two(tmp_path) -> None:
    # Arrange
    registry, _ = _registry(tmp_path)
    # Act
    result = _run(registry, ["env", "set", "demo", "not.a.var=1"])
    # Assert
    assert result.exit_code == 2


def test_a_malformed_argument_does_not_touch_the_spec(tmp_path) -> None:
    # Arrange — the arguments are parsed before the file is opened.
    registry, spec = _registry(tmp_path)
    before = spec.read_text(encoding="utf-8")
    # Act
    _run(registry, ["env", "set", "demo", "A=1", "NOEQUALS"])
    # Assert
    assert spec.read_text(encoding="utf-8") == before


def test_a_malformed_key_on_unset_exits_two(tmp_path) -> None:
    # Arrange
    registry, _ = _registry(tmp_path)
    # Act
    result = _run(registry, ["env", "unset", "demo", "1BAD"])
    # Assert
    assert result.exit_code == 2


def test_a_spec_shape_the_editor_refuses_exits_one(tmp_path) -> None:
    # Arrange — a multi-line value cannot be rewritten as one line.
    registry, _ = _registry(
        tmp_path,
        text="spec:\n  apptainer:\n    env:\n      S: |\n        one\n        two\n",
    )
    # Act
    result = _run(registry, ["env", "set", "demo", "S=x"])
    # Assert
    assert result.exit_code == 1


def test_a_refused_spec_is_left_byte_identical(tmp_path) -> None:
    # Arrange
    registry, spec = _registry(
        tmp_path,
        text="spec:\n  apptainer:\n    env:\n      S: |\n        one\n        two\n",
    )
    before = spec.read_text(encoding="utf-8")
    # Act
    _run(registry, ["env", "set", "demo", "S=x"])
    # Assert
    assert spec.read_text(encoding="utf-8") == before
