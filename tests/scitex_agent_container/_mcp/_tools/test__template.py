"""Tests for ``_mcp/_tools/_template.py`` — contributor spec rendering."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scitex_agent_container.config._validation import validate_raw
from scitex_agent_container._mcp._tools._template import (  # noqa: F401
    _derive_branch_short,
    _render,
    register_template_tools,
    template_render_contributor_spec,
)


@pytest.fixture
def dry_run_result() -> dict:
    return template_render_contributor_spec(
        name="c-sac-alpha",
        port=9001,
        task="echo hello",
    )


@pytest.fixture
def written_result(tmp_path: Path) -> dict:
    return template_render_contributor_spec(
        name="c-sac-beta",
        port=9002,
        task="run task",
        output_dir=str(tmp_path),
        dry_run=False,
    )


def test_derive_branch_short_strips_c_sac_prefix() -> None:
    # Arrange
    name = "c-sac-foo"
    # Act
    result = _derive_branch_short(name)
    # Assert
    assert result == "foo"


def test_derive_branch_short_strips_c_prefix() -> None:
    # Arrange
    name = "c-bar"
    # Act
    result = _derive_branch_short(name)
    # Assert
    assert result == "bar"


def test_derive_branch_short_returns_name_when_no_prefix() -> None:
    # Arrange
    name = "plain-name"
    # Act
    result = _derive_branch_short(name)
    # Assert
    assert result == "plain-name"


def test_render_substitutes_simple_variable() -> None:
    # Arrange
    template = "hello {{ name }}"
    # Act
    rendered = _render(template, {"name": "world"})
    # Assert
    assert rendered == "hello world"


def test_render_tolerates_whitespace_inside_braces() -> None:
    # Arrange
    template = "x={{x}} y={{  y  }}"
    # Act
    rendered = _render(template, {"x": "1", "y": "2"})
    # Assert
    assert rendered == "x=1 y=2"


def test_render_raises_keyerror_for_missing_variable() -> None:
    # Arrange
    template = "missing={{ nope }}"
    mapping: dict[str, str] = {}
    # Act
    raised: Exception | None = None
    try:
        _render(template, mapping)
    except KeyError as exc:
        raised = exc
    # Assert
    assert isinstance(raised, KeyError)


def test_dry_run_returns_written_false(dry_run_result: dict) -> None:
    # Arrange
    result = dry_run_result
    # Act
    written = result["written"]
    # Assert
    assert written is False


def test_dry_run_yaml_field_parses_as_yaml(dry_run_result: dict) -> None:
    # Arrange
    result = dry_run_result
    # Act
    doc = yaml.safe_load(result["yaml"])
    # Assert
    assert doc["apiVersion"] == "scitex-agent-container/v3"


def test_dry_run_yaml_includes_substituted_port(dry_run_result: dict) -> None:
    # Arrange
    result = dry_run_result
    # Act
    doc = yaml.safe_load(result["yaml"])
    # Assert
    assert doc["spec"]["a2a"]["port"] == 9001


def test_dry_run_yaml_includes_substituted_task(dry_run_result: dict) -> None:
    # Arrange
    result = dry_run_result
    # Act
    doc = yaml.safe_load(result["yaml"])
    # Assert
    assert doc["spec"]["startup_commands"][0]["command"] == "echo hello"


def test_dry_run_labels_strip_c_sac_branch_short(dry_run_result: dict) -> None:
    # Arrange
    result = dry_run_result
    # Act
    doc = yaml.safe_load(result["yaml"])
    # Assert
    assert doc["metadata"]["labels"]["branch_short"] == "alpha"


def test_dry_run_path_targets_named_yaml_file(dry_run_result: dict) -> None:
    # Arrange
    result = dry_run_result
    # Act
    path = result["path"]
    # Assert
    assert path.endswith("c-sac-alpha.yaml")


def test_write_path_returns_written_true(written_result: dict) -> None:
    # Arrange
    result = written_result
    # Act
    written = result["written"]
    # Assert
    assert written is True


def test_write_path_creates_file_on_disk(written_result: dict) -> None:
    # Arrange
    result = written_result
    # Act
    exists = Path(result["path"]).is_file()
    # Assert
    assert exists is True


def test_write_path_file_contents_match_yaml_field(written_result: dict) -> None:
    # Arrange
    result = written_result
    # Act
    disk_text = Path(result["path"]).read_text()
    # Assert
    assert disk_text == result["yaml"]


def test_write_path_uses_explicit_branch_kind(tmp_path: Path) -> None:
    # Arrange
    result = template_render_contributor_spec(
        name="gamma",
        port=9003,
        task="cmd",
        branch_kind="fix",
        output_dir=str(tmp_path),
        dry_run=False,
    )
    # Act
    doc = yaml.safe_load(result["yaml"])
    # Assert
    assert doc["metadata"]["labels"]["branch_kind"] == "fix"


def test_write_path_uses_explicit_branch_short(tmp_path: Path) -> None:
    # Arrange
    result = template_render_contributor_spec(
        name="delta",
        port=9004,
        task="cmd",
        branch_short="custom-slug",
        output_dir=str(tmp_path),
        dry_run=False,
    )
    # Act
    doc = yaml.safe_load(result["yaml"])
    # Assert
    assert doc["metadata"]["labels"]["branch_short"] == "custom-slug"


def test_register_template_tools_invokes_mcp_tool_decorator() -> None:
    # Arrange
    registered: list = []

    class _RecordingMCP:
        def tool(self):
            def _decorator(fn):
                registered.append(fn)
                return fn

            return _decorator

    mcp = _RecordingMCP()
    # Act
    register_template_tools(mcp)
    # Assert
    assert registered == [template_render_contributor_spec]


def test_rendered_contributor_spec_opens_with_the_design_document_line(
    dry_run_result: dict,
) -> None:
    # Arrange — operator ruling 2026-08-11 (ADR-0022 §3): a spec is the
    # contract for an agent not yet started; a running agent's state lives
    # in the database. The header says so where a reader will see it.
    expected = (
        "# THIS IS A DESIGN DOCUMENT — the contract for an agent not yet started."
    )
    # Act
    first_line = dry_run_result["yaml"].splitlines()[0]
    # Assert
    assert first_line == expected


def test_rendered_contributor_spec_sends_state_to_the_database(
    dry_run_result: dict,
) -> None:
    # Arrange
    expected = (
        "# The state of a RUNNING agent lives in the database, never in this file."
    )
    # Act
    second_line = dry_run_result["yaml"].splitlines()[1]
    # Assert
    assert second_line == expected


def test_contributor_header_is_a_comment_and_does_not_change_the_spec(
    dry_run_result: dict,
) -> None:
    # Arrange
    # Act
    doc = yaml.safe_load(dry_run_result["yaml"])
    # Assert
    assert doc["apiVersion"] == "scitex-agent-container/v3"

# ---------------------------------------------------------------------------
# The rendered spec must be VALID, not merely parseable.
#
# Measured 2026-09-17: the tool rendered its own embedded template, which the
# v3 grammar had left behind — top-level `image` / `model` / `skills`, a
# `multiplexer` key, `health.method: multiplexer-alive`, no `host`, and 65
# required fields absent. The validator rejected it with 8 errors (the missing
# set dominating), so every spec materialised from this tool was born invalid:
# 28 agents in the fleet inventory carry exactly that shape, and a twin inherits
# its parent's spec verbatim. The field set now comes from the canonical
# scaffold in cli_pkg/_create_templates.py, which renders clean, and the tool
# validates its own output before returning it.
# ---------------------------------------------------------------------------


def test_the_rendered_spec_validates_against_the_v3_grammar(dry_run_result: dict) -> None:
    """A generator that can emit an invalid spec is a generator that will."""
    # Arrange
    doc = yaml.safe_load(dry_run_result["yaml"])
    # Act
    errors = validate_raw(doc, "<test:rendered-contributor-spec>")
    # Assert
    assert errors == [], f"the rendered contributor spec must load: {errors[:3]}"


def test_the_rendered_spec_names_the_portable_image(dry_run_result: dict) -> None:
    """#1376 made managed image specs portable; a .sif path is an artefact."""
    # Arrange
    doc = yaml.safe_load(dry_run_result["yaml"])
    # Act
    image = doc["spec"]["apptainer"]["image"]
    # Assert
    assert image == "sac-base"


