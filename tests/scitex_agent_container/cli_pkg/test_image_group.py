"""Tests for ``sac image`` group — build / sandbox / freeze / list / status / snapshot.

No-mocks rewrite (PA-306). The previous version fabricated a
``scitex_container.apptainer`` module on ``sys.modules`` populated with
``MagicMock`` callables — fake-for-fake, untrustworthy. This version:

* exercises real filesystem code paths against ``tmp_path``-rooted
  ``$HOME`` (set via the ``HOME`` env var, no ``monkeypatch``),
* swaps the public backend loaders (``image_group._load_apptainer`` /
  ``image_group._load_env_snapshot``) for hand-rolled real callables
  that return a small, real-behaviour fake backend class — same
  save/restore pattern as ``test_channel_group``'s ``_swap_urlopen``,
* deletes tests whose only assertion was ``MagicMock.assert_called_once()``
  (mock-only behaviour, not real behaviour).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg import image_group as ig
from scitex_agent_container.cli_pkg.image_group import image_group

# ---------------------------------------------------------------------------
# Real-fake backend — small class with concrete return values + call log.
# Stands in for ``scitex_container.apptainer`` without ``MagicMock``.
# ---------------------------------------------------------------------------


class _FakeApptainerBackend:
    """Hand-rolled stand-in for ``scitex_container.apptainer``.

    Each method records ``(args, kwargs)`` into a per-name call log and
    returns the value configured at construction time. ``raises`` maps
    method name → exception instance to raise on call; this lets tests
    cover the real ``except`` branch in the CLI.
    """

    def __init__(
        self,
        *,
        build_result: Path | None = None,
        sandbox_create_result: Path | None = None,
        sandbox_update_result: dict | None = None,
        sandbox_to_sif_result: Path | None = None,
        rollback_result: str = "1.0.0",
        status_result: list | None = None,
        raises: dict[str, BaseException] | None = None,
    ) -> None:
        self.calls: dict[str, list[tuple[tuple, dict]]] = {}
        self._returns = {
            "build": build_result or Path("/tmp/out.sif"),
            "sandbox_create": sandbox_create_result or Path("/tmp/sandbox-out"),
            "sandbox_update": sandbox_update_result
            if sandbox_update_result is not None
            else {"updated": ["scitex"]},
            "sandbox_to_sif": sandbox_to_sif_result or Path("/tmp/frozen.sif"),
            "switch_version": None,
            "rollback": rollback_result,
            "status": status_result if status_result is not None else [],
        }
        self._raises = raises or {}

    def _record(self, name: str, args: tuple, kwargs: dict) -> Any:
        self.calls.setdefault(name, []).append((args, kwargs))
        if name in self._raises:
            raise self._raises[name]
        return self._returns[name]

    def build(self, *a, **kw):
        return self._record("build", a, kw)

    def sandbox_create(self, *a, **kw):
        return self._record("sandbox_create", a, kw)

    def sandbox_update(self, *a, **kw):
        return self._record("sandbox_update", a, kw)

    def sandbox_to_sif(self, *a, **kw):
        return self._record("sandbox_to_sif", a, kw)

    def switch_version(self, *a, **kw):
        return self._record("switch_version", a, kw)

    def rollback(self, *a, **kw):
        return self._record("rollback", a, kw)

    def status(self, *a, **kw):
        return self._record("status", a, kw)


@contextmanager
def _use_backend(backend: _FakeApptainerBackend) -> Iterator[_FakeApptainerBackend]:
    """Swap ``image_group._load_apptainer`` for a real loader returning ``backend``."""
    saved = ig._load_apptainer
    ig._load_apptainer = lambda: backend  # type: ignore[assignment]
    try:
        yield backend
    finally:
        ig._load_apptainer = saved  # type: ignore[assignment]


@contextmanager
def _use_env_snapshot(payload: dict) -> Iterator[list[tuple]]:
    """Swap ``image_group._load_env_snapshot`` with a real recording callable."""
    calls: list[tuple] = []

    def _fake_env_snapshot(*a, **kw):
        calls.append((a, kw))
        return payload

    saved = ig._load_env_snapshot
    ig._load_env_snapshot = lambda: _fake_env_snapshot  # type: ignore[assignment]
    try:
        yield calls
    finally:
        ig._load_env_snapshot = saved  # type: ignore[assignment]


@contextmanager
def _use_source_builder(
    *,
    result: Path | None = None,
    raises: BaseException | None = None,
) -> Iterator[list[tuple]]:
    """Swap ``image_group._build_layer_from_source`` for a real recording fake.

    Same save/restore pattern as ``_use_backend``: a hand-rolled callable
    (no MagicMock) records every invocation into the yielded list. If
    ``raises`` is provided, the fake raises that exception instead of
    returning ``result`` — covers the apptainer-failed code branch.
    """
    calls: list[tuple] = []

    def _fake_builder(*a, **kw):
        calls.append((a, kw))
        if raises is not None:
            raise raises
        return result or Path("/tmp/sac-fake.sif")

    saved = ig._build_layer_from_source
    ig._build_layer_from_source = _fake_builder  # type: ignore[assignment]
    try:
        yield calls
    finally:
        ig._build_layer_from_source = saved  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# tmp-rooted HOME so every command writes into ``tmp_path``.
# Real env var, real bootstrap, real ``.gitignore``. No monkeypatch.
# ---------------------------------------------------------------------------


@pytest.fixture
def home_tmp(tmp_path: Path) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    saved_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    saved_containers_dir = ig._CONTAINERS_DIR
    ig._CONTAINERS_DIR = home / ".scitex" / "agent-container" / "containers"  # type: ignore[assignment]
    try:
        yield tmp_path
    finally:
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        ig._CONTAINERS_DIR = saved_containers_dir  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def test_build_dry_run_prints_dry_run_marker_and_exits_zero(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["build", "--dry-run"])
    # Assert
    assert result.exit_code == 0 and "dry-run" in result.output


def test_build_unknown_layer_fails_with_click_choice_error(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["build", "unknown-layer"])
    # Assert
    assert result.exit_code != 0 and (
        "Invalid value" in result.output or "Usage" in result.output
    )


def test_build_refuses_to_build_base_without_yes_flag(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["build", "base"])
    # Assert
    assert result.exit_code == 2 and "Refusing" in result.output


def test_build_warns_when_existing_sif_would_be_overwritten(home_tmp):
    # Arrange
    out_dir = ig._CONTAINERS_DIR / "sac-base"
    out_dir.mkdir(parents=True)
    (out_dir / "sac-base.sif").write_bytes(b"x" * 100)
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["build", "base", "--dry-run"])
    # Assert
    assert result.exit_code == 0 and "Existing" in result.output


def test_build_warns_when_existing_sandbox_dir_would_be_overwritten(home_tmp):
    # Arrange
    out_dir = ig._CONTAINERS_DIR / "sac-base"
    out_dir.mkdir(parents=True)
    (out_dir / "sac-base.sandbox").mkdir()
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["build", "base", "--sandbox", "--dry-run"])
    # Assert
    assert result.exit_code == 0 and "sandbox dir" in result.output


def test_build_errors_when_recipe_def_file_is_missing(home_tmp):
    # Arrange — point _RECIPES_DIR at an empty real dir; no monkeypatch.
    saved_recipes = ig._RECIPES_DIR
    ig._RECIPES_DIR = home_tmp / "no-recipes"  # type: ignore[assignment]
    runner = CliRunner()
    try:
        # Act
        result = runner.invoke(image_group, ["build", "base", "--yes"])
    finally:
        ig._RECIPES_DIR = saved_recipes  # type: ignore[assignment]
    # Assert
    assert result.exit_code == 1 and "recipe not found" in result.output


def test_build_success_invokes_source_builder_and_prints_built_message(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/sac-base.sif")) as calls:
        result = runner.invoke(image_group, ["build", "base", "--yes"])
    # Assert
    assert result.exit_code == 0 and "built" in result.output and len(calls) == 1


def test_build_success_passes_layer_def_path_pkg_root_and_force(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/sac-base.sif")) as calls:
        runner.invoke(image_group, ["build", "base", "--yes"])
    # Assert
    kwargs = calls[0][1]
    assert (
        kwargs["layer"] == "base"
        and kwargs["force"] is True
        and kwargs["sandbox"] is False
        and kwargs["def_path"].name == "apptainer-base.def"
        and kwargs["pkg_root"].name == "scitex_agent_container"
    )


def test_build_sandbox_flag_forwarded_to_source_builder(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/sac-base.sandbox")) as calls:
        runner.invoke(image_group, ["build", "base", "--sandbox", "--yes"])
    # Assert
    assert calls[0][1]["sandbox"] is True


def test_build_reports_apptainer_failure_with_exit_code_1(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_source_builder(raises=RuntimeError("apptainer broken")):
        result = runner.invoke(image_group, ["build", "base", "--yes"])
    # Assert
    assert result.exit_code == 1 and "apptainer build failed" in result.output


# ---------------------------------------------------------------------------
# sandbox
# ---------------------------------------------------------------------------


def test_sandbox_from_layer_name_resolves_to_known_sif_and_calls_backend(home_tmp):
    # Arrange
    backend = _FakeApptainerBackend(sandbox_create_result=Path("/tmp/sandbox-out"))
    ig._CONTAINERS_DIR.mkdir(parents=True, exist_ok=True)
    (ig._CONTAINERS_DIR / "apptainer-base.sif").write_bytes(b"sif")
    runner = CliRunner()
    # Act
    with _use_backend(backend):
        result = runner.invoke(image_group, ["sandbox", "base"])
    # Assert
    assert (
        result.exit_code == 0
        and "sandbox" in result.output
        and len(backend.calls.get("sandbox_create", [])) == 1
    )


def test_sandbox_from_explicit_path_skips_layer_resolution(home_tmp):
    # Arrange
    backend = _FakeApptainerBackend()
    sif = home_tmp / "some.sif"
    sif.write_bytes(b"x")
    runner = CliRunner()
    # Act
    with _use_backend(backend):
        result = runner.invoke(image_group, ["sandbox", str(sif)])
    # Assert
    assert result.exit_code == 0


def test_sandbox_errors_when_layer_sif_not_built_yet(home_tmp):
    # Arrange
    backend = _FakeApptainerBackend()
    ig._CONTAINERS_DIR.mkdir(parents=True, exist_ok=True)
    runner = CliRunner()
    # Act
    with _use_backend(backend):
        result = runner.invoke(image_group, ["sandbox", "base"])
    # Assert
    assert result.exit_code != 0 and "Build it first" in result.output


def test_sandbox_errors_when_source_is_neither_path_nor_known_layer(home_tmp):
    # Arrange
    backend = _FakeApptainerBackend()
    runner = CliRunner()
    # Act
    with _use_backend(backend):
        result = runner.invoke(image_group, ["sandbox", "totally-bogus-name"])
    # Assert
    assert result.exit_code != 0 and "neither a path nor a known layer" in result.output


# ---------------------------------------------------------------------------
# update / freeze
# ---------------------------------------------------------------------------


def test_update_with_no_package_flag_defaults_to_scitex_all(tmp_path):
    # Arrange
    backend = _FakeApptainerBackend(sandbox_update_result={"upgraded": ["scitex"]})
    sb = tmp_path / "sb"
    sb.mkdir()
    runner = CliRunner()
    # Act
    with _use_backend(backend):
        result = runner.invoke(image_group, ["update", str(sb)])
    # Assert
    assert (
        result.exit_code == 0
        and "scitex" in result.output
        and backend.calls["sandbox_update"][0][1]["packages"] == ("scitex[all]",)
    )


def test_update_passes_explicit_packages_through_to_backend(tmp_path):
    # Arrange
    backend = _FakeApptainerBackend(sandbox_update_result={})
    sb = tmp_path / "sb"
    sb.mkdir()
    runner = CliRunner()
    # Act
    with _use_backend(backend):
        result = runner.invoke(
            image_group, ["update", str(sb), "-p", "numpy", "-p", "scipy"]
        )
    # Assert
    assert result.exit_code == 0 and backend.calls["sandbox_update"][0][1][
        "packages"
    ] == ("numpy", "scipy")


def test_freeze_calls_sandbox_to_sif_and_prints_frozen_marker(tmp_path):
    # Arrange
    backend = _FakeApptainerBackend(sandbox_to_sif_result=Path("/tmp/frozen.sif"))
    sb = tmp_path / "sb"
    sb.mkdir()
    out = tmp_path / "out.sif"
    runner = CliRunner()
    # Act
    with _use_backend(backend):
        result = runner.invoke(image_group, ["freeze", str(sb), str(out)])
    # Assert
    assert (
        result.exit_code == 0
        and "frozen" in result.output
        and len(backend.calls.get("sandbox_to_sif", [])) == 1
    )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_with_empty_containers_dir_reports_no_sifs(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["list"])
    # Assert
    assert result.exit_code == 0 and (
        "no SIFs" in result.output or "containers dir" in result.output
    )


def test_list_renders_both_sif_files_and_sandbox_dirs(home_tmp):
    # Arrange
    ig._CONTAINERS_DIR.mkdir(parents=True, exist_ok=True)
    sif = ig._CONTAINERS_DIR / "scitex-agent-container-1.0.0.sif"
    sif.write_bytes(b"x" * 100)
    sb = ig._CONTAINERS_DIR / "scitex-agent-container-2.0.0.sandbox"
    sb.mkdir()
    (sb / "f.txt").write_bytes(b"y" * 100)
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["list"])
    # Assert
    assert (
        result.exit_code == 0 and "1.0.0" in result.output and "2.0.0" in result.output
    )


def test_list_json_emits_kind_sif_for_sif_files(home_tmp):
    # Arrange
    ig._CONTAINERS_DIR.mkdir(parents=True, exist_ok=True)
    (ig._CONTAINERS_DIR / "scitex-agent-container-1.0.0.sif").write_bytes(b"x")
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["list", "--json"])
    start = result.output.index("[")
    end = result.output.rindex("]") + 1
    data = json.loads(result.output[start:end])
    # Assert
    assert result.exit_code == 0 and data[0]["kind"] == "sif"


# ---------------------------------------------------------------------------
# switch / rollback / status / snapshot
# ---------------------------------------------------------------------------


def test_switch_delegates_to_backend_and_reports_target_version(home_tmp):
    # Arrange
    backend = _FakeApptainerBackend()
    runner = CliRunner()
    # Act
    with _use_backend(backend):
        result = runner.invoke(image_group, ["switch", "2.0.0"])
    # Assert
    assert (
        result.exit_code == 0
        and "switched" in result.output
        and backend.calls["switch_version"][0][1]["version"] == "2.0.0"
    )


def test_rollback_prints_previous_version_returned_by_backend(home_tmp):
    # Arrange
    backend = _FakeApptainerBackend(rollback_result="1.0.0")
    runner = CliRunner()
    # Act
    with _use_backend(backend):
        result = runner.invoke(image_group, ["rollback"])
    # Assert
    assert result.exit_code == 0 and "1.0.0" in result.output


def test_status_with_empty_backend_payload_reports_no_containers(home_tmp):
    # Arrange
    backend = _FakeApptainerBackend(status_result=[])
    runner = CliRunner()
    # Act
    with _use_backend(backend):
        result = runner.invoke(image_group, ["status"])
    # Assert
    assert result.exit_code == 0 and "no containers" in result.output


def test_status_renders_rebuild_marker_for_entries_with_needs_rebuild_true(home_tmp):
    # Arrange
    entries = [
        {"name": "alpha", "sif_size": "100MB", "needs_rebuild": False},
        {"name": "beta", "sif_size": "200MB", "needs_rebuild": True},
    ]
    backend = _FakeApptainerBackend(status_result=entries)
    runner = CliRunner()
    # Act
    with _use_backend(backend):
        result = runner.invoke(image_group, ["status"])
    # Assert
    assert (
        result.exit_code == 0
        and "alpha" in result.output
        and "REBUILD" in result.output
    )


def test_status_json_passes_backend_payload_through_verbatim(home_tmp):
    # Arrange
    entries = [{"name": "a", "sif_size": "1MB", "needs_rebuild": False}]
    backend = _FakeApptainerBackend(status_result=entries)
    runner = CliRunner()
    # Act
    with _use_backend(backend):
        result = runner.invoke(image_group, ["status", "--json"])
    start = result.output.index("[")
    end = result.output.rindex("]") + 1
    data = json.loads(result.output[start:end])
    # Assert
    assert result.exit_code == 0 and data == entries


def test_snapshot_with_no_output_flag_writes_json_to_stdout(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_env_snapshot({"pip": ["scitex==1.0"]}):
        result = runner.invoke(image_group, ["snapshot"])
    # Assert
    assert result.exit_code == 0 and "scitex==1.0" in result.output


def test_snapshot_with_output_path_writes_json_file_and_prints_wrote(
    home_tmp, tmp_path
):
    # Arrange
    out = tmp_path / "snap.json"
    runner = CliRunner()
    # Act
    with _use_env_snapshot({"foo": "bar"}):
        result = runner.invoke(image_group, ["snapshot", "-o", str(out)])
    # Assert
    assert (
        result.exit_code == 0
        and out.is_file()
        and json.loads(out.read_text()) == {"foo": "bar"}
        and "wrote" in result.output
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_resolve_def_name_raises_for_unknown_layer():
    # Arrange
    bad_layer = "nope"

    # Act
    def _call():
        return ig._resolve_def_name(bad_layer)

    # Assert
    with pytest.raises(Exception):
        _call()


def test_resolve_source_to_sif_raises_when_layer_not_built(home_tmp):
    # Arrange
    ig._CONTAINERS_DIR.mkdir(parents=True, exist_ok=True)

    # Act
    def _call():
        return ig._resolve_source_to_sif("base")

    # Assert
    with pytest.raises(Exception):
        _call()


# ---------------------------------------------------------------------------
# _LAYERS registry — every registered layer must point at an in-repo .def
# that actually ships in the wheel (no ghost entries, no typo'd filenames).
# ---------------------------------------------------------------------------


def test_every_registered_layer_has_a_recipe_file_in_recipes_dir():
    # Arrange — _RECIPES_DIR is the package-relative containers/ dir
    # (wheel-shipped). Every value in _LAYERS must resolve to a real
    # file there, or `sac image build <layer> -y` will exit 1 at the
    # recipe-not-found gate.
    missing = [
        name for name, fn in ig._LAYERS.items() if not (ig._RECIPES_DIR / fn).is_file()
    ]
    # Act
    # (the comprehension above is the work; assertion below is the check)
    # Assert
    assert missing == []


def test_texlive_layer_is_registered_in_layers_dict():
    # Arrange — operator/lead agreed 2026-06-03 that the texlive SIF
    # ships as a sub-tool layer alongside base/scitex, built via
    # `sac image build texlive -y` (NOT a separate dotfiles-deploy
    # path). Pinning the registration here so a future refactor that
    # accidentally drops the entry trips a red test.
    # Act
    registered = "texlive" in ig._LAYERS
    # Assert
    assert registered is True


def test_texlive_layer_points_at_apptainer_texlive_def():
    # Arrange — naming convention is apptainer-<layer>.def for SSoT
    # consistency with base/scitex. This pins the filename so the
    # in-repo recipe move is observable in tests.
    # Act
    fn = ig._LAYERS.get("texlive")
    # Assert
    assert fn == "apptainer-texlive.def"


def test_build_texlive_dry_run_prints_layer_name(home_tmp):
    # Arrange — texlive must be accepted by the click.Choice validator
    # (regression guard for the _LAYERS extension).
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["build", "texlive", "--dry-run"])
    # Assert
    assert result.exit_code == 0 and "texlive" in result.output


def test_build_texlive_passes_apptainer_texlive_def_to_source_builder(home_tmp):
    # Arrange — verify the build path resolves the def-name correctly
    # end-to-end, not just the registry lookup. Catches a future bug
    # where _LAYERS is updated but the build verb is hard-coded to
    # base/scitex.
    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/sac-texlive.sif")) as calls:
        runner.invoke(image_group, ["build", "texlive", "--yes"])
    # Assert
    assert calls[0][1]["def_path"].name == "apptainer-texlive.def"


def test_build_texlive_warns_when_existing_sac_texlive_sif_would_be_overwritten(
    home_tmp,
):
    # Arrange — pre-place a fake existing artifact at the expected
    # path. The build verb computes the path as
    # _CONTAINERS_DIR / sac-<layer> / sac-<layer>.sif and warns BEFORE
    # the --yes gate; this pins the artifact-path naming so a refactor
    # of the convention can't silently break the operator's expected
    # ~/.scitex/agent-container/containers/sac-texlive/sac-texlive.sif.
    out_dir = ig._CONTAINERS_DIR / "sac-texlive"
    out_dir.mkdir(parents=True)
    (out_dir / "sac-texlive.sif").write_bytes(b"x" * 100)
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["build", "texlive", "--dry-run"])
    # Assert
    assert result.exit_code == 0 and "Existing" in result.output
