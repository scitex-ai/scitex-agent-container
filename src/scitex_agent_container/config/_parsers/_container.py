"""Parser for ``spec.container``.

No ``runtime`` key is read: the container ENGINE is unconditionally
apptainer (``config._container_engine.CONTAINER_ENGINE``) and a spec
that still writes ``container.runtime`` fails validation rather than
being parsed into a field nothing consults.
"""

from __future__ import annotations

from .._types import ContainerSpec


def parse_container(spec: dict) -> ContainerSpec:
    raw = spec.get("container", {}) or {}
    return ContainerSpec(
        image=raw.get("image", "scitex-agent-container:latest"),
        volumes=raw.get("volumes", []) or [],
        network=raw.get("network", "host"),
        mount_host_claude=bool(raw.get("mount_host_claude", False)),
    )
