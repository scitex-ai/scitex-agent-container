"""Reuse the launching SAC installation inside Hermes' existing mounts.

The image's SAC may predate the authored spec schema. Keep its reviewed
Hermes harness, but run SAC's owner and tools from the installation that
compiled the profile. No wrapper, package shadow, or second environment is
created. Launch validation requires the complete Python installation to be
visible through the finalized explicit binds.
"""

from __future__ import annotations

import os
import shutil
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

HERMES_BINARY = "/opt/hermes-agent/.venv/bin/hermes"
#: SAC's source tree some images carry (importable with a PYTHONPATH).
IMAGE_SAC_SOURCE = "/opt/scitex-agent-container-src"


class HermesSacRuntimeError(RuntimeError):
    """The compiled profile cannot use its compatible SAC installation."""


@dataclass(frozen=True)
class SacInstallation:
    binary: Path
    python: Path
    prefix: Path
    base_prefix: Path

    @property
    def required_paths(self) -> tuple[Path, ...]:
        paths = (self.binary, self.python, self.prefix, self.base_prefix)
        hops = tuple(hop for path in paths for hop in _symlink_hops(path))
        return tuple(
            dict.fromkeys((*paths, *hops, *(path.resolve() for path in paths)))
        )


def _symlink_hops(path: Path) -> tuple[Path, ...]:
    """Include intermediate interpreter aliases hidden by Path.resolve()."""
    current = path
    hops: list[Path] = []
    visited: set[Path] = set()
    while current not in visited:
        visited.add(current)
        for parent in (*reversed(current.parents), current):
            if not parent.is_symlink():
                continue
            target = parent.readlink()
            if not target.is_absolute():
                target = parent.parent / target
            current = Path(os.path.abspath(target / current.relative_to(parent)))
            hops.append(current)
            # CPython locates its standard library beside bin/, including
            # when a managed Python installation has a version alias.
            if current.parent.name == "bin":
                hops.append(current.parent.parent)
            break
        else:
            return tuple(hops)
    raise HermesSacRuntimeError("SAC installation has a cyclic interpreter symlink")


def sac_installation(config: Any) -> SacInstallation:
    """Resolve standard installed scripts, accepting same-installation aliases.

    The launching installation's scripts directory wins when it carries an
    executable ``sac``. Otherwise the ``sac`` on ``PATH`` is accepted: the
    SIF test driver installs the checkout with ``pip --target`` (no console
    scripts) and exposes exactly this checkout through a ``sac`` shim, so
    requiring the scripts directory would refuse the genuine installation.
    """
    candidates = [Path(sysconfig.get_path("scripts")) / "sac"]
    located = shutil.which("sac")
    if located:
        candidates.append(Path(located))
    binary = next(
        (
            candidate
            for candidate in candidates
            if candidate.is_file() and os.access(candidate, os.X_OK)
        ),
        None,
    )
    python = Path(sys.executable)
    if binary is None:
        raise HermesSacRuntimeError(
            "Hermes requires sac in the launching Python installation's scripts "
            "directory or on PATH; install scitex-agent-container with that "
            "Python first"
        )
    from ._board_identity_env import raw_args_env
    from ._fleet_env import effective_env

    declared = {
        **effective_env(config),
        **raw_args_env(getattr(getattr(config, "apptainer", None), "raw_args", None)),
    }
    for key in ("SAC_BIN", "SAC_BIN_IN_SIF"):
        value = str(declared.get(key, os.environ.get(key, "")) or "").strip()
        if value:
            candidate = Path(value)
            if not candidate.is_absolute() or candidate.resolve() != binary.resolve():
                raise HermesSacRuntimeError(
                    f"{key} must identify the launching SAC installation; "
                    "an older in-image or unrelated CLI cannot read the compiled spec"
                )
    installation = SacInstallation(
        binary=binary,
        python=python,
        prefix=Path(sys.prefix),
        base_prefix=Path(sys.base_prefix),
    )
    if any(
        not path.is_absolute() or any(c in str(path) for c in ":,\n")
        for path in installation.required_paths
    ):
        raise HermesSacRuntimeError(
            "SAC installation paths cannot be rendered safely as Apptainer flags"
        )
    return installation


def _sealed(config: Any) -> bool:
    return getattr(config, "sandbox", None) is not None


def control_plane_paths(config: Any) -> list[str]:
    """What a sealed agent must not see: its state dir and SAC itself.

    The owner keeps all of them and starts the agent tree with each one
    covered (``_hermes_tui_owner --hide``): ``/state/<name>`` (gateway key,
    secret env, logs), the launching installation (its whole environment
    when it is a venv, else its ``sac`` script and package), SAC's own
    in-image venv as a whole, and every other in-image SAC location the TUI
    channel resolver knows (shared venvs: their SAC scripts and package).
    """
    from ._apptainer_inner_argv_tui import (
        _SAC_BIN_IN_SIF_CANDIDATES,
        _SAC_BIN_IN_SIF_DEFAULT,
    )

    installation = sac_installation(config)
    paths = [f"/state/{config.name}"]
    if installation.prefix != installation.base_prefix:
        paths.append(str(installation.prefix))
    else:
        paths.append(str(Path(__file__).resolve().parents[1]))
    if not installation.binary.is_relative_to(installation.prefix):
        paths.append(str(installation.binary))
    paths.append(str(Path(_SAC_BIN_IN_SIF_DEFAULT).parents[1]))
    for candidate in _SAC_BIN_IN_SIF_CANDIDATES:
        paths += [candidate, str(Path(candidate).with_name("scitex-agent-container"))]
    paths.append(IMAGE_SAC_SOURCE)
    return list(dict.fromkeys(paths))


