"""Tests for scitex_agent_container._state.fleet_template (F-CS2).

One-assertion-per-test with explicit Arrange/Act/Assert markers
(TQ002/TQ007). ``pytest.parametrize`` is used where the matrix is
genuinely declarative (TQ001). No mocks/monkeypatch (PA-306).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state.fleet_template import (
    expand_params_file,
    find_unsubstituted_vars,
    read_csv_rows,
    render_one,
)

_TEMPLATE = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata: { labels: { project: ${PROJECT}, capsule: ${CAPSULE_ID} } }
spec:
  runtime: apptainer
  # Concrete host — not a HOSTNAME placeholder: expand fails loud on any
  # leftover dollar-brace token (comments included), so fleet templates
  # carry concrete/CSV-driven placement.
  host: fleet-host
  workdir: /tmp/${name}-workdir
  apptainer:
    image: /x.sif
    binds: []
  claude:
    model: sonnet
  health:
    enabled: true
    interval: 60
  restart:
    policy: on-failure
    max_retries: 3
  startup_commands:
    - command: "Run capsule ${CAPSULE_ID} on ${PROJECT}."
"""

_TWO_ROW_CSV = "name,PROJECT,CAPSULE_ID\ncap-aa-1,paper-x,aa-1\ncap-aa-2,paper-x,aa-2\n"


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


@pytest.fixture
def two_row_expansion(tmp_path: Path) -> list[Path]:
    """Materialise the standard two-row fleet used by several tests."""
    template = tmp_path / "template.yaml"
    csv_file = tmp_path / "fleet.csv"
    _write(template, _TEMPLATE)
    _write(csv_file, _TWO_ROW_CSV)
    return expand_params_file(template, csv_file, tmp_path / "out")


def test_expand_emits_one_yaml_per_csv_row(two_row_expansion: list[Path]):
    # Arrange
    paths = two_row_expansion
    # Act
    names = [p.name for p in paths]
    # Assert
    assert names == ["cap-aa-1.yaml", "cap-aa-2.yaml"]


def test_expand_substitutes_all_placeholders(two_row_expansion: list[Path]):
    # Arrange
    rendered = two_row_expansion[0].read_text()
    # Act
    leftover = find_unsubstituted_vars(rendered)
    # Assert
    assert leftover == []


def test_expand_substitutes_name_token_in_workdir(
    two_row_expansion: list[Path],
):
    # Arrange
    rendered = two_row_expansion[0].read_text()
    # Act
    workdir_present = "/tmp/cap-aa-1-workdir" in rendered
    # Assert
    assert workdir_present


def test_expand_substitutes_named_column_value(
    two_row_expansion: list[Path],
):
    # Arrange
    rendered = two_row_expansion[0].read_text()
    # Act
    project_present = "project: paper-x" in rendered
    # Assert
    assert project_present


def test_expand_exposes_name_column_as_dollar_name(tmp_path: Path):
    """The 'name' column is also exposed as ``${name}`` in templates."""
    # Arrange
    template = tmp_path / "t.yaml"
    csv_file = tmp_path / "f.csv"
    _write(template, "spec:\n  workdir: /tmp/${name}\n")
    _write(csv_file, "name\nfoo\n")
    # Act
    paths = expand_params_file(template, csv_file, tmp_path / "out")
    # Assert
    assert "/tmp/foo" in paths[0].read_text()


@pytest.mark.parametrize(
    "template_body, csv_body, expected_match",
    [
        pytest.param(
            "spec: { workdir: /tmp/${MISSING_VAR} }\n",
            "name\nfoo\n",
            "MISSING_VAR",
            id="unresolved-placeholder",
        ),
        pytest.param(
            "spec: { runtime: apptainer }\n",
            "PROJECT,CAPSULE_ID\np,c\n",
            "name",
            id="missing-name-column",
        ),
        pytest.param(
            "spec: { runtime: apptainer }\n",
            "name\nfoo\nfoo\n",
            "duplicate",
            id="duplicate-names",
        ),
    ],
)
def test_expand_rejects_invalid_input(
    tmp_path: Path,
    template_body: str,
    csv_body: str,
    expected_match: str,
):
    # Arrange
    template = tmp_path / "t.yaml"
    csv_file = tmp_path / "f.csv"
    _write(template, template_body)
    _write(csv_file, csv_body)
    raiser = pytest.raises(ValueError, match=expected_match)
    # Act
    # Assert
    with raiser:
        expand_params_file(template, csv_file, tmp_path / "out")


