"""Tests for ``sac agents new`` — scaffold a fresh v3 spec.yaml.

Card sac-fresh-agent-specs (2026-06-13). Authoring policy is "fresh
template, not in-place repair": the operator runs ``sac agents new
<name>`` and gets a v3-clean spec.yaml + to_home/ skeleton next to it,
ready to edit. The validator must accept the output as-is.

Discipline: AAA markers each on their own line; one literal ``assert``
per test; real filesystem fixtures (``tmp_path``), no mocks.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._new import new as new_cmd
from scitex_agent_container.config._validation import validate_config


def test_new_writes_spec_yaml_at_target(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(new_cmd, ["my-agent", "--base-dir", str(base)])
    # Assert
    assert (base / "my-agent" / "spec.yaml").is_file()


def test_new_minimal_template_passes_v3_validator(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(
        new_cmd, ["fresh-agent", "--base-dir", str(base), "--template", "minimal"]
    )
    errors = validate_config(base / "fresh-agent" / "spec.yaml")
    # Assert — fresh template must satisfy the live validator (zero errors).
    assert errors == []


def test_new_full_template_passes_v3_validator(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(
        new_cmd, ["full-fresh", "--base-dir", str(base), "--template", "full"]
    )
    errors = validate_config(base / "full-fresh" / "spec.yaml")
    # Assert
    assert errors == []


def test_new_default_template_is_minimal(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    runner.invoke(new_cmd, ["defaulty", "--base-dir", str(base)])
    # Act — parse the spec YAML to inspect the rendered config keys, NOT
    # the prose docstring (the comment legitimately MENTIONS the field
    # name as an "add this if you need it" pointer).
    parsed = yaml.safe_load((base / "defaulty" / "spec.yaml").read_text())
    # Assert — minimal template omits startup_prompts; the full template ships it.
    assert "startup_prompts" not in parsed.get("spec", {})


def test_new_creates_to_home_skeleton(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(new_cmd, ["agent-x", "--base-dir", str(base)])
    # Assert — to_home/ exists as a sibling of spec.yaml so the runtime
    # auto-discovers it (spec-reference §to_home).
    assert (base / "agent-x" / "to_home").is_dir()


def test_new_refuses_to_overwrite_existing_spec(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    (base / "dupe").mkdir(parents=True)
    (base / "dupe" / "spec.yaml").write_text("# pre-existing\n")
    # Act
    result = runner.invoke(new_cmd, ["dupe", "--base-dir", str(base)])
    # Assert — non-zero exit so accidental clobber is impossible without --force.
    assert result.exit_code != 0


def test_new_force_overwrites_existing_spec(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    (base / "dupe2").mkdir(parents=True)
    (base / "dupe2" / "spec.yaml").write_text("# stale\n")
    # Act
    runner.invoke(new_cmd, ["dupe2", "--base-dir", str(base), "--force"])
    text = (base / "dupe2" / "spec.yaml").read_text()
    # Assert — fresh template replaces the stale stub (apiVersion line is canonical).
    assert "scitex-agent-container/v3" in text


def test_new_rejects_unknown_template(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    result = runner.invoke(
        new_cmd, ["bad", "--base-dir", str(base), "--template", "nope"]
    )
    # Assert — Click's Choice raises UsageError (exit code 2).
    assert result.exit_code != 0


def test_new_emits_canonical_apiversion_header(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(new_cmd, ["headered", "--base-dir", str(base)])
    first_lines = (base / "headered" / "spec.yaml").read_text().splitlines()[:10]
    # Assert — apiVersion appears in the file head (no buried boilerplate).
    assert any("apiVersion: scitex-agent-container/v3" in line for line in first_lines)


def test_new_template_kind_is_agent_not_agentproxy(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(new_cmd, ["kindly", "--base-dir", str(base)])
    text = (base / "kindly" / "spec.yaml").read_text()
    # Assert — default scaffold is the common case (SDK runner).
    assert "kind: Agent" in text


def test_new_rejects_invalid_agent_name(tmp_path: Path) -> None:
    # Arrange — names with slashes would write outside the base dir.
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    result = runner.invoke(new_cmd, ["bad/name", "--base-dir", str(base)])
    # Assert
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Dir-template instantiation (`_template_<kind>/` + SAC_PLACEHOLDER fills).
# ---------------------------------------------------------------------------

# A valid-once-filled minimal spec carrying placeholder tokens. After
# --project / --agent-id substitution the result must pass validate_config.
_DEMO_SPEC = """\
# SAC_PLACEHOLDER_AGENT_ID — demo dir-template.
apiVersion: scitex-agent-container/v3
kind: Agent

metadata:
  labels:
    role: worker
    groups: [demo]
    description: SAC_PLACEHOLDER_PROJECT agent SAC_PLACEHOLDER_AGENT_ID.

spec:
  runtime: apptainer
  host: local
  workdir: ~/proj/SAC_PLACEHOLDER_PROJECT/SAC_PLACEHOLDER_AGENT_ID

  apptainer:
    image: ~/.scitex/agent-container/containers/sac-base.sif
    binds: []

  claude:
    model: haiku
    flags:
      - --dangerously-skip-permissions

  health:
    enabled: true
    interval: 60

  restart:
    policy: on-failure
    max_retries: 3