def sac_runtime_env_flags(config: Any) -> list[str]:
    """Prepend the standard scripts directory without replacing image tools.

    A sealed run gets none of it: its agent must not find SAC on ``PATH``
    (the owner is started by absolute interpreter path and needs no PATH).
    """
    if _sealed(config):
        return []
    binary = sac_installation(config).binary
    return [
        "--env",
        f"PREPEND_PATH={binary.parent}",
        "--env",
        f"SAC_BIN={binary}",
        "--env",
        f"SAC_BIN_IN_SIF={binary}",
    ]


def bind_sac_mcp_command(config: Any, servers: dict[str, dict[str, Any]]) -> None:
    """Use the same installed CLI for builtin SAC stdio MCP servers.

    A sealed run refuses them: its agent tree cannot reach SAC (hidden), and
    SAC's control actions are exactly what minimal authority withholds.
    """
    binary = str(sac_installation(config).binary)
    for name, server in servers.items():
        if (
            name in {"sac", "scitex-agent-container"}
            or Path(str(server.get("command", ""))).name == "sac"
        ):
            if _sealed(config):
                raise HermesSacRuntimeError(
                    "spec.sandbox hides SAC from the agent; remove the SAC MCP "
                    f"server {name!r} from the sealed agent's MCP config"
                )
            server["command"] = binary


def _bind_sources(argv: Sequence[str]) -> dict[Path, Path]:
    sources: dict[Path, Path] = {}
    for index, arg in enumerate(argv):
        value = ""
        if arg in {"--bind", "-B"} and index + 1 < len(argv):
            value = argv[index + 1]
        elif arg.startswith(("--bind=", "-B=")):
            value = arg.split("=", 1)[1]
        for declaration in value.split(",") if value else ():
            parts = declaration.split(":", 2)
            source = Path(parts[0]).expanduser()
            destination = Path(parts[1]) if len(parts) > 1 else source
            # Apptainer's first bind to an exact destination wins.
            sources.setdefault(destination, source)
    return sources


def _refuse_exposed_sac(installation: SacInstallation, declared: dict) -> None:
    """A sealed agent's environment must not point it at SAC."""
    exposed = [key for key in ("SAC_BIN", "SAC_BIN_IN_SIF") if declared.get(key)]
    prepend = declared.get("PREPEND_PATH", "").split(":")
    if str(installation.binary.parent) in prepend:
        exposed.append("PREPEND_PATH")
    if exposed:
        raise HermesSacRuntimeError(
            "spec.sandbox hides SAC from the agent, but the finalized "
            f"environment points it at SAC via {', '.join(exposed)}; remove "
            "those declarations"
        )


def validate_sac_runtime(config: Any, *, launch_argv: Sequence[str]) -> None:
    """Refuse an unbound/shadowed installation rather than use old image SAC."""
    installation = sac_installation(config)
    sources = _bind_sources(launch_argv)
    for path in installation.required_paths:
        covering = [
            (target, source)
            for target, source in sources.items()
            if path.is_relative_to(target)
        ]
        if covering:
            target, source = max(covering, key=lambda item: len(item[0].parts))
            exposed = source / path.relative_to(target)
            if exposed.exists() and exposed.resolve() == path.resolve():
                continue
        raise HermesSacRuntimeError(
            f"Launching SAC installation path {path} is absent or shadowed in "
            "the finalized Apptainer binds; bind the installed Python prefix "
            "and its interpreter target at their existing absolute paths "
            "before starting Hermes"
        )
    declared: dict[str, str] = {}
    for index, arg in enumerate(launch_argv):
        value = ""
        if arg == "--env" and index + 1 < len(launch_argv):
            value = launch_argv[index + 1]
        elif arg.startswith("--env="):
            value = arg.split("=", 1)[1]
        for assignment in value.split(","):
            key, separator, content = assignment.partition("=")
            if separator:
                declared[key] = content
    binary = str(installation.binary)
    if _sealed(config):
        _refuse_exposed_sac(installation, declared)
        return
    if declared.get("PREPEND_PATH", "").split(":")[0] != str(
        installation.binary.parent
    ) or any(declared.get(key) != binary for key in ("SAC_BIN", "SAC_BIN_IN_SIF")):
        raise HermesSacRuntimeError(
            "Finalized Hermes tool environment overrides the compatible SAC CLI; "
            "remove conflicting SAC_BIN, SAC_BIN_IN_SIF, or PREPEND_PATH declarations"
        )
