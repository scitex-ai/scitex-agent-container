"""Tests for ``sac agents create`` — scaffold a fresh v3 spec.yaml.

Card sac-fresh-agent-specs (2026-06-13). Authoring policy is "fresh
template, not in-place repair": the operator runs ``sac agents create
<name>`` and gets a v3-clean spec.yaml + to_home/ skeleton next to it,
ready to edit. The validator must accept the output as-is.

Renamed from ``new`` to ``create`` (card
refactor/consolidate-create-into-new-templates) — CRUD-consistent
naming now that the old, narrower ``create`` command was folded into
this one's dir-template system, freeing the name back up.

Discipline: AAA markers each on their own line; one literal ``assert``
per test; real filesystem fixtures (``tmp_path``), no mocks.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._create import create as create_cmd
from scitex_agent_container.config._validation import validate_config
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml


def test_create_writes_spec_yaml_at_target(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(create_cmd, ["my-agent", "--base-dir", str(base)])
    # Assert
    assert (base / "my-agent" / "spec.yaml").is_file()


def test_create_minimal_template_passes_v3_validator(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(
        create_cmd, ["fresh-agent", "--base-dir", str(base), "--template", "minimal"]
    )
    errors = validate_config(base / "fresh-agent" / "spec.yaml")
    # Assert — fresh template must satisfy the live validator (zero errors).
    assert errors == []


def test_create_full_template_passes_v3_validator(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(
        create_cmd, ["full-fresh", "--base-dir", str(base), "--template", "full"]
    )
    errors = validate_config(base / "full-fresh" / "spec.yaml")
    # Assert
    assert errors == []


# ---------------------------------------------------------------------------
# `full` template = the PROVEN developer shape (card
# sac-agents-new-template-stale; operator 2026-06-25 "very general, just
# developer like existing ones"). It must render a READY dev agent, not the
# stale generic skeleton (runtime apptainer, model sonnet, binds [],
# placeholder prompt, NO overlay / SCITEX_TODO_AGENT_ID / channels /
# editable-install / dev labels). These tests pin every load-bearing field
# the card flagged as missing — they FAIL against the old template.
# ---------------------------------------------------------------------------


def _render_full(tmp_path: Path, name: str = "proj-x") -> dict:
    """Render the inline ``full`` template for ``name``; return the parsed doc."""
    runner = CliRunner()
    base = tmp_path / "agents"
    runner.invoke(create_cmd, [name, "--base-dir", str(base), "--template", "full"])
    return yaml.safe_load((base / name / "spec.yaml").read_text())


def test_full_template_runtime_is_tui(tmp_path: Path) -> None:
    # Arrange
    doc = _render_full(tmp_path)
    # Act
    spec = doc["spec"]
    # Assert — interactive TUI dev agent, not the stale generic apptainer shape.
    assert spec["runtime"] == "tui"


def test_full_template_is_relaxed(tmp_path: Path) -> None:
    # Arrange
    doc = _render_full(tmp_path)
    # Act
    relaxed = doc["spec"]["apptainer"]["relaxed"]
    # Assert — relaxed isolation (dev agent shares the operator's host tree).
    assert relaxed is True


def test_full_template_declares_directory_overlay(tmp_path: Path) -> None:
    # Arrange
    doc = _render_full(tmp_path)
    # Act
    overlay = doc["spec"]["apptainer"]["overlay"]
    # Assert — persistent per-agent overlay (MISSING on the stale template).
    assert overlay.endswith("/overlays/proj-x/")


def test_full_template_wires_scitex_todo_agent_id(tmp_path: Path) -> None:
    # Arrange
    doc = _render_full(tmp_path)
    # Act
    env = doc["spec"]["apptainer"]["env"]
    # Assert — todo-store writes attribute to THIS agent (MISSING before).
    assert env["SCITEX_TODO_AGENT_ID"] == "proj-x"


def test_full_template_lists_the_three_fleet_channels(tmp_path: Path) -> None:
    # Arrange
    doc = _render_full(tmp_path)
    # Act
    channels = doc["spec"]["claude"]["channels"]
    # Assert — sac + scitex-todo + telegrammer push channels (all MISSING before).
    assert channels == [
        "server:sac",
        "server:scitex-todo",
        "server:claude-code-telegrammer",
    ]


def test_full_template_editable_installs_the_repo(tmp_path: Path) -> None:
    # Arrange
    doc = _render_full(tmp_path)
    # Act
    cmds = " ".join(sc.get("command", "") for sc in doc["spec"]["startup_commands"])
    # Assert — the dev loop editable-installs the agent's own repo (MISSING before).
    assert "pip install -e" in cmds


def test_full_template_kick_is_start_or_continue(tmp_path: Path) -> None:
    # Arrange
    doc = _render_full(tmp_path)
    # Act
    prompts = doc["spec"]["startup_prompts"]
    # Assert — a real self-resume kick, not a "replace this / READY" placeholder.
    assert prompts == ["Start or continue."]


def test_full_template_has_developer_metadata_labels(tmp_path: Path) -> None:
    # Arrange
    doc = _render_full(tmp_path)
    # Act
    role = doc["metadata"]["labels"]["role"]
    # Assert — developer role labels present (stale template was a bare 'worker').
    assert role == "project-maintainer"


def test_full_template_full_home_bind_at_canonical_path(tmp_path: Path) -> None:
    # Arrange
    doc = _render_full(tmp_path)
    home = str(Path.home())
    # Act
    binds = doc["spec"]["apptainer"]["binds"]
    # Assert — full host reach at the canonical path (was binds: [] on the stale one).
    assert f"{home}:{home}:rw" in binds


def test_full_template_model_is_opus(tmp_path: Path) -> None:
    # Arrange
    doc = _render_full(tmp_path)
    # Act
    model = doc["spec"]["claude"]["model"]
    # Assert — opus (dev workhorse), not the stale 'sonnet'.
    assert model.startswith("opus")


def test_full_template_workdir_under_home_matches_agent(tmp_path: Path) -> None:
    # Arrange
    doc = _render_full(tmp_path)
    home = str(Path.home())
    # Act
    workdir = doc["spec"]["workdir"]
    # Assert — --pwd is the repo path, bound rw above (host==container).
    assert workdir == f"{home}/proj/proj-x"


def test_create_default_template_is_minimal(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    runner.invoke(create_cmd, ["defaulty", "--base-dir", str(base)])
    # Act — parse the spec YAML to inspect the rendered config keys, NOT
    # the prose docstring (the comment legitimately MENTIONS the field
    # name as an "add this if you need it" pointer).
    parsed = yaml.safe_load((base / "defaulty" / "spec.yaml").read_text())
    # Assert — minimal template ships an EMPTY startup_prompts (explicit-fields
    # ruling 2026-07-21: the key must be present); the full template ships a
    # non-empty kick. Empty distinguishes the default=minimal rendering.
    assert parsed["spec"]["startup_prompts"] == []


def test_create_creates_to_home_skeleton(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(create_cmd, ["agent-x", "--base-dir", str(base)])
    # Assert — to_home/ exists as a sibling of spec.yaml so the runtime
    # auto-discovers it (spec-reference §to_home).
    assert (base / "agent-x" / "to_home").is_dir()


def test_create_refuses_to_overwrite_existing_spec(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    (base / "dupe").mkdir(parents=True)
    (base / "dupe" / "spec.yaml").write_text("# pre-existing\n")
    # Act
    result = runner.invoke(create_cmd, ["dupe", "--base-dir", str(base)])
    # Assert — non-zero exit so accidental clobber is impossible without --force.
    assert result.exit_code != 0


def test_create_force_overwrites_existing_spec(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    (base / "dupe2").mkdir(parents=True)
    (base / "dupe2" / "spec.yaml").write_text("# stale\n")
    # Act
    runner.invoke(create_cmd, ["dupe2", "--base-dir", str(base), "--force"])
    text = (base / "dupe2" / "spec.yaml").read_text()
    # Assert — fresh template replaces the stale stub (apiVersion line is canonical).
    assert "scitex-agent-container/v3" in text


def test_create_rejects_unknown_template(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    result = runner.invoke(
        create_cmd, ["bad", "--base-dir", str(base), "--template", "nope"]
    )
    # Assert — Click's Choice raises UsageError (exit code 2).
    assert result.exit_code != 0


def test_create_emits_canonical_apiversion_header(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(create_cmd, ["headered", "--base-dir", str(base)])
    first_lines = (base / "headered" / "spec.yaml").read_text().splitlines()[:10]
    # Assert — apiVersion appears in the file head (no buried boilerplate).
    assert any("apiVersion: scitex-agent-container/v3" in line for line in first_lines)


def test_create_template_kind_is_agent_not_agentproxy(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(create_cmd, ["kindly", "--base-dir", str(base)])
    text = (base / "kindly" / "spec.yaml").read_text()
    # Assert — default scaffold is the common case (SDK runner).
    assert "kind: Agent" in text


def test_create_rejects_invalid_agent_name(tmp_path: Path) -> None:
    # Arrange — names with slashes would write outside the base dir.
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    result = runner.invoke(create_cmd, ["bad/name", "--base-dir", str(base)])
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
  host: ${HOSTNAME}
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

# Red-start ruling 2026-07-21: fixture templates must carry EVERY field.
# The readable minimal body above stays the authored surface; the merge
# fills the remainder with the validator's own paste defaults.
_DEMO_SPEC = explicitize_yaml(_DEMO_SPEC)


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


def _invoke_demo(runner: CliRunner, base: Path, name: str, *extra_args: str) -> object:
    """Instantiate the demo dir-template under ``base`` and return result."""
    return runner.invoke(
        create_cmd,
        [name, "--base-dir", str(base), "--template", "demo", *extra_args],
    )


def test_create_dir_template_instantiation_exits_zero(tmp_path: Path) -> None:
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


def test_create_dir_template_writes_spec_yaml(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    _invoke_demo(runner, base, "demo1", "--project", "myproj")
    # Assert
    assert (base / "demo1" / "spec.yaml").is_file()


def test_create_dir_template_leaves_no_placeholder(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    _invoke_demo(runner, base, "demo1", "--project", "myproj")
    # Assert
    assert _no_placeholder_remains(base / "demo1")


def test_create_dir_template_filled_spec_validates(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    _invoke_demo(runner, base, "demo1", "--project", "myproj")
    errors = validate_config(base / "demo1" / "spec.yaml")
    # Assert
    assert errors == []


def test_create_dir_template_copies_to_home_tree(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    runner.invoke(
        create_cmd,
        ["d2", "--base-dir", str(base), "--template", "demo", "--project", "p"],
    )
    home = (base / "d2" / "to_home" / "CLAUDE.md").read_text()
    # Assert — to_home/ copied AND its placeholder substituted (agent-id default).
    assert "agent=d2 project=p" in home


def test_create_dir_template_agent_id_defaults_to_name(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act — omit --agent-id; it must default to <name>.
    runner.invoke(
        create_cmd,
        [
            "nameddefault",
            "--base-dir",
            str(base),
            "--template",
            "demo",
            "--project",
            "p",
        ],
    )
    text = (base / "nameddefault" / "to_home" / "CLAUDE.md").read_text()
    # Assert
    assert "agent=nameddefault" in text


def test_create_dir_template_missing_placeholder_exits_nonzero(tmp_path: Path) -> None:
    # Arrange — omit --project so SAC_PLACEHOLDER_PROJECT cannot be filled.
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    result = _invoke_demo(runner, base, "partial")
    # Assert
    assert result.exit_code != 0


def test_create_dir_template_missing_placeholder_names_token(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    result = _invoke_demo(runner, base, "partial")
    # Assert — error must name the exact unfilled token.
    assert "SAC_PLACEHOLDER_PROJECT" in result.output


def test_create_dir_template_missing_placeholder_leaves_no_output(
    tmp_path: Path,
) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    _invoke_demo(runner, base, "partial")
    # Assert — partial output removed; no half-written agent.
    assert not (base / "partial").exists()


def test_create_dir_template_set_exits_zero(tmp_path: Path) -> None:
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


def test_create_dir_template_set_fills_custom_token(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base, extra_token="EXTRA")
    # Act
    _invoke_demo(runner, base, "withextra", "--project", "p", "--set", "EXTRA=val")
    text = (base / "withextra" / "to_home" / "CLAUDE.md").read_text()
    # Assert
    assert "extra=val" in text


def test_create_dir_template_discovery_is_dynamic(tmp_path: Path) -> None:
    # Arrange — a brand-new _template_demo/ dir with NO code change must
    # be accepted as a --template choice.
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    result = runner.invoke(
        create_cmd,
        ["dyn", "--base-dir", str(base), "--template", "demo", "--project", "p"],
    )
    # Assert
    assert result.exit_code == 0


def test_create_inline_minimal_still_works_with_dir_templates_present(
    tmp_path: Path,
) -> None:
    # Arrange — dir-template present, but inline 'minimal' must still render
    # the string template (not be shadowed).
    runner = CliRunner()
    base = tmp_path / "agents"
    _make_demo_template(base)
    # Act
    runner.invoke(create_cmd, ["inl", "--base-dir", str(base), "--template", "minimal"])
    errors = validate_config(base / "inl" / "spec.yaml")
    # Assert
    assert errors == []


# ---------------------------------------------------------------------------
# Old-`create` consolidation — the retired, narrower `sac agents create`
# (auto-detect / marker-block machinery, card sac-templated-agent-create
# 2026-06-25) was folded into the dir-template system rather than kept as
# a separate command (card refactor/consolidate-create-into-new-templates).
# `developer`/`scientist` below are SYNTHETIC demo fixtures exercising the
# generic `_template_<kind>/` mechanism hermetically in CI — they do NOT
# mirror any real shipped template. The fleet's actual dir-templates are
# `_template_python_developer` / `_template_researcher` / `_template_generalist`
# (operator's agents root, outside this repo); there is no
# `_template_developer` / `_template_scientist` and there never will be.
# ---------------------------------------------------------------------------

_DEVELOPER_DEMO_SPEC = """\
# SAC_PLACEHOLDER_PROJECT — developer agent (mirrors _template_developer).
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: SAC_PLACEHOLDER_PROJECT
    purpose: SAC_PLACEHOLDER_PROJECT-maintainer
    role: project-maintainer
    groups: [developer]
    cardinality: singleton

