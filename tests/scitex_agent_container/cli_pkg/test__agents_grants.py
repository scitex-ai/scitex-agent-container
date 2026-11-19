"""``sac agents grants infer|check`` — the migration CLI, end to end.

A real spec in ``tmp_path`` with ``$HOME`` redirected there (so every host
source resolves inside the test), driven through click's ``CliRunner``.
No mocks; the render goes through the production materialisers.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container.cli_pkg._agents_grants import grants_group

_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: t
    sac-builtin: "off"
spec:
  runtime: tui
  host: ${{HOSTNAME}}
  workdir: /tmp/agt-work
  apptainer:
    image: /x.sif
    binds: []
  claude:
    model: claude-opus-4-8[1m]
"""


def _spec(tmp_path: Path, env_save_restore) -> Path:
    from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    path = home / ".scitex" / "agent-container" / "agents" / "agt" / "spec.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(explicitize_yaml(_SPEC), encoding="utf-8")
    return path


def test_infer_prints_a_loadable_grants_block(tmp_path: Path, env_save_restore) -> None:
    # Arrange
    spec = _spec(tmp_path, env_save_restore)
    render = tmp_path / "render"
    # Act
    result = CliRunner().invoke(
        grants_group, ["infer", str(spec), "--render-dir", str(render)]
    )
    # Assert
    assert "grants:" in result.output.splitlines()


def test_check_passes_for_inferred_block(tmp_path: Path, env_save_restore) -> None:
    # Arrange
    spec = _spec(tmp_path, env_save_restore)
    render = tmp_path / "render"
    # Act
    result = CliRunner().invoke(
        grants_group, ["check", str(spec), "--render-dir", str(render)]
    )
    # Assert
    assert (result.exit_code, "PASS (0 diff)" in result.output) == (0, True)


def test_check_json_reports_one_object_per_agent(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    spec = _spec(tmp_path, env_save_restore)
    render = tmp_path / "render"
    args = ["check", str(spec), "--json", "--render-dir", str(render)]
    # Act
    result = CliRunner().invoke(grants_group, args)
    # Assert
    assert '"passed": true' in result.output
