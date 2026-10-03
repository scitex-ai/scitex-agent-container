"""Map CCT's contracted container interpreter through its actual launch bind."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ._apptainer_scratch import _argv_uvwork_bind

_CONTAINER_PYTHON = "/uvwork/venv-agent/bin/python"


class CctHostPythonError(RuntimeError):
    """The selected host sidecar has no proven executable interpreter."""


@dataclass(frozen=True)
class CctHostPythonBind:
    """Nonsecret bind evidence from one agent's finished launch argv."""

    agent: str
    host_uvwork: Path | None


def capture_cct_host_python_bind(config, argv: list[str]) -> CctHostPythonBind:
    """Keep the first bind, matching the existing Apptainer launch planner."""
    found = _argv_uvwork_bind(argv)
    host = None if found is None else Path(found[0])
    if host is not None and (not host.is_absolute() or ".." in host.parts):
        raise CctHostPythonError("CCT /uvwork bind must have an absolute host source")
    return CctHostPythonBind(agent=config.name, host_uvwork=host)


def resolve_cct_host_python(
    config,
    declared: str | None,
    binding: CctHostPythonBind | None,
) -> str | None:
    """Translate only the contracted private Python; never infer a host path."""
    if declared is None or not declared.startswith("/uvwork/"):
        return declared
    if declared != _CONTAINER_PYTHON:
        raise CctHostPythonError("CCT interpreter is not the contracted private Python")
    if binding is None or binding.host_uvwork is None:
        raise CctHostPythonError("CCT interpreter needs /uvwork from the actual launch")
    if binding.agent != config.name:
        raise CctHostPythonError("CCT interpreter binding belongs to another agent")
    root = binding.host_uvwork
    if not root.is_absolute() or ".." in root.parts:
        raise CctHostPythonError("CCT /uvwork bind must have an absolute host source")
    python = root / "venv-agent/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise CctHostPythonError("CCT mapped private Python is not an executable file")
    return str(python)