spec:
  runtime: tui
  host: ${HOSTNAME}
  workdir: /home/ywatanabe/proj/SAC_PLACEHOLDER_PROJECT

  apptainer:
    image: /home/ywatanabe/.scitex/agent-container/containers/sac-base.sif
    relaxed: true
    binds:
      - /home/ywatanabe:/home/ywatanabe:rw
    raw_args:
      - --userns
      - --containall
      - --home=/home/agent
      - --overlay=/home/ywatanabe/.scitex/agent-container/containers/overlays/SAC_PLACEHOLDER_AGENT_ID/
      - --env=SCITEX_AGENT_CONTAINER_STATE_DB=/state/SAC_PLACEHOLDER_AGENT_ID/state.db
      - --env=SCITEX_TODO_AGENT_ID=SAC_PLACEHOLDER_AGENT_ID

  claude:
    model: claude-opus-4-8[1m]
    flags:
      - --dangerously-skip-permissions
    channels:
      - server:claude-code-telegrammer
      - server:sac
      - server:scitex-todo

  a2a:
    port: auto

  startup_commands:
    - command: 'echo install-step'

  startup_prompts:
    - Start or continue.

  health:
    enabled: true
    interval: 60

  restart:
    policy: on-failure
    max_retries: 3
"""

_SCIENTIST_DEMO_SPEC = _DEVELOPER_DEMO_SPEC.replace(
    "SAC_PLACEHOLDER_PROJECT-maintainer", "SAC_PLACEHOLDER_PROJECT-research"
).replace("groups: [developer]", "groups: [scientist]")

# Red-start ruling 2026-07-21: fixture templates must carry EVERY field
# (merged from the validator's own paste defaults; minimal body stays
# the authored surface). Derive scientist from developer BEFORE the
# merge so the string replaces still hit the readable bodies.
_DEVELOPER_DEMO_SPEC = explicitize_yaml(_DEVELOPER_DEMO_SPEC)
_SCIENTIST_DEMO_SPEC = explicitize_yaml(_SCIENTIST_DEMO_SPEC)


def _write_dir_template(base: Path, kind: str, body: str) -> Path:
    """Create a fake ``_template_<kind>/`` dir under ``base`` and return it."""
    tdir = base / f"_template_{kind}"
    (tdir / "to_home").mkdir(parents=True)
    (tdir / "spec.yaml").write_text(body)
    return tdir


def test_create_developer_template_leaves_no_placeholder(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _write_dir_template(base, "developer", _DEVELOPER_DEMO_SPEC)
    # Act
    runner.invoke(
        create_cmd,
        [
            "dev1",
            "--base-dir",
            str(base),
            "--template",
            "developer",
            "--project",
            "myproj",
        ],
    )
    # Assert
    assert _no_placeholder_remains(base / "dev1")


def test_create_developer_template_validates(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _write_dir_template(base, "developer", _DEVELOPER_DEMO_SPEC)
    # Act
    runner.invoke(
        create_cmd,
        [
            "dev2",
            "--base-dir",
            str(base),
            "--template",
            "developer",
            "--project",
            "myproj",
        ],
    )
    errors = validate_config(base / "dev2" / "spec.yaml")
    # Assert
    assert errors == []


def test_create_developer_template_group_label(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _write_dir_template(base, "developer", _DEVELOPER_DEMO_SPEC)
    # Act
    runner.invoke(
        create_cmd,
        [
            "dev3",
            "--base-dir",
            str(base),
            "--template",
            "developer",
            "--project",
            "myproj",
        ],
    )
    parsed = yaml.safe_load((base / "dev3" / "spec.yaml").read_text())
    # Assert
    assert parsed["metadata"]["labels"]["groups"] == ["developer"]


def test_create_scientist_template_leaves_no_placeholder(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _write_dir_template(base, "scientist", _SCIENTIST_DEMO_SPEC)
    # Act
    runner.invoke(
        create_cmd,
        [
            "sci1",
            "--base-dir",
            str(base),
            "--template",
            "scientist",
            "--project",
            "paperx",
        ],
    )
    # Assert
    assert _no_placeholder_remains(base / "sci1")


def test_create_scientist_template_validates(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _write_dir_template(base, "scientist", _SCIENTIST_DEMO_SPEC)
    # Act
    runner.invoke(
        create_cmd,
        [
            "sci2",
            "--base-dir",
            str(base),
            "--template",
            "scientist",
            "--project",
            "paperx",
        ],
    )
    errors = validate_config(base / "sci2" / "spec.yaml")
    # Assert
    assert errors == []


def test_create_scientist_template_group_label(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _write_dir_template(base, "scientist", _SCIENTIST_DEMO_SPEC)
    # Act
    runner.invoke(
        create_cmd,
        [
            "sci3",
            "--base-dir",
            str(base),
            "--template",
            "scientist",
            "--project",
            "paperx",
        ],
    )
    parsed = yaml.safe_load((base / "sci3" / "spec.yaml").read_text())
    # Assert
    assert parsed["metadata"]["labels"]["groups"] == ["scientist"]


def test_create_scientist_template_purpose_suffix(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    _write_dir_template(base, "scientist", _SCIENTIST_DEMO_SPEC)
    # Act
    runner.invoke(
        create_cmd,
        [
            "sci4",
            "--base-dir",
            str(base),
            "--template",
            "scientist",
            "--project",
            "paperx",
        ],
    )
    parsed = yaml.safe_load((base / "sci4" / "spec.yaml").read_text())
    # Assert — scientist purpose is the research suffix (mirrors retired `create`).
    assert parsed["metadata"]["labels"]["purpose"] == "paperx-research"


# ---------------------------------------------------------------------------
# `--help` shows the LIVE `_template_*` set, not a stale hardcoded list.
# ---------------------------------------------------------------------------


def test_create_help_lists_live_dir_templates(tmp_path: Path) -> None:
    # Arrange — a brand-new throwaway dir-template with NO code change must
    # surface in --help, proving the list is live-scanned, not hardcoded.
    runner = CliRunner()
    base = tmp_path / "agents"
    _write_dir_template(base, "zzz_test", _DEVELOPER_DEMO_SPEC)
    # Act
    result = runner.invoke(create_cmd, ["--base-dir", str(base), "--help"])
    # Assert
    assert "zzz_test" in result.output


def test_create_help_lists_inline_templates(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    result = runner.invoke(create_cmd, ["--base-dir", str(base), "--help"])
    # Assert — built-in presets are always listed alongside any dir-templates.
    assert "minimal" in result.output and "full" in result.output


def test_create_help_omits_dir_template_absent_from_root(tmp_path: Path) -> None:
    # Arrange — an EMPTY agents root: no _template_* dirs exist.
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act — collapse ALL whitespace so the check is terminal-width-independent.
    # click's HelpFormatter wraps the epilog to the detected terminal width
    # (which varies under `apptainer exec` / CI, where COLUMNS may not
    # propagate into the pytest subprocess), and a narrow width can split
    # "none found" across a line break — the historical SIF failure. Matching
    # on the normalized stream asserts the message, not the wrap column.
    result = runner.invoke(create_cmd, ["--base-dir", str(base), "--help"])
    normalized = " ".join(result.output.split())
    # Assert — nothing invented; the live scan reports none found.
    assert "none found" in normalized


def test_create_rejects_unknown_template_still_lists_choices(tmp_path: Path) -> None:
    # Arrange — regression check: an unknown --template still fails loud
    # and names the valid choices (unaffected by the --help epilog change).
    runner = CliRunner()
    base = tmp_path / "agents"
    _write_dir_template(base, "developer", _DEVELOPER_DEMO_SPEC)
    # Act
    result = runner.invoke(
        create_cmd, ["whoops", "--base-dir", str(base), "--template", "nope"]
    )
    # Assert
    assert "developer" in result.output and result.exit_code != 0
