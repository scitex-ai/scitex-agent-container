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
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from click.testing import CliRunner

from _scitex_agent_container_bootstrap import (
    ImageBuildSourceMismatch,
    assert_image_build_source_authority,
)
from scitex_agent_container.cli_pkg import _image_activation
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
            "list_builds": status_result if status_result is not None else [],
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

    def list_builds(self, *a, **kw):
        entries = self._record("list_builds", a, kw)
        layer = str(a[1])
        return [
            entry
            for entry in entries
            if Path(str(entry.get("sif", ""))).parent.name == layer
        ]


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


@contextmanager
def _use_demoter(*, lines: list[str] | None = None) -> Iterator[list[dict]]:
    """Swap ``image_group._demote_build_priority`` for a recording fake.

    Same save/restore pattern as ``_use_source_builder``. The fake
    records each call's kwargs into the yielded list and returns
    ``lines`` (default: none) — it NEVER demotes, so the pytest process
    keeps its priority (real demotion is one-way; see
    tests/scitex_agent_container/test__build_priority.py for the real
    child-process behavior tests).
    """
    calls: list[dict] = []

    def _fake_demoter(**kw):
        calls.append(kw)
        return list(lines or [])

    saved = ig._demote_build_priority
    ig._demote_build_priority = _fake_demoter  # type: ignore[assignment]
    try:
        yield calls
    finally:
        ig._demote_build_priority = saved  # type: ignore[assignment]


@contextmanager
def _use_advisory(*, text: str | None = None) -> Iterator[list[dict]]:
    """Swap ``image_group._remote_build_advisory`` for a recording fake.

    Same save/restore pattern as ``_use_demoter``. The fake records each
    call's kwargs into the yielded list and returns ``text`` (default:
    ``None`` = host looks idle) so the decision never depends on the CI
    host's live loadavg (real threshold behavior is covered in
    tests/scitex_agent_container/test__build_priority.py).
    """
    calls: list[dict] = []

    def _fake_advisory(**kw):
        calls.append(kw)
        return text

    saved = ig._remote_build_advisory
    ig._remote_build_advisory = _fake_advisory  # type: ignore[assignment]
    try:
        yield calls
    finally:
        ig._remote_build_advisory = saved  # type: ignore[assignment]


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
    saved_state_root = ig._SCITEX_USER_STATE_ROOT
    ig._CONTAINERS_DIR = home / ".scitex" / "agent-container" / "containers"  # type: ignore[assignment]
    ig._SCITEX_USER_STATE_ROOT = home / ".scitex"  # type: ignore[assignment]
    try:
        yield tmp_path
    finally:
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        ig._CONTAINERS_DIR = saved_containers_dir  # type: ignore[assignment]
        ig._SCITEX_USER_STATE_ROOT = saved_state_root  # type: ignore[assignment]


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


@contextmanager
def _use_environment_package_root(root: Path):
    saved = ig._image_source_build._environment_package_root
    ig._image_source_build._environment_package_root = lambda: root
    try:
        yield
    finally:
        ig._image_source_build._environment_package_root = saved


def test_plain_build_refuses_mixed_source_provenance_before_builder(home_tmp):
    # Arrange
    selected_root = home_tmp / "selected-worktree" / "src" / "scitex_agent_container"
    selected_root.mkdir(parents=True)
    # Act
    with _use_environment_package_root(selected_root):
        with _use_source_builder(result=Path("/tmp/should-not-build.sif")) as calls:
            result = CliRunner().invoke(image_group, ["build", "base", "--yes"])
    # Assert
    assert (
        result.exit_code == 1
        and calls == []
        and "SAC source provenance is mixed" in result.output
        and "loaded package root:" in result.output
        and f"active-environment package root: {selected_root}" in result.output
    )


def test_reproducible_build_refuses_mixed_source_provenance_before_builder(home_tmp):
    # Arrange
    selected_root = home_tmp / "selected-worktree" / "src" / "scitex_agent_container"
    selected_root.mkdir(parents=True)
    # Act
    with _use_environment_package_root(selected_root):
        with _use_reproducible_builder() as calls:
            result = CliRunner().invoke(
                image_group, ["build", "base", "--yes", "--reproducible"]
            )

    # Assert
    assert (
        result.exit_code == 1
        and calls == []
        and "SAC source provenance is mixed" in result.output
        and "loaded build-helper root:" in result.output
        and f"active-environment package root: {selected_root}" in result.output
    )


