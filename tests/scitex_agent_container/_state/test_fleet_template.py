"""Tests for scitex_agent_container._state.fleet_template (F-CS2)."""

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
  runtime: docker
  workdir: /tmp/${name}-workdir
  startup_commands:
    - command: "Run capsule ${CAPSULE_ID} on ${PROJECT}."
"""


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_expand_materialises_one_yaml_per_csv_row(tmp_path: Path):
    template = tmp_path / "template.yaml"
    csv_file = tmp_path / "fleet.csv"
    _write(template, _TEMPLATE)
    _write(
        csv_file,
        "name,PROJECT,CAPSULE_ID\ncap-aa-1,paper-x,aa-1\ncap-aa-2,paper-x,aa-2\n",
    )
    out = tmp_path / "out"
    paths = expand_params_file(template, csv_file, out)
    assert [p.name for p in paths] == ["cap-aa-1.yaml", "cap-aa-2.yaml"]
    rendered = paths[0].read_text()
    assert "${" not in rendered
    assert "/tmp/cap-aa-1-workdir" in rendered
    assert "project: paper-x" in rendered


def test_expand_substitutes_name_token(tmp_path: Path):
    """The 'name' column is also exposed as ``${name}`` in templates."""
    template = tmp_path / "t.yaml"
    csv_file = tmp_path / "f.csv"
    _write(template, "spec:\n  workdir: /tmp/${name}\n")
    _write(csv_file, "name\nfoo\n")
    paths = expand_params_file(template, csv_file, tmp_path / "out")
    assert "/tmp/foo" in paths[0].read_text()


def test_expand_rejects_unresolved_placeholder(tmp_path: Path):
    template = tmp_path / "t.yaml"
    csv_file = tmp_path / "f.csv"
    _write(template, "spec: { workdir: /tmp/${MISSING_VAR} }\n")
    _write(csv_file, "name\nfoo\n")
    with pytest.raises(ValueError, match="MISSING_VAR"):
        expand_params_file(template, csv_file, tmp_path / "out")


def test_expand_rejects_missing_name_column(tmp_path: Path):
    template = tmp_path / "t.yaml"
    csv_file = tmp_path / "f.csv"
    _write(template, "spec: { runtime: docker }\n")
    _write(csv_file, "PROJECT,CAPSULE_ID\np,c\n")
    with pytest.raises(ValueError, match="name"):
        expand_params_file(template, csv_file, tmp_path / "out")


def test_expand_rejects_duplicate_names(tmp_path: Path):
    template = tmp_path / "t.yaml"
    csv_file = tmp_path / "f.csv"
    _write(template, "spec: { runtime: docker }\n")
    _write(csv_file, "name\nfoo\nfoo\n")
    with pytest.raises(ValueError, match="duplicate"):
        expand_params_file(template, csv_file, tmp_path / "out")


def test_expand_skips_blank_rows(tmp_path: Path):
    template = tmp_path / "t.yaml"
    csv_file = tmp_path / "f.csv"
    _write(template, "spec: { runtime: docker }\n")
    _write(csv_file, "name\nfoo\n\nbar\n")
    paths = expand_params_file(template, csv_file, tmp_path / "out")
    assert [p.name for p in paths] == ["foo.yaml", "bar.yaml"]


def test_expand_overwrite_protects_by_default(tmp_path: Path):
    template = tmp_path / "t.yaml"
    csv_file = tmp_path / "f.csv"
    _write(template, "spec: { runtime: docker }\n")
    _write(csv_file, "name\nfoo\n")
    out = tmp_path / "out"
    expand_params_file(template, csv_file, out)
    with pytest.raises(FileExistsError):
        expand_params_file(template, csv_file, out)
    # Overwrite=True replaces.
    expand_params_file(template, csv_file, out, overwrite=True)


def test_render_one_writes_single_instance(tmp_path: Path):
    template = tmp_path / "t.yaml"
    _write(template, "spec:\n  workdir: /tmp/${name}-${TASK}\n")
    p = render_one(
        template,
        {"TASK": "smoke"},
        tmp_path / "out",
        name="ad-hoc-1",
    )
    assert p.read_text().strip() == "spec:\n  workdir: /tmp/ad-hoc-1-smoke"


def test_find_unsubstituted_vars_lists_unique_names():
    s = "x ${A} y ${B} z ${A}"
    assert find_unsubstituted_vars(s) == ["A", "B"]


def test_read_csv_rows_strips_blanks(tmp_path: Path):
    csv_file = tmp_path / "f.csv"
    _write(csv_file, "name,X\n foo, 1\n\nbar, 2\n")
    rows = read_csv_rows(csv_file)
    assert [r["name"] for r in rows] == [" foo", "bar"]
    # Values pass through verbatim (template substitution is exact).
    assert rows[0]["X"] == " 1"


# ---------------------------------------------------------------------------
# CLI surface (sac agent start --params-file)
# ---------------------------------------------------------------------------


def test_start_params_file_expands_and_dry_runs(tmp_path: Path):
    """End-to-end: --params-file + --dry-run materialises N yamls and
    runs the existing dry-run path against each. We only assert on the
    materialised files (no live spawn) so the test is hermetic."""
    template = tmp_path / "template.yaml"
    csv_file = tmp_path / "fleet.csv"
    _write(
        template,
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n  runtime: docker\n"
        "  workdir: /tmp/${name}\n",
    )
    _write(csv_file, "name\nalpha\nbeta\n")
    out_dir = tmp_path / "expanded"

    from click.testing import CliRunner

    from scitex_agent_container.cli_pkg.lifecycle_cmds import start

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
    assert result.exit_code == 0, result.output
    assert (out_dir / "alpha" / "alpha.yaml").is_file()
    assert (out_dir / "beta" / "beta.yaml").is_file()


def test_start_params_file_requires_single_target(tmp_path: Path):
    template = tmp_path / "t.yaml"
    csv_file = tmp_path / "f.csv"
    _write(template, "spec: { runtime: docker }\n")
    _write(csv_file, "name\nfoo\n")

    from click.testing import CliRunner

    from scitex_agent_container.cli_pkg.lifecycle_cmds import start

    runner = CliRunner()
    result = runner.invoke(
        start,
        [str(template), str(template), "--params-file", str(csv_file)],
    )
    assert result.exit_code == 2
    assert "exactly one TARGET" in (result.output + (result.stderr or ""))