# EOF
"""


def _make_demo_template(base: Path, *, extra_token: str | None = None) -> Path:
    """Create a fake ``_template_demo/`` dir under ``base`` and return it.

    Carries SAC_PLACEHOLDER_PROJECT / SAC_PLACEHOLDER_AGENT_ID in
    spec.yaml + a token inside to_home/ so substitution coverage spans
    the whole tree. ``extra_token`` (e.g. "EXTRA") injects a custom
    ``SAC_PLACEHOLDER_<EXTRA>`` into to_home/.
    """
    tdir = base / "_template_demo"
    (tdir / "to_home").mkdir(parents=True)
    (tdir / "spec.yaml").write_text(_DEMO_SPEC)
    home_body = "agent=SAC_PLACEHOLDER_AGENT_ID project=SAC_PLACEHOLDER_PROJECT\n"
    if extra_token is not None:
        home_body += f"extra=SAC_PLACEHOLDER_{extra_token}\n"
    (tdir / "to_home" / "CLAUDE.md").write_text(home_body)
    return tdir


def _no_placeholder_remains(agent_dir: Path) -> bool:
    """True iff no ``SAC_PLACEHOLDER_*`` token survives anywhere in tree."""
    for path in agent_dir.rglob("*"):
        if path.is_file() and "SAC_PLACEHOLDER_" in path.read_text():
            return False
    return True


def _invoke_demo(
    runner: CliRunner, base: Path, name: str, *extra_args: str
) -> object:
    """Instantiate the demo dir-template under ``base`` and return result."""
    return runner.invoke(
        new_cmd,
        [name, "--base-dir", str(base), "--template", "demo", *extra_args],
    )


def test_new_dir_template_instantiation_exits_zero(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    result = _invoke_demo(
        runner, base, "demo1", "--project", "myproj", "--agent-id", "demo1"
    )
    # Assert
    assert result.exit_code == 0


def test_new_dir_template_writes_spec_yaml(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    _invoke_demo(runner, base, "demo1", "--project", "myproj")
    # Assert
    assert (base / "demo1" / "spec.yaml").is_file()


def test_new_dir_template_leaves_no_placeholder(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    _invoke_demo(runner, base, "demo1", "--project", "myproj")
    # Assert
    assert _no_placeholder_remains(base / "demo1")


def test_new_dir_template_filled_spec_validates(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    _invoke_demo(runner, base, "demo1", "--project", "myproj")
    errors = validate_config(base / "demo1" / "spec.yaml")
    # Assert
    assert errors == []


def test_new_dir_template_copies_to_home_tree(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    runner.invoke(
        new_cmd,
        ["d2", "--base-dir", str(base), "--template", "demo", "--project", "p"],
    )
    home = (base / "d2" / "to_home" / "CLAUDE.md").read_text()
    # Assert — to_home/ copied AND its placeholder substituted (agent-id default).
    assert "agent=d2 project=p" in home


def test_new_dir_template_agent_id_defaults_to_name(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act — omit --agent-id; it must default to <name>.
    runner.invoke(
        new_cmd,
        ["nameddefault", "--base-dir", str(base), "--template", "demo", "--project", "p"],
    )
    text = (base / "nameddefault" / "to_home" / "CLAUDE.md").read_text()
    # Assert
    assert "agent=nameddefault" in text


def test_new_dir_template_missing_placeholder_exits_nonzero(tmp_path: Path) -> None:
    # Arrange — omit --project so SAC_PLACEHOLDER_PROJECT cannot be filled.
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    result = _invoke_demo(runner, base, "partial")
    # Assert
    assert result.exit_code != 0


def test_new_dir_template_missing_placeholder_names_token(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    result = _invoke_demo(runner, base, "partial")
    # Assert — error must name the exact unfilled token.
    assert "SAC_PLACEHOLDER_PROJECT" in result.output


def test_new_dir_template_missing_placeholder_leaves_no_output(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    _invoke_demo(runner, base, "partial")
    # Assert — partial output removed; no half-written agent.
    assert not (base / "partial").exists()


def test_new_dir_template_set_exits_zero(tmp_path: Path) -> None:
    # Arrange — demo carries a custom SAC_PLACEHOLDER_EXTRA token.
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base, extra_token="EXTRA")
    # Act
    result = _invoke_demo(
        runner, base, "withextra", "--project", "p", "--set", "EXTRA=val"
    )
    # Assert
    assert result.exit_code == 0


def test_new_dir_template_set_fills_custom_token(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base, extra_token="EXTRA")
    # Act
    _invoke_demo(runner, base, "withextra", "--project", "p", "--set", "EXTRA=val")
    text = (base / "withextra" / "to_home" / "CLAUDE.md").read_text()
    # Assert
    assert "extra=val" in text


def test_new_dir_template_discovery_is_dynamic(tmp_path: Path) -> None:
    # Arrange — a brand-new _template_demo/ dir with NO code change must
    # be accepted as a --template choice.
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    result = runner.invoke(
        new_cmd,
        ["dyn", "--base-dir", str(base), "--template", "demo", "--project", "p"],
    )
    # Assert
    assert result.exit_code == 0


def test_new_inline_minimal_still_works_with_dir_templates_present(
    tmp_path: Path,
) -> None:
    # Arrange — dir-template present, but inline 'minimal' must still render
    # the string template (not be shadowed).
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    runner.invoke(
        new_cmd, ["inl", "--base-dir", str(base), "--template", "minimal"]
    )
    errors = validate_config(base / "inl" / "spec.yaml")
    # Assert
    assert errors == []
