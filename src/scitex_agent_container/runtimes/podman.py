"""Podman container runtime adapter.

Podman's CLI is docker-compatible for every command this package
invokes (``run``, ``rm``, ``stop``, ``ps``, ``logs``, ``build``), so
:class:`PodmanRuntime` is just :class:`DockerRuntime` with a different
binary name. Use ``spec.container.runtime: podman`` in YAML to opt in.

Operational notes:

* **Rootless by default.** Unlike docker, podman runs without a
  privileged daemon. The mount paths and uid mapping inside the
  container behave the same way docker's userns-remap does, so the
  ``container = isolation boundary`` story is preserved.
* **No daemon.** ``podman ps`` queries the local user's container
  store directly; there is nothing to keep alive between invocations.
* **Drop-in for docker images.** ``ghcr.io/...`` and other OCI images
  pull and run unchanged.
"""

from __future__ import annotations

from .docker import DockerRuntime

__all__ = ["PodmanRuntime"]


class PodmanRuntime(DockerRuntime):
    """Run agents inside Podman containers (docker-compatible CLI)."""

    BIN: str = "podman"