def test_mixed_source_refusal_precedes_artifact_directory_creation(home_tmp):
    # Arrange
    selected_root = home_tmp / "selected-worktree" / "src" / "scitex_agent_container"
    selected_root.mkdir(parents=True)
    artifact_root = home_tmp / "must-not-be-created"
    saved_containers = ig._CONTAINERS_DIR
    ig._CONTAINERS_DIR = artifact_root
    try:
        # Act
        with _use_environment_package_root(selected_root):
            result = CliRunner().invoke(image_group, ["build", "base", "--yes"])
    finally:
        ig._CONTAINERS_DIR = saved_containers
    # Assert
    assert (result.exit_code, artifact_root.exists()) == (1, False)


def test_bootstrap_mismatch_names_both_roots_and_recovery_hint(home_tmp):
    # Arrange
    canonical = home_tmp / "canonical"
    expected = canonical / "src" / "scitex_agent_container"
    expected.mkdir(parents=True)
    stale = home_tmp / "stale" / "src" / "scitex_agent_container"
    stale.mkdir(parents=True)
    purelib = home_tmp / "venv" / "site-packages"
    dist_info = purelib / "scitex_agent_container-0.0.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: scitex-agent-container\nVersion: 0.0.0\n"
    )
    (dist_info / "direct_url.json").write_text(
        '{"url":"' + canonical.as_uri() + '","dir_info":{"editable":true}}'
    )
    # Act
    try:
        assert_image_build_source_authority(
            ["image", "build", "base", "-y"],
            metadata_paths=(purelib,),
            runtime_root=stale,
            working_dir=home_tmp,
        )
    except ImageBuildSourceMismatch as exc:
        message = str(exc)
    else:
        message = ""
    # Assert
    assert (
        "before filesystem or image mutation" in message,
        f"runtime-loaded package root: {stale}" in message,
        f"editable direct_url authority: {expected}" in message,
        "unset PYTHONPATH" in message,
    ) == (True, True, True, True)


def _sac_checkout(root: Path) -> Path:
    """Create the minimum real filesystem shape the bootstrap recognizes."""
    package = root / "src" / "scitex_agent_container"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    bootstrap = root / "src" / "_scitex_agent_container_bootstrap"
    bootstrap.mkdir()
    (bootstrap / "__init__.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "scitex-agent-container"\n', encoding="utf-8"
    )
    return package


def test_bootstrap_refuses_runtime_source_different_from_cwd_checkout(home_tmp):
    # Arrange — no editable dist-info is available. CWD is still an explicit
    # source selection when it is inside a recognizable SAC checkout.
    intended = _sac_checkout(home_tmp / "intended")
    stale = home_tmp / "stale" / "src" / "scitex_agent_container"
    stale.mkdir(parents=True)
    nested_cwd = intended.parents[1] / "tests" / "unit"
    nested_cwd.mkdir(parents=True)
    # Act
    try:
        assert_image_build_source_authority(
            ["image", "build", "base", "-y"],
            metadata_paths=(),
            runtime_root=stale,
            working_dir=nested_cwd,
        )
    except ImageBuildSourceMismatch as exc:
        message = str(exc)
    else:
        message = ""
    # Assert
    assert (
        "before filesystem or image mutation" in message,
        f"command-working-directory package root: {intended}" in message,
        f"runtime-loaded package root: {stale}" in message,
        "unset PYTHONPATH" in message,
    ) == (True, True, True, True)


def test_bootstrap_accepts_runtime_source_matching_cwd_checkout(home_tmp):
    # Arrange
    package = _sac_checkout(home_tmp / "selected")
    # Act
    result = assert_image_build_source_authority(
        ["image", "build", "base", "-y"],
        metadata_paths=(),
        runtime_root=package,
        working_dir=package.parents[1],
    )
    # Assert
    assert result is None


