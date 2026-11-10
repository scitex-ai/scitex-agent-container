"""Creation previews report arguments without qualifying or writing a scaffold."""

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._create import create
from scitex_agent_container.runtimes._fleet_env import HOST_PROCESS_AGENT_NAME


def _files(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_create_preview_redacts_values_and_preserves_existing_files(tmp_path):
    # Arrange
    base = tmp_path / "agents"
    existing = base / "existing" / "spec.yaml"
    existing.parent.mkdir(parents=True)
    existing.write_text("owned spec must not be replaced", encoding="utf-8")
    before = _files(tmp_path)
    args = [
        "existing",
        "--dry-run",
        "--base-dir",
        str(base),
        "--template",
        "not-checked",
        "--set",
        "token=owned-secret",
        "--set",
        "OTHER=another-secret",
    ]

    # Act
    result = CliRunner().invoke(create, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        result.stderr,
        plan["scope"],
        plan["dry_run"],
        plan["spec_path"],
        plan["template"],
        plan["set_keys"],
        "owned-secret" in result.output,
        "another-secret" in result.output,
        "destination state and overwrite refusal" in plan["unverified"],
        _files(tmp_path),
    ) == (
        0,
        "",
        "arguments_only",
        True,
        str(existing),
        "not-checked",
        ["OTHER", "TOKEN"],
        False,
        False,
        True,
        before,
    )


def test_create_preview_leaves_default_root_unresolved(tmp_path):
    # Arrange
    env = {
        "HOME": str(tmp_path),
        "SCITEX_DIR": str(tmp_path / "state"),
        "SAC_AGENT_SCOPE": "invalid-if-resolver-is-called",
    }
    before = _files(tmp_path)

    # Act
    result = CliRunner().invoke(create, ["owned", "--dry-run"], env=env)
    plan = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        plan["base_dir"],
        plan["spec_path"],
        _files(tmp_path),
    ) == (0, None, "<primary-agents-root>/owned/spec.yaml", before)


@pytest.mark.parametrize("pair", ["owned-secret", "=owned-secret"])
def test_create_preview_refuses_malformed_set_without_echoing_value(tmp_path, pair):
    # Arrange
    args = ["owned", "--dry-run", "--base-dir", str(tmp_path), "--set", pair]

    # Act
    result = CliRunner().invoke(create, args)

    # Assert
    assert (
        result.exit_code,
        "non-empty KEY=VALUE" in result.stderr,
        "owned-secret" in result.output,
        _files(tmp_path),
    ) == (2, True, False, {})


@pytest.mark.parametrize(
    ("name", "reason"),
    [("bad/name", "Invalid agent name"), (HOST_PROCESS_AGENT_NAME, "reserved")],
)
def test_create_preview_retains_name_admission(tmp_path, name, reason):
    # Arrange
    args = [name, "--dry-run", "--base-dir", str(tmp_path)]

    # Act
    result = CliRunner().invoke(create, args)

    # Assert
    assert (result.exit_code, reason in result.stderr, _files(tmp_path)) == (
        2,
        True,
        {},
    )