def test_expand_skips_blank_rows(tmp_path: Path):
    # Arrange
    template = tmp_path / "t.yaml"
    csv_file = tmp_path / "f.csv"
    _write(template, "spec: { runtime: apptainer }\n")
    _write(csv_file, "name\nfoo\n\nbar\n")
    # Act
    paths = expand_params_file(template, csv_file, tmp_path / "out")
    # Assert
    assert [p.name for p in paths] == ["foo.yaml", "bar.yaml"]


@pytest.fixture
def expanded_once(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Run a single expansion and return (template, csv, out_dir)."""
    template = tmp_path / "t.yaml"
    csv_file = tmp_path / "f.csv"
    _write(template, "spec: { runtime: apptainer }\n")
    _write(csv_file, "name\nfoo\n")
    out = tmp_path / "out"
    expand_params_file(template, csv_file, out)
    return template, csv_file, out


def test_expand_refuses_to_clobber_without_overwrite_flag(
    expanded_once: tuple[Path, Path, Path],
):
    # Arrange
    template, csv_file, out = expanded_once
    raiser = pytest.raises(FileExistsError)
    # Act
    # Assert
    with raiser:
        expand_params_file(template, csv_file, out)


def test_expand_overwrite_true_replaces_existing(
    expanded_once: tuple[Path, Path, Path],
):
    # Arrange
    template, csv_file, out = expanded_once
    target = out / "foo" / "foo.yaml"
    # Act
    expand_params_file(template, csv_file, out, overwrite=True)
    # Assert
    assert target.is_file()


def test_render_one_writes_single_instance_with_substitutions(tmp_path: Path):
    # Arrange
    template = tmp_path / "t.yaml"
    _write(template, "spec:\n  workdir: /tmp/${name}-${TASK}\n")
    # Act
    p = render_one(
        template,
        {"TASK": "smoke"},
        tmp_path / "out",
        name="ad-hoc-1",
    )
    # Assert
    assert p.read_text().strip() == "spec:\n  workdir: /tmp/ad-hoc-1-smoke"


def test_find_unsubstituted_vars_lists_unique_names_sorted():
    # Arrange
    s = "x ${A} y ${B} z ${A}"
    # Act
    result = find_unsubstituted_vars(s)
    # Assert
    assert result == ["A", "B"]


@pytest.fixture
def csv_with_blanks_and_whitespace(tmp_path: Path) -> list[dict[str, str]]:
    """A CSV row set with a blank line and leading whitespace in values."""
    csv_file = tmp_path / "f.csv"
    _write(csv_file, "name,X\n foo, 1\n\nbar, 2\n")
    return read_csv_rows(csv_file)


def test_read_csv_rows_skips_blank_lines(
    csv_with_blanks_and_whitespace: list[dict[str, str]],
):
    # Arrange
    rows = csv_with_blanks_and_whitespace
    # Act
    names = [r["name"] for r in rows]
    # Assert
    assert names == [" foo", "bar"]


def test_read_csv_rows_preserves_value_whitespace_verbatim(
    csv_with_blanks_and_whitespace: list[dict[str, str]],
):
    """Template substitution is exact, so CSV values pass through verbatim."""
    # Arrange
    rows = csv_with_blanks_and_whitespace
    # Act
    x_value = rows[0]["X"]
    # Assert
    assert x_value == " 1"


# ---------------------------------------------------------------------------
# CLI surface (sac agent start --params-file)
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_dry_run_result(tmp_path: Path, env_save_restore, pg_schema: str):
    """Invoke ``sac agent start --params-file --dry-run`` once.

    DEPENDS ON ``pg_schema`` since 2026-08-28, and the reason is worth stating
    because it is surprising: a ``--dry-run`` still reaches
    ``resolve_a2a_port`` -> ``claim_port``, so it really does take an a2a port
    claim. That was already true under SQLite — the claim just landed in the
    per-test ``state.db`` and nothing said so. Now the ledger is PostgreSQL and
    an unreachable store makes the dry run exit 1, which is what surfaced it.
    Requesting the fixture isolates the claim; that the dry run mutates state
    at all is pre-existing behaviour and out of scope here.

    The ``start`` command's preflight reads ``$HOME/.claude/.credentials.json``
    on the actual-dispatch path; ``--dry-run`` still ends up exercising
    that branch via the per-target loop, so we pin ``$HOME`` at the
    test's ``tmp_path`` and install a fresh OAuth credentials file there
    so CI runners (which have no such file) don't short-circuit before
    the materialisation step we care about.
    """
    import json
    import time

    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.delete("ANTHROPIC_API_KEY")
    env_save_restore.delete("SAC_ANTHROPIC_API_KEY")
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    expires_at_ms = int((time.time() + 3600) * 1000)
    (claude_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat-fake",
                    "refreshToken": "sk-ant-ort-fake",
                    "expiresAt": expires_at_ms,
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                }
            }
        ),
        encoding="utf-8",
    )

    from tests.scitex_agent_container._helpers.explicit_spec import (
        explicitize_yaml,
    )

    template = tmp_path / "template.yaml"
    csv_file = tmp_path / "fleet.csv"
    # Red-start ruling 2026-07-21: every field explicit (body wins). The
    # merge introduces no dollar-brace tokens, so expand's leftover check
    # stays quiet.
    _write(
        template,
        explicitize_yaml(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n  runtime: apptainer\n"
            # Empty host: (caller's host) — 'local' is banned; a placeholder
            # would trip expand's leftover check, and a concrete foreign name
            # would make the post-expand `start --dry-run` fail loud as an
            # unregistered host.
            "  host:\n"
            "  workdir: /tmp/${name}\n"
            "  apptainer:\n    image: /x.sif\n    binds: []\n"
            "  claude:\n    model: sonnet\n"
            "  health:\n    enabled: true\n    interval: 60\n"
            "  restart:\n    policy: on-failure\n    max_retries: 3\n"
        ),
    )
    _write(csv_file, "name\nalpha\nbeta\n")
    out_dir = tmp_path / "expanded"

    from click.testing import CliRunner

    from scitex_agent_container.cli_pkg.lifecycle import start

    runner = CliRunner()
    result = runner.invoke(
        start,
        [
            str(template),
            "--params-file",
            str(csv_file),
            "--params-out",
            str(out_dir),
            "--dry-run",
        ],
    )
    return result, out_dir


def test_start_params_file_dry_run_exits_zero(cli_dry_run_result):
    # Arrange
    result, _ = cli_dry_run_result
    # Act
    exit_code = result.exit_code
    # Assert
    assert exit_code == 0, result.output


@pytest.mark.parametrize("agent_name", ["alpha", "beta"])
def test_start_params_file_dry_run_materialises_yaml(
    cli_dry_run_result, agent_name: str
):
    """End-to-end: --params-file + --dry-run materialises N yamls and
    runs the existing dry-run path against each. We only assert on the
    materialised files (no live spawn) so the test is hermetic."""
    # Arrange
    _, out_dir = cli_dry_run_result
    # Act
    target = out_dir / agent_name / f"{agent_name}.yaml"
    # Assert
    assert target.is_file()


@pytest.fixture
def cli_two_targets_result(tmp_path: Path):
    """Invoke start with two TARGET args — this should fail."""
    template = tmp_path / "t.yaml"
    csv_file = tmp_path / "f.csv"
    _write(template, "spec: { runtime: apptainer }\n")
    _write(csv_file, "name\nfoo\n")

    from click.testing import CliRunner

    from scitex_agent_container.cli_pkg.lifecycle import start

    runner = CliRunner()
    return runner.invoke(
        start,
        [str(template), str(template), "--params-file", str(csv_file)],
    )


def test_start_params_file_with_two_targets_exits_with_usage_error(
    cli_two_targets_result,
):
    # Arrange
    result = cli_two_targets_result
    # Act
    exit_code = result.exit_code
    # Assert
    assert exit_code == 2


def test_start_params_file_with_two_targets_reports_target_arity(
    cli_two_targets_result,
):
    # Arrange
    result = cli_two_targets_result
    combined = result.output + (result.stderr or "")
    # Act
    has_message = "exactly one TARGET" in combined
    # Assert
    assert has_message
