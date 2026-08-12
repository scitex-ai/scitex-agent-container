"""Tests for ``sac agents declare-a2a-host``.

The property that matters most is that DRY-RUN IS THE DEFAULT: an operator who
types the verb with no flags must not have written anything. The registry dir
is redirected through ``SCITEX_AGENT_CONTAINER_AGENTS_DIR`` (the override
``refresh_acl`` already defines) via a yield fixture — STX-NM002 forbids
monkeypatch.

STX-TQ007: one logical assert per test.
"""

from __future__ import annotations

import os

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._declare_a2a_host import declare_a2a_host

_WITHOUT_HOST = "spec:\n  host: ywata-note-win\n  a2a:\n    port: auto\n"
_WITH_HOST = "spec:\n  a2a:\n    port: auto\n    host: 127.0.0.1\n"
_ENV = "SCITEX_AGENT_CONTAINER_AGENTS_DIR"


@pytest.fixture()
def fleet(tmp_path):
    """A tmp registry, wired in through the documented env override."""
    root = tmp_path / "agents"
    for agent, text in (("needs-it", _WITHOUT_HOST), ("already-has-it", _WITH_HOST)):
        (root / agent).mkdir(parents=True)
        (root / agent / "spec.yaml").write_text(text)
    previous = os.environ.get(_ENV)
    os.environ[_ENV] = str(root)
    yield root
    if previous is None:
        os.environ.pop(_ENV, None)
    else:
        os.environ[_ENV] = previous


def test_the_default_invocation_writes_nothing(fleet) -> None:
    # Arrange — report by default, mutate only on request.
    root = fleet
    # Act
    CliRunner().invoke(declare_a2a_host, [])
    # Assert
    assert (root / "needs-it" / "spec.yaml").read_text() == _WITHOUT_HOST


def test_the_dry_run_names_the_spec_it_would_change(fleet) -> None:
    # Arrange
    _ = fleet
    # Act
    result = CliRunner().invoke(declare_a2a_host, [])
    # Assert
    assert "needs-it" in result.output


def test_the_dry_run_reports_the_value_it_would_write(fleet) -> None:
    # Arrange — an operator must be able to see it is the code default.
    _ = fleet
    # Act
    result = CliRunner().invoke(declare_a2a_host, [])
    # Assert
    assert "127.0.0.1" in result.output


def test_a_clean_dry_run_exits_zero(fleet) -> None:
    # Arrange
    _ = fleet
    # Act
    result = CliRunner().invoke(declare_a2a_host, [])
    # Assert
    assert result.exit_code == 0


def test_a_dry_run_that_found_a_problem_does_not_exit_zero(fleet) -> None:
    # Arrange — an unreadable spec means the plan does not describe what would
    # happen. Anything scripting this verb reads the exit code, not the prose,
    # so a plan that is not safe to apply must not look like a clean run.
    root = fleet
    (root / "broken").mkdir()
    (root / "broken" / "spec.yaml").write_bytes(b"\xff\xfe\x00binary")
    # Act
    result = CliRunner().invoke(declare_a2a_host, [])
    # Assert
    assert result.exit_code != 0


def test_an_unreadable_spec_is_named_in_the_dry_run(fleet) -> None:
    # Arrange — an exit code alone does not say WHICH spec to go and fix.
    root = fleet
    (root / "broken").mkdir()
    (root / "broken" / "spec.yaml").write_bytes(b"\xff\xfe\x00binary")
    # Act
    result = CliRunner().invoke(declare_a2a_host, [])
    # Assert
    assert "broken" in result.output


def test_applying_writes_the_declaration(fleet) -> None:
    # Arrange
    root = fleet
    # Act
    CliRunner().invoke(declare_a2a_host, ["--apply"])
    # Assert
    assert "    host: 127.0.0.1\n" in (root / "needs-it" / "spec.yaml").read_text()


def test_applying_leaves_an_already_declaring_spec_byte_identical(fleet) -> None:
    # Arrange
    root = fleet
    # Act
    CliRunner().invoke(declare_a2a_host, ["--apply"])
    # Assert
    assert (root / "already-has-it" / "spec.yaml").read_text() == _WITH_HOST


def test_applying_twice_is_a_no_op(fleet) -> None:
    # Arrange — the sweep must be safe to re-run.
    root = fleet
    CliRunner().invoke(declare_a2a_host, ["--apply"])
    once = (root / "needs-it" / "spec.yaml").read_text()
    # Act
    CliRunner().invoke(declare_a2a_host, ["--apply"])
    # Assert
    assert (root / "needs-it" / "spec.yaml").read_text() == once


def test_there_is_no_host_option_to_change_what_agents_bind_to(fleet) -> None:
    # Arrange — an operator-supplied host would make "zero behaviour change" a
    # property of the invocation rather than of the command.
    _ = fleet
    # Act
    result = CliRunner().invoke(declare_a2a_host, ["--host", "0.0.0.0"])
    # Assert
    assert result.exit_code != 0


def test_a_missing_registry_is_reported_not_silently_skipped(tmp_path) -> None:
    # Arrange
    previous = os.environ.get(_ENV)
    os.environ[_ENV] = str(tmp_path / "nope")
    # Act
    result = CliRunner().invoke(declare_a2a_host, [])
    # Assert
    try:
        assert "not found" in result.output
    finally:
        if previous is None:
            os.environ.pop(_ENV, None)
        else:
            os.environ[_ENV] = previous


def test_the_verb_is_registered_on_the_agents_group() -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.agent_group import agent_group

    # Act
    registered = agent_group.commands.get("declare-a2a-host")
    # Assert
    assert registered is not None
