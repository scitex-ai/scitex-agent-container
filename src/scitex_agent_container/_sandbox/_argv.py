"""Sealed-run contributions to the ``apptainer exec`` argv (pure functions).

``build_run_argv`` calls :func:`sandbox_flags` (network + run-scoped binds),
:func:`netgw_wrap` (the in-container gateway around the inner command) and
:func:`drop_writable_tmpfs` (the per-run overlay replaces it). The tmux
launch wraps the finished argv with :func:`supervised_argv` so the host-side
supervisor owns the proxy, the env seal and the outcome record. Nothing here
touches the filesystem: ``sac agents explain`` and ``--dry-run`` render it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ._env_policy import allowed_env_names
from ._layout import C_BIN, C_INGRESS, C_NETGW, C_PROXY_SOCK, C_SOCK, RunPaths, run_link

__all__ = [
    "SUPERVISOR_MODULE",
    "drop_writable_tmpfs",
    "is_sandboxed",
    "netgw_wrap",
    "sandbox_flags",
    "supervised_argv",
]

SUPERVISOR_MODULE = "scitex_agent_container._sandbox"
TUN_DEVICE = "/dev/net/tun"


def is_sandboxed(config: Any) -> bool:
    return getattr(config, "sandbox", None) is not None


def sandbox_flags(config: Any, state_dir: Path) -> list[str]:
    """Network seal + the run-scoped writable layer, via the stable run link."""
    rp = RunPaths(run_link(state_dir))
    return [
        "--net",
        "--network",
        "none",
        # --containall gives a minimal /dev; netgw needs the TUN clone device.
        "--bind",
        TUN_DEVICE,
        "--bind",
        f"{rp.bin}:{C_BIN}:ro",
        "--bind",
        f"{rp.sock}:{C_SOCK}:rw",
        # An explicit /uvwork bind suppresses sac's persistent per-agent one
        # (spec binds win in _apptainer_scratch), so /uvwork is per-run too.
        "--bind",
        f"{rp.uvwork}:/uvwork:rw",
        "--overlay",
        str(rp.overlay),
        "--workdir",
        str(rp.workdir),
    ]


def drop_writable_tmpfs(argv: list[str]) -> list[str]:
    """apptainer refuses ``--writable-tmpfs`` together with ``--overlay``."""
    return [a for a in argv if a != "--writable-tmpfs"]


def netgw_wrap(inner: list[str]) -> list[str]:
    """Run ``inner`` under the gateway in a private user+net namespace."""
    return [
        "unshare",
        "--user",
        "--map-current-user",
        "--net",
        "--keep-caps",
        "python3",
        "-I",
        C_NETGW,
        "--sock",
        C_PROXY_SOCK,
        "--ingress",
        C_INGRESS,
        "--",
        *inner,
    ]


def supervised_argv(
    config: Any, argv: list[str], *, state_dir: Path, image: str
) -> list[str]:
    """``argv`` (``apptainer exec ...``) run under the host-side supervisor."""
    sb = config.sandbox
    out = [
        sys.executable,
        "-I",
        "-m",
        SUPERVISOR_MODULE,
        "supervise",
        "--state-dir",
        str(Path(state_dir).expanduser()),
        "--agent",
        str(getattr(config, "name", "")),
        "--image",
        str(image),
    ]
    for entry in sb.egress_allow_strings:
        out += ["--egress-allow", entry]
    for name in sorted(allowed_env_names(config)):
        out += ["--allow-env", name]
    return [*out, "--", *argv]
