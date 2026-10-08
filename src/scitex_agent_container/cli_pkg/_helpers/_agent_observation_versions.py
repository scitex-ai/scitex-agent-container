"""Keep private, image and loaded-process package observations distinct."""

from __future__ import annotations

import ast
import re
from email.parser import BytesParser
from pathlib import Path

from ._agent_observation_io import mount_source, read_bytes

PACKAGES = {"scitex-agent-container", "scitex-cards", "scitex-dev"}


def distribution_versions(site):
    """Inspect METADATA only; never import packages or execute an interpreter."""
    result = {name: None for name in PACKAGES}
    matches = list(Path(site).glob("*.dist-info/METADATA"))
    if len(matches) > 512:
        raise ValueError("distribution-inventory-bound")
    for path in matches:
        metadata = BytesParser().parsebytes(read_bytes(path, 1024 * 1024))
        name = str(metadata.get("Name") or "").lower().replace("_", "-")
        if name not in PACKAGES:
            continue
        version = metadata.get("Version")
        if not version or len(version) > 80 or result[name] is not None:
            raise ValueError("distribution-metadata-ambiguous")
        result[name] = version
    return result


def _sites(root):
    paths = list(Path(root).glob("lib/python*/site-packages"))
    if len(paths) != 1:
        raise ValueError("interpreter-site-unknown")
    return paths[0]


def sac_build(site, version):
    """Parse the installed public build stamp as data, without importing code."""
    path = Path(site) / "scitex_agent_container/_provenance/_build_info.py"
    try:
        tree = ast.parse(read_bytes(path, 65_536))
        stamps = [
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "STAMP"
                for target in node.targets
            )
        ]
        if len(stamps) != 1 or not isinstance(stamps[0], dict):
            raise ValueError("build-stamp-ambiguous")
        stamp = stamps[0]
        commit, code_hash = stamp.get("commit"), stamp.get("code_hash")
        if (
            stamp.get("version") != version
            or not isinstance(commit, str)
            or re.fullmatch(r"[a-f0-9]{40}", commit) is None
            or not isinstance(code_hash, str)
            or re.fullmatch(r"[a-f0-9]{32}|[a-f0-9]{64}", code_hash) is None
        ):
            raise ValueError("build-stamp-unqualified")
        return {"state": "observed", "commit": commit, "code_hash": code_hash}
    except (OSError, ValueError, SyntaxError):
        return {"state": "unknown"}


def _environment(site):
    packages = distribution_versions(site)
    return {
        "state": "observed",
        "packages": packages,
        "sac_build": sac_build(site, packages.get("scitex-agent-container")),
    }


def observe_versions(process, environment):
    """Missing private metadata never asserts a missing image distribution."""
    result = {
        "private_environment": {"state": "unknown", "packages": {}},
        "image_environment": {"state": "unknown", "packages": {}},
        "loaded_process": {"state": "unknown", "reason": "module-origins-not-exposed"},
        "skew": "unknown",
    }
    mounts = read_bytes(process / "mountinfo", 2 * 1024 * 1024).decode()
    host_mounts = read_bytes(Path("/proc/self/mountinfo"), 2 * 1024 * 1024).decode()
    try:
        private = mount_source(mounts, "/uvwork", host_mountinfo=host_mounts)
        result["private_environment"] = _environment(_sites(private / "venv-agent"))
    except (OSError, ValueError):
        pass
    try:
        result["image_environment"] = _environment(
            _sites(process / "root/opt/venv-sac")
        )
    except (OSError, ValueError):
        pass
    if read_bytes(process / "mountinfo", 2 * 1024 * 1024).decode() != mounts:
        raise ValueError("selected-bind-changed")
    private = result["private_environment"]
    image = result["image_environment"]
    if private["state"] == image["state"] == "observed":
        comparable = [
            (private["packages"].get(name), image["packages"].get(name))
            for name in PACKAGES
        ]
        private_build, image_build = private["sac_build"], image["sac_build"]
        build_known = private_build["state"] == image_build["state"] == "observed"
        result["skew"] = (
            "different"
            if any(a and b and a != b for a, b in comparable)
            or (build_known and private_build["code_hash"] != image_build["code_hash"])
            else (
                "equal"
                if all(a and b for a, b in comparable) and build_known
                else "unknown"
            )
        )
    # This is the runtime's declaration, not an independent whole-image hash.
    result["image_declaration_present"] = bool(
        environment.get("APPTAINER_CONTAINER")
        or environment.get("SINGULARITY_CONTAINER")
    )
    declaration = (
        environment.get("APPTAINER_CONTAINER")
        or environment.get("SINGULARITY_CONTAINER")
        or ""
    )
    image_hash = re.fullmatch(
        r"sac-base-sha256-([a-f0-9]{64})\.sif", Path(declaration).name
    )
    result["image_declaration"] = (
        {"state": "declared", "sha256": image_hash[1], "verification": "not-rehashed"}
        if image_hash
        else {"state": "unknown"}
    )
    return result