def test_the_rendered_spec_has_no_legacy_top_level_keys(dry_run_result: dict) -> None:
    """image / model / skills / multiplexer moved in the v3 realignment."""
    # Arrange
    doc = yaml.safe_load(dry_run_result["yaml"])
    # Act
    legacy = [key for key in ("image", "model", "skills", "multiplexer") if key in doc["spec"]]
    # Assert
    assert legacy == []


def test_the_rendered_spec_declares_a_host(dry_run_result: dict) -> None:
    """`spec.host` is REQUIRED (operator directive 2026-06-23, no hidden default)."""
    # Arrange
    doc = yaml.safe_load(dry_run_result["yaml"])
    # Act
    host = doc["spec"].get("host")
    # Assert
    assert host, "a rendered spec must declare placement"

# ---------------------------------------------------------------------------
# The startup task is DATA, and the spec is YAML.
#
# Found by turning my own review checklist on the fix before asking anyone else
# to: the task was interpolated into the text as a bare scalar, so
#   "do the thing: carefully"  -> ScannerError (mapping values not allowed)
#   "run task #42"            -> VALID YAML, command silently became "run task"
#   "line one\nline two"      -> ScannerError
# The second one is the dangerous shape: no error, no warning, half the command
# gone. Escaping is the fix, and a round-trip is the assertion.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "task",
    [
        "do the thing: carefully",
        "run task #42",
        'say "hello" now',
        "line one\nline two",
        "path C:\\tmp\\x",
        "--- not a doc",
    ],
)
def test_the_startup_task_round_trips_exactly(tmp_path: Path, task: str) -> None:
    """A task is data: it must survive the YAML text unchanged."""
    # Arrange / Act
    result = template_render_contributor_spec(
        name="round-trip", port=19991, task=task, output_dir=str(tmp_path), dry_run=False
    )
    # Assert
    doc = yaml.safe_load(result["yaml"])
    assert doc["spec"]["startup_commands"][0]["command"] == task


@pytest.mark.parametrize(
    "task",
    [
        "do the thing: carefully",
        "run task #42",
        "line one\nline two",
    ],
)
def test_a_task_with_yaml_significant_characters_still_validates(tmp_path: Path, task: str) -> None:
    """The spec must LOAD — a task is allowed to contain ':' or a newline."""
    # Arrange / Act
    result = template_render_contributor_spec(
        name="yaml-task", port=19992, task=task, output_dir=str(tmp_path), dry_run=False
    )
    # Assert
    errors = validate_raw(yaml.safe_load(result["yaml"]), "<task-with-yaml-chars>")
    assert errors == []


def test_a_project_name_with_yaml_characters_does_not_break_the_labels() -> None:
    """The labels block is text too: the same class of bug lives there."""
    # Arrange / Act
    result = template_render_contributor_spec(
        name="label-check", port=19993, task="ok", target_repo="repo: with colon"
    )
    # Assert
    doc = yaml.safe_load(result["yaml"])
    assert doc["metadata"]["labels"]["project"] == "repo: with colon"
