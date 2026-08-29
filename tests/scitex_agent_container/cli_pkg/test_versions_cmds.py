"""CLI tests for ``sac versions`` (scitex-* version introspection).

PA-306: no mocks. ``CliRunner`` drives the real click command; the
base-image exec path runs a REAL fake ``apptainer`` binary on PATH (a
self-contained Python script branching on argv) against a real temp
containers dir. ``--base-only`` keeps the test off the live agent registry.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg import main
from scitex_agent_container.cli_pkg.versions_cmds import versions

_PIP_LIST = json.dumps(
    [
        {"name": "scitex", "version": "2.11.0"},
        {"name": "scitex-io", "version": "1.2.3"},
        {"name": "numpy", "version": "2.0.0"},
    ]
)


def _install_fake_apptainer(bin_dir: Path, env) -> None:
    """Fake ``apptainer`` whose ``pip list --format=json`` returns _PIP_LIST.

    ``cat <manifest>`` always exits 1 (no baked manifest) so the verb takes
    the live fallback — the shippable-today path.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "apptainer"
    body = (
        f"#!{sys.executable}\n"
        "import sys\n"
        "inner = sys.argv[3:]\n"
        "if 'list' in inner and '--format=json' in inner:\n"
        f"    sys.stdout.write({_PIP_LIST!r}); sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    script.write_text(body)
    script.chmod(0o755)
    env.set("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


@pytest.fixture
def containers_dir(tmp_path: Path, env_save_restore) -> Path:
    """A real containers dir with one base SIF + a fake apptainer on PATH."""
    (tmp_path / "sac-base.sif").write_text("")
    _install_fake_apptainer(tmp_path / "_bin", env_save_restore)
    return tmp_path


def test_json_emits_flat_contract_rows(containers_dir: Path):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(
        versions, ["--json", "--live", "--base-only", "--containers-dir", str(containers_dir)]
    )
    rows = json.loads(result.stdout)
    # Assert
    assert all(
        set(r) == {"agent", "layer", "image", "package", "version", "source"}
        for r in rows
    )


def test_json_base_rows_are_scitex_only(containers_dir: Path):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(
        versions, ["--live", "--base-only", "--containers-dir", str(containers_dir)]
    )
    packages = {r["package"] for r in json.loads(result.stdout)}
    # Assert
    assert packages == {"scitex", "scitex-io"}


def test_live_flag_tags_source_live(containers_dir: Path):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(
        versions, ["--live", "--base-only", "--containers-dir", str(containers_dir)]
    )
    sources = {r["source"] for r in json.loads(result.stdout)}
    # Assert
    assert sources == {"live"}


def test_table_output_shows_headers(containers_dir: Path):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(
        versions, ["--table", "--live", "--base-only", "--containers-dir", str(containers_dir)]
    )
    # Assert
    assert "PACKAGE" in result.output and "base-image" in result.output


def test_versions_registered_on_main_group(containers_dir: Path):
    # Arrange
    runner = CliRunner()
    # Act — resolve through the top-level LazyGroup, proving registration.
    result = runner.invoke(
        main,
        ["versions", "--live", "--base-only", "--containers-dir", str(containers_dir)],
    )
    # Assert
    assert result.exit_code == 0 and json.loads(result.stdout)
