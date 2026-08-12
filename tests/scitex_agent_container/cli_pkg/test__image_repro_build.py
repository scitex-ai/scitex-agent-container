"""Tests for ``cli_pkg/_image_repro_build.py`` — the reproducible round trip.

No MagicMock: the round-trip call is a hand-rolled real callable swapped in
through the module-level ``_container_build_reproducible`` seam (the same
save/restore pattern ``test__image_source_build`` and ``test_image_group``
use), and the staging it exercises writes real files into tmp_path.

The behaviour worth pinning is WHAT REACHES scitex-container — above all the
build context (``cwd``). sac's whole contribution to a build is a staged
directory holding its own source tree and the prerequisite layer's SIF; the
shipped recipes reference both by RELATIVE path. If that directory does not
arrive as the build cwd, apptainer FATALs before running a line of ``%post``.
That missing argument is the entire reason the round trip shipped in
scitex-container for months with zero callers.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container.cli_pkg import _image_repro_build as irb


@pytest.fixture
def fake_pkg_root(tmp_path: Path) -> Path:
    """A real on-disk fake of the installed package tree (no MagicMock).

    Mirrors a wheel-install layout closely enough for
    ``stage_build_context``: the bundled build inputs pyproject NAMES must
    all be present or staging refuses.
    """
    root = tmp_path / "scitex_agent_container"
    root.mkdir()
    (root / "__init__.py").write_text("__version__ = '0.0.0-test'\n")
    bundled = root / "_bundled"
    bundled.mkdir()
    (bundled / "pyproject.toml").write_text(
        "[project]\nname = 'scitex-agent-container'\nversion = '0.0.0-test'\n"
    )
    (bundled / "README.md").write_text("# fake readme for tests\n")
    (bundled / "hatch_build.py").write_text("# fake build hook\n")
    return root


@pytest.fixture
def recipe(tmp_path: Path) -> Path:
    """A real .def file to stage."""
    p = tmp_path / "apptainer-proxy.def"
    p.write_text(
        "Bootstrap: docker\nFrom: ubuntu:24.04\n\n%files\n"
        "    scitex-agent-container-src /opt/scitex-agent-container-src\n"
    )
    return p


@contextmanager
def _use_roundtrip(*, result=None, raises=None) -> Iterator[list[dict]]:
    """Swap ``_container_build_reproducible`` for a recording callable."""
    calls: list[dict] = []

    def _recording(**kw):
        calls.append(kw)
        if raises is not None:
            raise raises
        return result

    saved = irb._container_build_reproducible
    irb._container_build_reproducible = _recording  # type: ignore[assignment]
    try:
        yield calls
    finally:
        irb._container_build_reproducible = saved  # type: ignore[assignment]


@dataclass
class _FakeDiff:
    text: str = "1 changed (pip:numpy: 2.1.0 -> 2.2.0)"

    def summary(self) -> str:
        return self.text


@dataclass
class _FakeResult:
    """Stand-in for scitex-container's RoundTripResult (same attributes)."""

    sif: Path = Path("/c/sac-proxy/sac-proxy-ts.sif")
    lock: Path = Path("/c/sac-proxy/sac-proxy-ts.lock")
    locked_def: Path = Path("/c/sac-proxy/sac-proxy-ts.def")
    verified: bool | None = True
    diff: object | None = None
    marker: Path = field(default=Path("/c/sac-proxy/sac-proxy-ts.verified"))


def _build(tmp_path, pkg_root, recipe, **kw):
    return irb.build_layer_reproducible(
        layer=kw.pop("layer", "proxy"),
        def_path=recipe,
        pkg_root=pkg_root,
        output_dir=tmp_path / "containers",
        **kw,
    )


class TestBuildContextReachesTheRoundTrip:
    """The staged directory must arrive as the build ``cwd``."""

    def test_passes_the_staging_dir_as_cwd(self, tmp_path, fake_pkg_root, recipe):
        # Arrange
        with _use_roundtrip(result=_FakeResult()) as calls:
            # Act
            _build(tmp_path, fake_pkg_root, recipe)
        # Assert
        assert calls[0]["cwd"] == tmp_path / "containers" / "sac-proxy" / "build-context"

    def test_staged_source_tree_exists_at_that_cwd(
        self, tmp_path, fake_pkg_root, recipe
    ):
        # Arrange
        with _use_roundtrip(result=_FakeResult()) as calls:
            # Act
            _build(tmp_path, fake_pkg_root, recipe)
        # Assert — the relative %files source the recipe names
        assert (calls[0]["cwd"] / "scitex-agent-container-src").is_dir()

    def test_staged_def_is_the_one_handed_to_the_round_trip(
        self, tmp_path, fake_pkg_root, recipe
    ):
        # Arrange
        with _use_roundtrip(result=_FakeResult()) as calls:
            # Act
            _build(tmp_path, fake_pkg_root, recipe)
        # Assert — the staged copy, not the source recipe
        assert calls[0]["def_path"] == calls[0]["cwd"] / recipe.name