def test_bootstrap_does_not_treat_an_arbitrary_cwd_as_source_authority(home_tmp):
    # Arrange — image builds remain CWD-independent outside a SAC checkout.
    runtime = home_tmp / "installed" / "scitex_agent_container"
    runtime.mkdir(parents=True)
    elsewhere = home_tmp / "unrelated-project"
    elsewhere.mkdir()
    # Act
    result = assert_image_build_source_authority(
        ["image", "build", "base", "-y"],
        metadata_paths=(),
        runtime_root=runtime,
        working_dir=elsewhere,
    )
    # Assert
    assert result is None


def test_bootstrap_cwd_authority_does_not_mask_version_warning(home_tmp):
    # Arrange — the same mismatch must remain observable to `sac --version`;
    # the destructive-build guard has no authority to silence that command.
    intended = _sac_checkout(home_tmp / "intended")
    stale = home_tmp / "stale" / "src" / "scitex_agent_container"
    stale.mkdir(parents=True)
    # Act
    result = assert_image_build_source_authority(
        ["--version"],
        metadata_paths=(),
        runtime_root=stale,
        working_dir=intended.parents[1],
    )
    # Assert
    assert result is None


def test_bootstrap_shadow_process_stops_before_stale_cli_import(home_tmp):
    # Arrange
    canonical = home_tmp / "canonical"
    (canonical / "src" / "scitex_agent_container").mkdir(parents=True)
    purelib = home_tmp / "venv" / "site-packages"
    dist_info = purelib / "scitex_agent_container-0.0.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: scitex-agent-container\nVersion: 0.0.0\n"
    )
    (dist_info / "direct_url.json").write_text(
        '{"url":"' + canonical.as_uri() + '","dir_info":{"editable":true}}'
    )
    stale_src = home_tmp / "stale" / "src"
    stale_package = stale_src / "scitex_agent_container"
    stale_package.mkdir(parents=True)
    (stale_package / "__init__.py").write_text("")
    marker = home_tmp / "stale-cli-imported"
    (stale_package / "cli.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['SAC_BOOTSTRAP_TEST_MARKER']).write_text('mutated')\n"
    )
    repo_src = Path(__file__).resolve().parents[3] / "src"
    script = (
        "import sys\nfrom pathlib import Path\n"
        "from _scitex_agent_container_bootstrap import "
        "assert_image_build_source_authority\n"
        "assert_image_build_source_authority("
        "['image','build','base','-y'], metadata_paths=(Path(sys.argv[1]),))\n"
        "import scitex_agent_container.cli\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(stale_src), str(repo_src)))
    env["SAC_BOOTSTRAP_TEST_MARKER"] = str(marker)
    # Act
    result = subprocess.run(
        [sys.executable, "-c", script, str(purelib)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    # Assert
    assert (
        result.returncode,
        marker.exists(),
        "unset PYTHONPATH" in result.stderr,
    ) == (1, False, True)


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


@contextmanager
def _use_reproducible_builder(*, result=None) -> Iterator[list[dict]]:
    """Swap ``image_group._run_reproducible_build`` for a recording fake.

    Same save/restore pattern as ``_use_source_builder``. The round trip
    itself is covered in ``test__image_repro_build``; what matters here is
    that ``--reproducible`` ROUTES to it instead of the plain build.
    """
    calls: list[dict] = []

    def _fake(**kw):
        calls.append(kw)
        return result

    saved = ig._run_reproducible_build
    ig._run_reproducible_build = _fake  # type: ignore[assignment]
    try:
        yield calls
    finally:
        ig._run_reproducible_build = saved  # type: ignore[assignment]


def test_build_reproducible_routes_to_the_round_trip(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_reproducible_builder() as calls:
        runner.invoke(image_group, ["build", "base", "--yes", "--reproducible"])
    # Assert
    assert len(calls) == 1


def test_build_reproducible_does_not_call_the_plain_builder(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/x.sif")) as plain:
        with _use_reproducible_builder():
            runner.invoke(image_group, ["build", "base", "--yes", "--reproducible"])
    # Assert
    assert plain == []


def test_build_reproducible_verifies_by_default(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_reproducible_builder() as calls:
        runner.invoke(image_group, ["build", "base", "--yes", "--reproducible"])
    # Assert
    assert calls[0]["verify"] is True


def test_build_skip_verify_turns_the_replay_off(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_reproducible_builder() as calls:
        runner.invoke(
            image_group,
            ["build", "base", "--yes", "--reproducible", "--skip-verify"],
        )
    # Assert
    assert calls[0]["verify"] is False


def test_build_reproducible_with_sandbox_is_refused(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(
        image_group, ["build", "base", "--yes", "--reproducible", "--sandbox"]
    )
    # Assert
    assert result.exit_code == 2


def test_build_skip_verify_without_reproducible_is_refused(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["build", "base", "--yes", "--skip-verify"])
    # Assert
    assert result.exit_code == 2


def test_build_accepts_the_proxy_layer(home_tmp):
    # Arrange — proxy shipped a recipe that no layer mapping could reach
    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/sac-proxy.sif")) as calls:
        runner.invoke(image_group, ["build", "proxy", "--yes"])
    # Assert
    assert calls[0][1]["def_path"].name == "apptainer-proxy.def"


def test_build_reports_apptainer_failure_with_exit_code_1(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_source_builder(raises=RuntimeError("apptainer broken")):
        result = runner.invoke(image_group, ["build", "base", "--yes"])
    # Assert
    assert result.exit_code == 1 and "apptainer build failed" in result.output


def test_build_scitex_passes_bootstrap_sif_pointing_at_built_base_sif(home_tmp):
    # Arrange — the scitex layer's .def bootstraps off ``sac-base.sif``
    # at a path RELATIVE to the build-context dir. The CLI must resolve
    # the prerequisite SIF and forward it as ``bootstrap_sif`` so the
    # staging helper symlinks it next to the staged .def.
    base_dir = ig._CONTAINERS_DIR / "sac-base"
    base_dir.mkdir(parents=True)
    base_sif = base_dir / "sac-base.sif"
    base_sif.write_bytes(b"fake base SIF")
    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/sac-scitex.sif")) as calls:
        result = runner.invoke(image_group, ["build", "scitex", "--yes"])
    # Assert
    kwargs = calls[0][1]
    assert (
        result.exit_code == 0
        and kwargs["layer"] == "scitex"
        and kwargs["bootstrap_sif"] == base_sif
    )


def test_build_base_passes_none_bootstrap_sif(home_tmp):
    # Arrange — top-of-stack ``base`` .def has no prerequisite SIF;
    # CLI must NOT make one up (would dangle a stale symlink in the
    # staging dir).
    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/sac-base.sif")) as calls:
        runner.invoke(image_group, ["build", "base", "--yes"])
    # Assert
    assert calls[0][1]["bootstrap_sif"] is None


def test_build_scitex_errors_loud_when_base_sif_missing(home_tmp):
    # Arrange — the operator asked for scitex but never built (or
    # successfully overwrote) sac-base.sif first. The CLI must FAIL
    # LOUD before invoking the builder, with the exact remediation
    # command in the error text — not let apptainer FATAL on a half-
    # staged context (the 2026-06-07 cohort-A rebuild stall).
    runner = CliRunner()
    # Act — no _use_source_builder: the failure must short-circuit
    # before the builder is ever called.
    result = runner.invoke(image_group, ["build", "scitex", "--yes"])
    # Assert
    assert result.exit_code == 1 and "sac image build base" in result.output


# ---------------------------------------------------------------------------
# build — low-priority self-demotion (incident-local-heavy-build)
# ---------------------------------------------------------------------------


def test_build_default_calls_priority_demoter_with_skip_false(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/sac-base.sif")):
        with _use_demoter() as calls:
            runner.invoke(image_group, ["build", "base", "--yes"])
    # Assert — self-demotion is the DEFAULT (no flag needed).
    assert calls == [{"skip": False}]


def test_build_no_nice_flag_forwards_skip_true_to_demoter(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/sac-base.sif")):
        with _use_demoter() as calls:
            runner.invoke(image_group, ["build", "base", "--yes", "--no-nice"])
    # Assert — the explicit opt-out for dedicated build machines / CI.
    assert calls == [{"skip": True}]


def test_build_dry_run_never_calls_priority_demoter(home_tmp):
    # Arrange — a dry run does no heavy work, so it must not demote.
    runner = CliRunner()
    # Act
    with _use_demoter() as calls:
        runner.invoke(image_group, ["build", "--dry-run"])
    # Assert
    assert calls == []


def test_build_echoes_low_priority_notice_from_demoter(home_tmp):
    # Arrange — the loud one-line notice must land in the build output
    # so nobody is surprised by a slower build.
    from scitex_agent_container._build_priority import LOW_PRIORITY_NOTICE

    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/sac-base.sif")):
        with _use_demoter(lines=[LOW_PRIORITY_NOTICE]):
            result = runner.invoke(image_group, ["build", "base", "--yes"])
    # Assert
    assert LOW_PRIORITY_NOTICE in result.output


# ---------------------------------------------------------------------------
# build — remote-first load advisory (incident-local-heavy-build closure #3)
# ---------------------------------------------------------------------------


def test_build_consults_remote_advisory_before_heavy_work(home_tmp):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/sac-base.sif")):
        with _use_advisory() as calls:
            runner.invoke(image_group, ["build", "base", "--yes"])
    # Assert — consulted exactly once, with live introspection defaults.
    assert calls == [{}]


def test_build_proceeds_demoted_when_advisory_fires(home_tmp):
    # Arrange — the advisory is a WARNING, never a refusal: a loaded
    # host still gets its (demoted) build.
    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/sac-base.sif")):
        with _use_advisory(text="HOST ALREADY LOADED: prefer Spartan"):
            result = runner.invoke(image_group, ["build", "base", "--yes"])
    # Assert
    assert result.exit_code == 0 and "built" in result.output


def test_build_dry_run_never_consults_remote_advisory(home_tmp):
    # Arrange — a dry run does no heavy work, so no advisory either.
    runner = CliRunner()
    # Act
    with _use_advisory() as calls:
        runner.invoke(image_group, ["build", "--dry-run"])
    # Assert
    assert calls == []


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
    data = json.loads(result.stdout)
    # Assert
    assert result.exit_code == 0 and data[0]["kind"] == "sif"


def test_list_json_reports_a_dangling_sif_symlink(home_tmp):
    # Arrange
    ig._CONTAINERS_DIR.mkdir(parents=True, exist_ok=True)
    link = ig._CONTAINERS_DIR / "retired.sif"
    link.symlink_to(ig._CONTAINERS_DIR / "missing.sif")
    runner = CliRunner()

    # Act
    result = runner.invoke(image_group, ["list", "--json"])
    data = json.loads(result.stdout)

    # Assert
    assert result.exit_code == 0 and data == [
        {
            "package": "agent-container",
            "name": "retired.sif",
            "path": str(link),
            "kind": "sif",
            "size_bytes": 0,
            "mtime": link.lstat().st_mtime,
            "resolves_to": "missing.sif",
            "target_state": "dangling",
        }
    ]


def test_list_json_stdout_holds_nothing_but_the_document(home_tmp):
    # Arrange — `sac image list --json` used to print a human
    # "scan root: .../*/containers/" banner to STDOUT before the payload,
    # so `sac image list --json | jq` died on the very first byte. The
    # tests could not see it: they parsed from `result.output.index("[")`
    # onwards, and a prefix-skip cannot fail on a prefix.
    ig._CONTAINERS_DIR.mkdir(parents=True, exist_ok=True)
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["list", "--json"])
    # Assert — stdout is EXACTLY the document, first byte to last.
    assert result.stdout == "[]\n"


# ---------------------------------------------------------------------------
# switch / rollback / status / snapshot
# ---------------------------------------------------------------------------


def _install_layer_versions(
    containers: Path, layer: str, versions: tuple[str, ...]
) -> list[Path]:
    image_name = f"sac-{layer}"
    layer_dir = containers / image_name
    layer_dir.mkdir(parents=True)
    artifacts = []
    for index, version in enumerate(versions, start=1):
        artifact = layer_dir / f"{image_name}-{version}.sif"
        artifact.write_bytes(version.encode())
        os.utime(artifact, ns=(index, index))
        artifacts.append(artifact)
    current = artifacts[-1]
    (layer_dir / f"{image_name}.sif").symlink_to(current.name)
    (containers / f"{image_name}.sif").symlink_to(Path(image_name) / current.name)
    return artifacts


def test_switch_repoints_both_live_links_and_reports_layer(home_tmp):
    # Arrange
    # The production SAC layout, not scitex-container's legacy
    # current.sif / scitex-v<version>.sif convention.
    artifacts = _install_layer_versions(
        ig._CONTAINERS_DIR, "scitex", ("2026-0914-010000", "2026-0914-020000")
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(
        image_group,
        ["switch", "2026-0914-010000", "--layer", "scitex"],
    )
    # Assert
    inner = ig._CONTAINERS_DIR / "sac-scitex" / "sac-scitex.sif"
    top = ig._CONTAINERS_DIR / "sac-scitex.sif"
    actual = (
        result.exit_code,
        "switched scitex" in result.output,
        inner.resolve(),
        top.resolve(),
    )
    expected = (0, True, artifacts[0], artifacts[0])
    assert actual == expected


def test_rollback_activates_immediately_older_base_image(home_tmp):
    # Arrange
    artifacts = _install_layer_versions(
        ig._CONTAINERS_DIR, "base", ("2026-0914-010000", "2026-0914-020000")
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["rollback"])
    # Assert
    inner = ig._CONTAINERS_DIR / "sac-base" / "sac-base.sif"
    top = ig._CONTAINERS_DIR / "sac-base.sif"
    actual = (
        result.exit_code,
        "2026-0914-010000" in result.output,
        inner.resolve(),
        top.resolve(),
    )
    expected = (0, True, artifacts[0], artifacts[0])
    assert actual == expected


def test_rollback_fails_loudly_when_live_links_disagree(home_tmp):
    # Arrange
    artifacts = _install_layer_versions(
        ig._CONTAINERS_DIR, "base", ("2026-0914-010000", "2026-0914-020000")
    )
    top = ig._CONTAINERS_DIR / "sac-base.sif"
    top.unlink()
    top.symlink_to(Path("sac-base") / artifacts[0].name)

    # Act
    result = CliRunner().invoke(image_group, ["rollback", "--layer", "base"])

    # Assert
    actual = (
        result.exit_code != 0,
        isinstance(result.exception, RuntimeError),
        "links disagree" in str(result.exception),
        (ig._CONTAINERS_DIR / "sac-base" / "sac-base.sif").resolve(),
        top.resolve(),
    )
    expected = (True, True, True, artifacts[1], artifacts[0])
    assert actual == expected


def test_switch_rejects_path_traversal_version(home_tmp):
    # Arrange
    runner = CliRunner()

    # Act
    result = runner.invoke(image_group, ["switch", "../outside"])

    # Assert
    actual = (
        result.exit_code != 0,
        isinstance(result.exception, ValueError),
        "invalid SAC image version" in str(result.exception),
    )
    assert actual == (True, True, True)


def test_switch_restores_both_links_when_second_flip_fails(home_tmp):
    # Arrange
    artifacts = _install_layer_versions(
        ig._CONTAINERS_DIR, "base", ("2026-0914-010000", "2026-0914-020000")
    )
    inner = ig._CONTAINERS_DIR / "sac-base" / "sac-base.sif"
    top = ig._CONTAINERS_DIR / "sac-base.sif"
    saved_atomic_symlink = _image_activation._atomic_symlink

    def _fail_new_top_once(link: Path, target: str) -> None:
        if link == top and target.endswith("2026-0914-010000.sif"):
            raise OSError("injected second-link failure")
        saved_atomic_symlink(link, target)

    _image_activation._atomic_symlink = _fail_new_top_once
    # Act
    try:
        result = CliRunner().invoke(
            image_group, ["switch", "2026-0914-010000", "--layer", "base"]
        )
    finally:
        _image_activation._atomic_symlink = saved_atomic_symlink

    # Assert
    actual = (
        result.exit_code != 0,
        isinstance(result.exception, OSError),
        inner.resolve(),
        top.resolve(),
    )
    expected = (True, True, artifacts[1], artifacts[1])
    assert actual == expected


def test_status_with_no_active_build_reports_no_active_images(home_tmp):
    # Arrange
    backend = _FakeApptainerBackend(status_result=[])
    runner = CliRunner()
    # Act
    with _use_backend(backend):
        result = runner.invoke(image_group, ["status"])
    # Assert
    assert result.exit_code == 0 and "no active SAC images" in result.output


def test_status_renders_active_current_layout_image(home_tmp):
    # Arrange
    artifact = ig._CONTAINERS_DIR / "sac-base" / "sac-base-2026-0912-140710.sif"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"x" * 1024)
    entries = [
        {
            "ts": "2026-0912-140710",
            "sif": str(artifact),
            "verified": True,
            "active": True,
        }
    ]
    backend = _FakeApptainerBackend(status_result=entries)
    runner = CliRunner()
    # Act
    with _use_backend(backend):
        result = runner.invoke(image_group, ["status"])
    # Assert
    assert (
        result.exit_code == 0
        and "sac-base" in result.output
        and "verified" in result.output
        and "2026-0912-140710" in result.output
    )


def test_status_discovers_real_current_artifact_store_layout(home_tmp):
    # Arrange
    layer_dir = ig._CONTAINERS_DIR / "sac-base"
    layer_dir.mkdir(parents=True)
    artifact = layer_dir / "sac-base-2026-0912-140710.sif"
    artifact.write_bytes(b"x" * 1024)
    artifact.with_suffix(".verified").write_text("round-trip verified\n")
    (ig._CONTAINERS_DIR / "sac-base.sif").symlink_to(Path("sac-base") / artifact.name)
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["status", "--json"])
    data = json.loads(result.stdout)
    # Assert
    assert (
        result.exit_code,
        data[0]["name"],
        data[0]["version"],
        data[0]["verification"],
        data[0]["sif_size_bytes"],
    ) == (0, "sac-base", "2026-0912-140710", "verified", 1024)


def test_status_json_reports_active_artifact_fields(home_tmp):
    # Arrange
    artifact = ig._CONTAINERS_DIR / "sac-base" / "sac-base-2026-0912-140710.sif"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"x" * 1024)
    entries = [
        {
            "ts": "2026-0912-140710",
            "sif": str(artifact),
            "verified": None,
            "active": True,
        }
    ]
    backend = _FakeApptainerBackend(status_result=entries)
    runner = CliRunner()
    # Act
    with _use_backend(backend):
        result = runner.invoke(image_group, ["status", "--json"])
    data = json.loads(result.stdout)
    # Assert
    assert (result.exit_code, data) == (
        0,
        [
            {
                "name": "sac-base",
                "version": "2026-0912-140710",
                "sif_path": str(artifact),
                "sif_size_bytes": 1024,
                "sif_date": data[0]["sif_date"],
                "verification": "unknown",
            }
        ],
    )


def test_status_ignores_inactive_builds(home_tmp):
    # Arrange
    artifact = ig._CONTAINERS_DIR / "sac-base" / "sac-base-2026-0912-120102.sif"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"x")
    backend = _FakeApptainerBackend(
        status_result=[
            {
                "ts": "2026-0912-120102",
                "sif": str(artifact),
                "verified": True,
                "active": False,
            }
        ]
    )
    runner = CliRunner()
    # Act
    with _use_backend(backend):
        result = runner.invoke(image_group, ["status"])
    # Assert
    assert result.exit_code == 0 and "no active SAC images" in result.output


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
# Cross-package discovery — ``~/.scitex/<pkg>/containers/*.sif`` convention
# (operator design 8566). sac does NOT know any package by name; the glob
# spans every package that follows the convention, with no _LAYERS edit
# required for new packages to appear.
# ---------------------------------------------------------------------------


def test_list_discovers_sif_in_downstream_package_dir(home_tmp):
    # Arrange — scitex-writer drops a SIF at the canonical convention
    # path. sac should surface it via the generic glob without any
    # package-name awareness.
    writer_dir = ig._SCITEX_USER_STATE_ROOT / "writer" / "containers"
    writer_dir.mkdir(parents=True)
    (writer_dir / "texlive.sif").write_bytes(b"x" * 100)
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["list"])
    # Assert
    assert result.exit_code == 0 and "texlive.sif" in result.output


def test_list_labels_downstream_sif_with_package_name(home_tmp):
    # Arrange — the rendered row carries ``<package>/<sif>`` so the
    # operator sees the owning package at a glance.
    writer_dir = ig._SCITEX_USER_STATE_ROOT / "writer" / "containers"
    writer_dir.mkdir(parents=True)
    (writer_dir / "texlive.sif").write_bytes(b"x" * 100)
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["list"])
    # Assert
    assert "writer/texlive.sif" in result.output


def test_list_json_carries_package_field_for_each_entry(home_tmp):
    # Arrange — JSON consumers need the package as a structured field,
    # not parsed out of the rendered label.
    writer_dir = ig._SCITEX_USER_STATE_ROOT / "writer" / "containers"
    writer_dir.mkdir(parents=True)
    (writer_dir / "texlive.sif").write_bytes(b"x" * 100)
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["list", "--json"])
    data = json.loads(result.stdout)
    # Assert
    assert data[0]["package"] == "writer"


def test_list_finds_sifs_across_multiple_packages_simultaneously(home_tmp):
    # Arrange — agent-container/sac-base.sif AND writer/texlive.sif
    # AND neurovista/whatever.sif should all surface from a single
    # scan. The convention is generic; new packages slot in without
    # sac code changes.
    ac_dir = ig._SCITEX_USER_STATE_ROOT / "agent-container" / "containers"
    ac_dir.mkdir(parents=True)
    (ac_dir / "sac-base.sif").write_bytes(b"x")
    writer_dir = ig._SCITEX_USER_STATE_ROOT / "writer" / "containers"
    writer_dir.mkdir(parents=True)
    (writer_dir / "texlive.sif").write_bytes(b"x")
    nv_dir = ig._SCITEX_USER_STATE_ROOT / "neurovista" / "containers"
    nv_dir.mkdir(parents=True)
    (nv_dir / "experiment.sif").write_bytes(b"x")
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["list"])
    # Assert
    assert all(
        marker in result.output
        for marker in ("sac-base.sif", "texlive.sif", "experiment.sif")
    )


def test_list_does_not_descend_below_containers_subdir(home_tmp):
    # Arrange — the glob is ``*/containers/*.sif`` (exactly 2 levels).
    # A SIF buried deeper (e.g. ``writer/containers/legacy/old.sif``)
    # is deliberately NOT surfaced — keeps the scan bounded and
    # forces packages onto the flat convention.
    nested = ig._SCITEX_USER_STATE_ROOT / "writer" / "containers" / "legacy"
    nested.mkdir(parents=True)
    (nested / "deep.sif").write_bytes(b"x")
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["list"])
    # Assert — deep.sif must NOT appear in output.
    assert "deep.sif" not in result.output


def test_list_ignores_sifs_outside_containers_subdir(home_tmp):
    # Arrange — a stray SIF directly under ``~/.scitex/writer/`` (not
    # under the ``containers/`` subdir) is OUTSIDE the convention and
    # must not surface. The scan is conventional, not free-form.
    stray = ig._SCITEX_USER_STATE_ROOT / "writer"
    stray.mkdir(parents=True)
    (stray / "stray.sif").write_bytes(b"x")
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["list"])
    # Assert
    assert "stray.sif" not in result.output


def test_scitex_user_state_root_is_dotscitex_under_home():
    # Arrange — pin the root constant so a refactor that moves it
    # silently (e.g. into a config var) trips a red test. The
    # convention's location is operator contract.
    expected = Path.home() / ".scitex"
    # Act
    actual = ig._SCITEX_USER_STATE_ROOT
    # Assert
    assert actual == expected
