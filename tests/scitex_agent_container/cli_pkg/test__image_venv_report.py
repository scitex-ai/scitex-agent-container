"""The image venv identifies its frozen contents instead of impersonating PyPI."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg._image_venv_report import (
    FROZEN_WARNING,
    image_venv_lines,
    inspect_image_venv,
)


def _distribution(site: Path, name: str, version: str) -> None:
    normalized = name.replace("-", "_")
    info = site / f"{normalized}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n",
        encoding="utf-8",
    )


@pytest.fixture
def baked_environment(tmp_path: Path) -> tuple[Path, Path, Path]:
    venv = tmp_path / "opt" / "venv-sac"
    site = venv / "lib" / "python3.12" / "site-packages"
    _distribution(site, "scitex-agent-container", "0.28.1")
    _distribution(site, "scitex-ui", "0.19.1")
    _distribution(site, "unrelated", "9.9.9")
    image = tmp_path / "sac-base.sif"
    image.write_bytes(b"sif")
    os.utime(image, (1_700_000_000, 1_700_000_000))
    return venv, image, tmp_path / "labels.json"


def test_report_reads_every_scitex_distribution_from_the_baked_venv(
    baked_environment: tuple[Path, Path, Path],
) -> None:
    # Arrange
    venv, image, labels = baked_environment
    # Act
    report = inspect_image_venv(
        image_venv=venv,
        labels_path=labels,
        environ={"APPTAINER_CONTAINER": str(image)},
        interpreter_prefix=venv,
    )
    # Assert
    assert report.packages == (
        ("scitex-agent-container", "0.28.1"),
        ("scitex-ui", "0.19.1"),
    )


def test_report_reads_the_image_build_time_from_the_sif(
    baked_environment: tuple[Path, Path, Path],
) -> None:
    # Arrange
    venv, image, labels = baked_environment
    # Act
    report = inspect_image_venv(
        image_venv=venv,
        labels_path=labels,
        environ={"SINGULARITY_CONTAINER": str(image)},
        interpreter_prefix=venv,
    )
    # Assert
    assert report.image_build_date == "2023-11-14T22:13:20Z"


def test_report_prefers_the_embedded_image_build_date(
    baked_environment: tuple[Path, Path, Path],
) -> None:
    # Arrange
    venv, image, labels = baked_environment
    labels.write_text(
        '{"org.label-schema.build-date": "Thursday_17_September_2026_7:53:33_JST"}',
        encoding="utf-8",
    )
    # Act
    report = inspect_image_venv(
        image_venv=venv,
        labels_path=labels,
        environ={"APPTAINER_CONTAINER": str(image)},
        interpreter_prefix=venv,
    )
    # Assert
    assert report.image_build_date == "Thursday_17_September_2026_7:53:33_JST"


def test_report_marks_the_baked_interpreter_as_active(
    baked_environment: tuple[Path, Path, Path],
) -> None:
    # Arrange
    venv, image, labels = baked_environment
    # Act
    report = inspect_image_venv(
        image_venv=venv,
        labels_path=labels,
        environ={"APPTAINER_CONTAINER": str(image)},
        interpreter_prefix=venv,
    )
    # Assert
    assert report.active_interpreter is True


def test_human_report_says_the_venv_is_not_a_release_verification_target(
    baked_environment: tuple[Path, Path, Path],
) -> None:
    # Arrange
    venv, image, labels = baked_environment
    report = inspect_image_venv(
        image_venv=venv,
        labels_path=labels,
        environ={"APPTAINER_CONTAINER": str(image)},
        interpreter_prefix=venv,
    )
    # Act
    rendered = "\n".join(image_venv_lines(report))
    # Assert
    assert FROZEN_WARNING in rendered


def test_missing_image_venv_is_reported_as_unavailable(tmp_path: Path) -> None:
    # Arrange
    absent = tmp_path / "missing"
    # Act
    report = inspect_image_venv(
        image_venv=absent,
        labels_path=tmp_path / "missing-labels.json",
        environ={},
        interpreter_prefix=tmp_path,
    )
    # Assert
    assert (report.available, report.packages) == (False, ())
