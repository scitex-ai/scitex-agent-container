"""Parser for ``spec.container``."""

from __future__ import annotations

from .._types import ContainerSpec


def parse_container(spec: dict) -> ContainerSpec:
    raw = spec.get("container", {}) or {}
    return ContainerSpec(
        runtime=raw.get("runtime", "none"),
        image=raw.get("image", "scitex-agent-container:latest"),
        volumes=raw.get("volumes", []) or [],
        network=raw.get("network", "host"),
        mount_host_claude=bool(raw.get("mount_host_claude", False)),
    )
