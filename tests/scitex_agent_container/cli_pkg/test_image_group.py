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


@pytest.fixture
def staged_python_pkgs_sif(home_tmp: Path) -> Path:
    """Stage the prerequisite SIF that a ``:base`` build now requires.

    Since the four-layer split (system-deps -> python-pkgs -> base ->
    scitex), ``base`` is no longer the bottom of the stack: it bootstraps
    ``From: ./sac-python-pkgs.sif``, so ``sac image build base`` FAILS LOUD
    before reaching the builder when that SIF is absent.

    Every test below that builds the DEFAULT layer is about something else
    entirely — flag plumbing, priority demotion, the reproducible round trip
    — and would otherwise die on a prerequisite it never meant to exercise.
    Staging it here keeps those tests testing what their names claim.

    Models the atomic layout scitex-container 0.3.0 lands: a timestamped SIF
    plus the stable inner boot symlink that ``resolve_bootstrap_sif``
    resolves (``is_file()`` follows the symlink).
    """
    parent_dir = ig._CONTAINERS_DIR / "sac-python-pkgs"
    parent_dir.mkdir(parents=True, exist_ok=True)
    real_sif = parent_dir / "sac-python-pkgs-20260814T000000Z.sif"
    real_sif.write_bytes(b"fake python-pkgs SIF")
    inner = parent_dir / "sac-python-pkgs.sif"
    inner.symlink_to(real_sif.name)
    return inner



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


def test_build_success_invokes_source_builder_and_prints_built_message(home_tmp, staged_python_pkgs_sif):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/sac-base.sif")) as calls:
        result = runner.invoke(image_group, ["build", "base", "--yes"])
    # Assert
    assert result.exit_code == 0 and "built" in result.output and len(calls) == 1


def test_build_success_passes_layer_def_path_pkg_root_and_force(home_tmp, staged_python_pkgs_sif):
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


def test_build_sandbox_flag_forwarded_to_source_builder(home_tmp, staged_python_pkgs_sif):
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


def test_build_reproducible_routes_to_the_round_trip(home_tmp, staged_python_pkgs_sif):
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


def test_build_reproducible_verifies_by_default(home_tmp, staged_python_pkgs_sif):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_reproducible_builder() as calls:
        runner.invoke(image_group, ["build", "base", "--yes", "--reproducible"])
    # Assert
    assert calls[0]["verify"] is True


def test_build_skip_verify_turns_the_replay_off(home_tmp, staged_python_pkgs_sif):
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


def test_build_reports_apptainer_failure_with_exit_code_1(home_tmp, staged_python_pkgs_sif):
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


def test_build_system_deps_passes_none_bootstrap_sif(home_tmp):
    # Arrange — ``system-deps`` is the BOTTOM of the four-layer stack: it
    # bootstraps off the pinned ubuntu registry image, so it has no
    # prerequisite SIF and the CLI must NOT make one up (would dangle a
    # stale symlink in the staging dir).
    #
    # This assertion used to name ``base``. The split moved base up the
    # chain — it now bootstraps From ./sac-python-pkgs.sif — so the
    # no-prerequisite property moved with the bottom of the stack rather
    # than disappearing.
    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/sac-system-deps.sif")) as calls:
        runner.invoke(image_group, ["build", "system-deps", "--yes"])
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


def test_build_default_calls_priority_demoter_with_skip_false(home_tmp, staged_python_pkgs_sif):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/sac-base.sif")):
        with _use_demoter() as calls:
            runner.invoke(image_group, ["build", "base", "--yes"])
    # Assert — self-demotion is the DEFAULT (no flag needed).
    assert calls == [{"skip": False}]


def test_build_no_nice_flag_forwards_skip_true_to_demoter(home_tmp, staged_python_pkgs_sif):
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


def test_build_echoes_low_priority_notice_from_demoter(home_tmp, staged_python_pkgs_sif):
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


def test_build_consults_remote_advisory_before_heavy_work(home_tmp, staged_python_pkgs_sif):
    # Arrange
    runner = CliRunner()
    # Act
    with _use_source_builder(result=Path("/tmp/sac-base.sif")):
        with _use_advisory() as calls:
            runner.invoke(image_group, ["build", "base", "--yes"])
    # Assert — consulted exactly once, with live introspection defaults.
    assert calls == [{}]


def test_build_proceeds_demoted_when_advisory_fires(home_tmp, staged_python_pkgs_sif):
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
    start = result.output.index("[")
    end = result.output.rindex("]") + 1
    data = json.loads(result.output[start:end])
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
