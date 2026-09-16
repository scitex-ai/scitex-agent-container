"""Describe the image-frozen SAC Python environment without importing it."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Mapping

IMAGE_VENV = Path("/opt/venv-sac")
IMAGE_LABELS = Path("/.singularity.d/labels.json")
FROZEN_WARNING = (
    "/opt/venv-sac is image-frozen; it is NOT a verification target for "
    "published artifacts. Verify a release in an isolated environment pinned "
    "to that release."
)


@dataclass(frozen=True)
class ImageVenvReport:
    """Observed identity of the Python environment baked into a SAC image."""

    path: str
    available: bool
    active_interpreter: bool
    image_path: str | None
    image_build_date: str | None
    image_build_date_source: str | None
    packages: tuple[tuple[str, str], ...]
    warning: str = FROZEN_WARNING

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "available": self.available,
            "active_interpreter": self.active_interpreter,
            "image_path": self.image_path,
            "image_build_date": self.image_build_date,
            "image_build_date_source": self.image_build_date_source,
            "packages": [
                {"name": name, "version": version} for name, version in self.packages
            ],
            "warning": self.warning,
        }


def _canonical_name(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _site_packages(venv: Path) -> tuple[Path, ...]:
    return tuple(sorted((venv / "lib").glob("python*/site-packages")))


def _installed_scitex_packages(venv: Path) -> tuple[tuple[str, str], ...]:
    rows: set[tuple[str, str]] = set()
    for site in _site_packages(venv):
        for dist in metadata.distributions(path=[str(site)]):
            name = _canonical_name(dist.metadata.get("Name") or "")
            if name == "scitex" or name.startswith("scitex-"):
                rows.add((name, dist.version))
    return tuple(sorted(rows))


def _image_build_date(
    path: str | None, labels_path: Path
) -> tuple[str | None, str | None]:
    try:
        labels = json.loads(labels_path.read_text(encoding="utf-8"))
        build_date = labels.get("org.label-schema.build-date")
        if isinstance(build_date, str) and build_date:
            return build_date, "embedded_label"
    except (OSError, ValueError, AttributeError):
        pass
    if not path:
        return None, None
    try:
        stamp = Path(path).stat().st_mtime
    except OSError:
        return None, None
    value = datetime.fromtimestamp(stamp, tz=UTC).isoformat().replace("+00:00", "Z")
    return value, "sif_mtime"


def inspect_image_venv(
    *,
    image_venv: Path = IMAGE_VENV,
    labels_path: Path = IMAGE_LABELS,
    environ: Mapping[str, str] | None = None,
    interpreter_prefix: Path | None = None,
) -> ImageVenvReport:
    """Inspect the baked venv and SIF metadata without executing either."""
    env = os.environ if environ is None else environ
    prefix = Path(sys.prefix) if interpreter_prefix is None else interpreter_prefix
    image_path = env.get("APPTAINER_CONTAINER") or env.get("SINGULARITY_CONTAINER")
    available = image_venv.is_dir()
    build_date, build_date_source = _image_build_date(image_path, labels_path)
    return ImageVenvReport(
        path=str(image_venv),
        available=available,
        active_interpreter=available and prefix.resolve() == image_venv.resolve(),
        image_path=image_path,
        image_build_date=build_date,
        image_build_date_source=build_date_source,
        packages=_installed_scitex_packages(image_venv) if available else (),
    )


def image_venv_lines(report: ImageVenvReport) -> tuple[str, ...]:
    """Stable human rendering shared by ``doctor`` and ``mcp doctor``."""
    lines = ["image environment", f"  venv: {report.path}"]
    if report.image_path:
        lines.append(f"  image: {report.image_path}")
    if report.image_build_date:
        source = {
            "embedded_label": "embedded image label",
            "sif_mtime": "SIF modification time",
        }.get(report.image_build_date_source, "unknown source")
        lines.append(f"  image build: {report.image_build_date} ({source})")
    else:
        lines.append("  image build: unknown (no readable SIF path)")
    lines.append(f"  warning: {report.warning}")
    if not report.available:
        lines.append("  scitex packages: unavailable (/opt/venv-sac not present)")
    elif not report.packages:
        lines.append("  scitex packages: none found")
    else:
        lines.append("  scitex packages:")
        lines.extend(f"    {name} {version}" for name, version in report.packages)
    return tuple(lines)


__all__ = [
    "FROZEN_WARNING",
    "IMAGE_LABELS",
    "IMAGE_VENV",
    "ImageVenvReport",
    "image_venv_lines",
    "inspect_image_venv",
]
