"""Tests for ``sac image`` group — build / sandbox / freeze / list / status / snapshot."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg import image_group as ig
from scitex_agent_container.cli_pkg.image_group import image_group


@pytest.fixture(autouse=True)
def sandbox_paths(tmp_path, monkeypatch):
    """Redirect _CONTAINERS_DIR + Path.home() to tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    containers = home / ".scitex" / "agent-container" / "containers"
    monkeypatch.setattr(ig, "_CONTAINERS_DIR", containers)
    # Stub the bootstrap call so it doesn't write a .gitignore in real tree.
    bootstrap = types.ModuleType("scitex_agent_container._state._bootstrap")
    bootstrap.ensure_root_gitignore = lambda path: None
    monkeypatch.setitem(
        sys.modules, "scitex_agent_container._state._bootstrap", bootstrap
    )
    return tmp_path


def _install_fake_apptainer(monkeypatch, **fns):
    mod = types.ModuleType("scitex_container.apptainer")
    mod.build = fns.get("build", MagicMock(return_value=Path("/tmp/out.sif")))
    mod.sandbox_create = fns.get(
        "sandbox_create", MagicMock(return_value=Path("/tmp/sandbox"))
    )
    mod.sandbox_update = fns.get(
        "sandbox_update", MagicMock(return_value={"updated": ["scitex"]})
    )
    mod.sandbox_to_sif = fns.get(
        "sandbox_to_sif", MagicMock(return_value=Path("/tmp/frozen.sif"))
    )
    mod.switch_version = fns.get("switch_version", MagicMock())
    mod.rollback = fns.get("rollback", MagicMock(return_value="2.0.0"))
    mod.status = fns.get("status", MagicMock(return_value=[]))
    monkeypatch.setitem(sys.modules, "scitex_container.apptainer", mod)
    # parent package
    if "scitex_container" not in sys.modules:
        parent = types.ModuleType("scitex_container")
        parent.apptainer = mod
        parent.env_snapshot = fns.get(
            "env_snapshot", MagicMock(return_value={"pip": []})
        )
        monkeypatch.setitem(sys.modules, "scitex_container", parent)
    else:
        sys.modules["scitex_container"].env_snapshot = fns.get(
            "env_snapshot",
            getattr(
                sys.modules["scitex_container"],
                "env_snapshot",
                MagicMock(return_value={}),
            ),
        )
    return mod


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def test_build_dry_run():
    runner = CliRunner()
    result = runner.invoke(image_group, ["build", "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.output


def test_build_unknown_layer_fails():
    runner = CliRunner()
    result = runner.invoke(image_group, ["build", "unknown-layer"])
    assert result.exit_code != 0
    # click.Choice gives "Invalid value"
    assert "Invalid value" in result.output or "Usage" in result.output


def test_build_refuses_without_yes(monkeypatch):
    _install_fake_apptainer(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(image_group, ["build", "base"])
    assert result.exit_code == 2
    assert "Refusing" in result.output


def test_build_existing_artifact_warning(monkeypatch, sandbox_paths):
    _install_fake_apptainer(monkeypatch)
    # Pre-create existing SIF to trip the warning branch.
    out_dir = ig._CONTAINERS_DIR / "sac-base"
    out_dir.mkdir(parents=True)
    (out_dir / "sac-base.sif").write_bytes(b"x" * 100)
    runner = CliRunner()
    result = runner.invoke(image_group, ["build", "base", "--dry-run"])
    assert result.exit_code == 0
    assert "Existing" in result.output


def test_build_sandbox_existing_warning(monkeypatch, sandbox_paths):
    _install_fake_apptainer(monkeypatch)
    out_dir = ig._CONTAINERS_DIR / "sac-base"
    out_dir.mkdir(parents=True)
    (out_dir / "sac-base.sandbox").mkdir()
    runner = CliRunner()
    result = runner.invoke(image_group, ["build", "base", "--sandbox", "--dry-run"])
    assert result.exit_code == 0
    assert "sandbox dir" in result.output


def test_build_missing_recipe(monkeypatch, sandbox_paths):
    _install_fake_apptainer(monkeypatch)
    monkeypatch.setattr(ig, "_RECIPES_DIR", sandbox_paths / "no-recipes")
    runner = CliRunner()
    result = runner.invoke(image_group, ["build", "base", "--yes"])
    assert result.exit_code == 1
    assert "recipe not found" in result.output


def test_build_success(monkeypatch, sandbox_paths):
    # Real recipe dir exists in the wheel; build() is mocked.
    fake_build = MagicMock(return_value=Path("/tmp/sac-base.sif"))
    _install_fake_apptainer(monkeypatch, build=fake_build)
    runner = CliRunner()
    result = runner.invoke(image_group, ["build", "base", "--yes"])
    assert result.exit_code == 0, result.output
    assert "built" in result.output
    fake_build.assert_called_once()


def test_build_apptainer_failure(monkeypatch):
    def boom(**k):
        raise RuntimeError("apptainer broken")

    _install_fake_apptainer(monkeypatch, build=boom)
    runner = CliRunner()
    result = runner.invoke(image_group, ["build", "base", "--yes"])
    assert result.exit_code == 1
    assert "apptainer build failed" in result.output


# ---------------------------------------------------------------------------
# sandbox
# ---------------------------------------------------------------------------


def test_sandbox_from_layer_name(monkeypatch, sandbox_paths):
    fake = MagicMock(return_value=Path("/tmp/sandbox-out"))
    _install_fake_apptainer(monkeypatch, sandbox_create=fake)
    # Pre-place a SIF the resolver can find.
    ig._CONTAINERS_DIR.mkdir(parents=True, exist_ok=True)
    (ig._CONTAINERS_DIR / "apptainer-base.sif").write_bytes(b"sif")
    runner = CliRunner()
    result = runner.invoke(image_group, ["sandbox", "base"])
    assert result.exit_code == 0, result.output
    assert "sandbox" in result.output
    fake.assert_called_once()


def test_sandbox_from_path(monkeypatch, sandbox_paths):
    _install_fake_apptainer(monkeypatch)
    sif = sandbox_paths / "some.sif"
    sif.write_bytes(b"x")
    runner = CliRunner()
    result = runner.invoke(image_group, ["sandbox", str(sif)])
    assert result.exit_code == 0, result.output


def test_sandbox_layer_missing_sif(monkeypatch, sandbox_paths):
    _install_fake_apptainer(monkeypatch)
    ig._CONTAINERS_DIR.mkdir(parents=True, exist_ok=True)
    runner = CliRunner()
    result = runner.invoke(image_group, ["sandbox", "base"])
    assert result.exit_code != 0
    assert "Build it first" in result.output


def test_sandbox_unknown_source(monkeypatch):
    _install_fake_apptainer(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(image_group, ["sandbox", "totally-bogus-name"])
    assert result.exit_code != 0
    assert "neither a path nor a known layer" in result.output


# ---------------------------------------------------------------------------
# update / freeze
# ---------------------------------------------------------------------------


def test_update_default_packages(monkeypatch, tmp_path):
    fake = MagicMock(return_value={"upgraded": ["scitex"]})
    _install_fake_apptainer(monkeypatch, sandbox_update=fake)
    sb = tmp_path / "sb"
    sb.mkdir()
    runner = CliRunner()
    result = runner.invoke(image_group, ["update", str(sb)])
    assert result.exit_code == 0, result.output
    assert "scitex" in result.output
    args, kwargs = fake.call_args
    assert kwargs["packages"] == ("scitex[all]",)


def test_update_specific_packages(monkeypatch, tmp_path):
    fake = MagicMock(return_value={})
    _install_fake_apptainer(monkeypatch, sandbox_update=fake)
    sb = tmp_path / "sb"
    sb.mkdir()
    runner = CliRunner()
    result = runner.invoke(
        image_group, ["update", str(sb), "-p", "numpy", "-p", "scipy"]
    )
    assert result.exit_code == 0, result.output
    assert fake.call_args.kwargs["packages"] == ("numpy", "scipy")


def test_freeze(monkeypatch, tmp_path):
    fake = MagicMock(return_value=Path("/tmp/frozen.sif"))
    _install_fake_apptainer(monkeypatch, sandbox_to_sif=fake)
    sb = tmp_path / "sb"
    sb.mkdir()
    out = tmp_path / "out.sif"
    runner = CliRunner()
    result = runner.invoke(image_group, ["freeze", str(sb), str(out)])
    assert result.exit_code == 0, result.output
    assert "frozen" in result.output
    fake.assert_called_once()


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_empty(monkeypatch, sandbox_paths):
    _install_fake_apptainer(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(image_group, ["list"])
    assert result.exit_code == 0
    assert "no SIFs" in result.output or "containers dir" in result.output


def test_list_with_sif_and_sandbox(monkeypatch, sandbox_paths):
    _install_fake_apptainer(monkeypatch)
    ig._CONTAINERS_DIR.mkdir(parents=True, exist_ok=True)
    sif = ig._CONTAINERS_DIR / "scitex-agent-container-1.0.0.sif"
    sif.write_bytes(b"x" * 100)
    sb = ig._CONTAINERS_DIR / "scitex-agent-container-2.0.0.sandbox"
    sb.mkdir()
    (sb / "f.txt").write_bytes(b"y" * 100)
    runner = CliRunner()
    result = runner.invoke(image_group, ["list"])
    assert result.exit_code == 0, result.output
    assert "1.0.0" in result.output
    assert "2.0.0" in result.output


def test_list_json(monkeypatch, sandbox_paths):
    _install_fake_apptainer(monkeypatch)
    ig._CONTAINERS_DIR.mkdir(parents=True, exist_ok=True)
    (ig._CONTAINERS_DIR / "scitex-agent-container-1.0.0.sif").write_bytes(b"x")
    runner = CliRunner()
    result = runner.invoke(image_group, ["list", "--json"])
    assert result.exit_code == 0, result.output
    # rich output may surround JSON; find the JSON line
    payload_line = [l for l in result.output.splitlines() if l.startswith("[")]
    # JSON might span multiple lines
    start = result.output.index("[")
    end = result.output.rindex("]") + 1
    data = json.loads(result.output[start:end])
    assert len(data) == 1
    assert data[0]["kind"] == "sif"


# ---------------------------------------------------------------------------
# switch / rollback / status / snapshot
# ---------------------------------------------------------------------------


def test_switch(monkeypatch):
    fake = MagicMock()
    _install_fake_apptainer(monkeypatch, switch_version=fake)
    runner = CliRunner()
    result = runner.invoke(image_group, ["switch", "2.0.0"])
    assert result.exit_code == 0, result.output
    fake.assert_called_once()
    assert "switched" in result.output


def test_rollback(monkeypatch):
    _install_fake_apptainer(monkeypatch, rollback=MagicMock(return_value="1.0.0"))
    runner = CliRunner()
    result = runner.invoke(image_group, ["rollback"])
    assert result.exit_code == 0, result.output
    assert "1.0.0" in result.output


def test_status_empty(monkeypatch):
    _install_fake_apptainer(monkeypatch, status=MagicMock(return_value=[]))
    runner = CliRunner()
    result = runner.invoke(image_group, ["status"])
    assert result.exit_code == 0
    assert "no containers" in result.output


def test_status_with_entries(monkeypatch):
    entries = [
        {"name": "alpha", "sif_size": "100MB", "needs_rebuild": False},
        {"name": "beta", "sif_size": "200MB", "needs_rebuild": True},
    ]
    _install_fake_apptainer(monkeypatch, status=MagicMock(return_value=entries))
    runner = CliRunner()
    result = runner.invoke(image_group, ["status"])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "REBUILD" in result.output


def test_status_json(monkeypatch):
    entries = [{"name": "a", "sif_size": "1MB", "needs_rebuild": False}]
    _install_fake_apptainer(monkeypatch, status=MagicMock(return_value=entries))
    runner = CliRunner()
    result = runner.invoke(image_group, ["status", "--json"])
    assert result.exit_code == 0
    start = result.output.index("[")
    end = result.output.rindex("]") + 1
    data = json.loads(result.output[start:end])
    assert data == entries


def test_snapshot_stdout(monkeypatch):
    fake_snap = MagicMock(return_value={"pip": ["scitex==1.0"]})
    _install_fake_apptainer(monkeypatch, env_snapshot=fake_snap)
    runner = CliRunner()
    result = runner.invoke(image_group, ["snapshot"])
    assert result.exit_code == 0, result.output
    assert "scitex==1.0" in result.output


def test_snapshot_to_file(monkeypatch, tmp_path):
    fake_snap = MagicMock(return_value={"foo": "bar"})
    _install_fake_apptainer(monkeypatch, env_snapshot=fake_snap)
    out = tmp_path / "snap.json"
    runner = CliRunner()
    result = runner.invoke(image_group, ["snapshot", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.is_file()
    assert json.loads(out.read_text()) == {"foo": "bar"}
    assert "wrote" in result.output


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_resolve_def_name_unknown():
    runner = CliRunner()
    # Direct call to make sure UsageError path is hit.
    with pytest.raises(Exception):
        ig._resolve_def_name("nope")


def test_resolve_source_to_sif_layer_no_sif(monkeypatch, sandbox_paths):
    ig._CONTAINERS_DIR.mkdir(parents=True, exist_ok=True)
    with pytest.raises(Exception):
        ig._resolve_source_to_sif("base")