class TestRoundTripArguments:
    """Layer naming, output root and the verify toggle."""

    def test_maps_layer_to_sac_prefixed_image_name(
        self, tmp_path, fake_pkg_root, recipe
    ):
        # Arrange
        with _use_roundtrip(result=_FakeResult()) as calls:
            # Act
            _build(tmp_path, fake_pkg_root, recipe, layer="base")
        # Assert
        assert calls[0]["image_name"] == "sac-base"

    def test_passes_the_containers_dir_as_root(self, tmp_path, fake_pkg_root, recipe):
        # Arrange
        with _use_roundtrip(result=_FakeResult()) as calls:
            # Act
            _build(tmp_path, fake_pkg_root, recipe)
        # Assert
        assert calls[0]["output_dir"] == tmp_path / "containers"

    def test_verify_defaults_to_on(self, tmp_path, fake_pkg_root, recipe):
        # Arrange
        with _use_roundtrip(result=_FakeResult()) as calls:
            # Act
            _build(tmp_path, fake_pkg_root, recipe)
        # Assert
        assert calls[0]["verify"] is True

    def test_verify_can_be_skipped(self, tmp_path, fake_pkg_root, recipe):
        # Arrange
        with _use_roundtrip(result=_FakeResult()) as calls:
            # Act
            _build(tmp_path, fake_pkg_root, recipe, verify=False)
        # Assert
        assert calls[0]["verify"] is False

    def test_returns_the_round_trip_result(self, tmp_path, fake_pkg_root, recipe):
        # Arrange
        expected = _FakeResult()
        with _use_roundtrip(result=expected):
            # Act
            got = _build(tmp_path, fake_pkg_root, recipe)
        # Assert
        assert got is expected


class TestValidateFlags:
    """Impossible combinations are refused BEFORE any staging work."""

    def test_reproducible_with_sandbox_is_refused(self):
        # Arrange
        # Act
        msg = irb.validate_flags(reproducible=True, sandbox=True, skip_verify=False)
        # Assert
        assert "mutually exclusive" in msg

    def test_skip_verify_without_reproducible_is_refused(self):
        # Arrange
        # Act
        msg = irb.validate_flags(reproducible=False, sandbox=False, skip_verify=True)
        # Assert
        assert "--skip-verify only applies with --reproducible." == msg

    def test_reproducible_alone_is_allowed(self):
        # Arrange
        # Act
        msg = irb.validate_flags(reproducible=True, sandbox=False, skip_verify=False)
        # Assert
        assert msg is None

    def test_reproducible_with_skip_verify_is_allowed(self):
        # Arrange
        # Act
        msg = irb.validate_flags(reproducible=True, sandbox=False, skip_verify=True)
        # Assert
        assert msg is None

    def test_plain_sandbox_build_is_allowed(self):
        # Arrange
        # Act
        msg = irb.validate_flags(reproducible=False, sandbox=True, skip_verify=False)
        # Assert
        assert msg is None


class TestDescribeResult:
    """The interesting outcome is whether the version sets matched."""

    def test_verified_build_reports_verified(self):
        # Arrange
        result = _FakeResult(verified=True)
        # Act
        lines = irb.describe_result(result)
        # Assert
        assert any("VERIFIED" in line for line in lines)

    def test_mismatch_reports_the_drift(self):
        # Arrange
        result = _FakeResult(verified=False, diff=_FakeDiff())
        # Act
        lines = irb.describe_result(result)
        # Assert
        assert any("pip:numpy: 2.1.0 -> 2.2.0" in line for line in lines)

    def test_mismatch_says_the_image_is_still_usable(self):
        # Arrange
        result = _FakeResult(verified=False, diff=_FakeDiff())
        # Act
        lines = irb.describe_result(result)
        # Assert
        assert any("USABLE" in line for line in lines)

    def test_skipped_verify_reports_unmarked(self):
        # Arrange
        result = _FakeResult(verified=None)
        # Act
        lines = irb.describe_result(result)
        # Assert
        assert any("SKIPPED" in line for line in lines)

    def test_reports_the_lock_path(self):
        # Arrange
        result = _FakeResult()
        # Act
        lines = irb.describe_result(result)
        # Assert
        assert any(str(result.lock) in line for line in lines)


class TestRunBuildFailure:
    """A failed apptainer build exits non-zero rather than tracebacking."""

    def test_build_failure_exits_nonzero(self, tmp_path, fake_pkg_root, recipe):
        # Arrange
        ctx = pytest.raises(SystemExit)
        # Act
        # Assert
        with _use_roundtrip(raises=RuntimeError("apptainer died")), ctx:
            irb.run_build(
                layer="proxy",
                def_path=recipe,
                pkg_root=fake_pkg_root,
                output_dir=tmp_path / "containers",
                bootstrap_sif=None,
                verify=True,
            )

    def test_successful_run_returns_the_result(self, tmp_path, fake_pkg_root, recipe):
        # Arrange
        expected = _FakeResult()
        # Act
        with _use_roundtrip(result=expected):
            got = irb.run_build(
                layer="proxy",
                def_path=recipe,
                pkg_root=fake_pkg_root,
                output_dir=tmp_path / "containers",
                bootstrap_sif=None,
                verify=True,
            )
        # Assert
        assert got is expected
