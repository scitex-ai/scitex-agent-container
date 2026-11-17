"""Whole wheel / sdist archive identity for SAC releases.

Loaded by path from release-identity.py (``python -I``); release_metadata
is registered in sys.modules before this module executes.
"""

import base64
import csv
import hashlib
import io
import re
import stat
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import PurePosixPath

import tomllib

from release_metadata import normalized_name

SHA = re.compile(r"[0-9a-f]{40}")


def safe_member(name):
    path = PurePosixPath(name)
    if (
        "\\" in name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/"))
    ):
        raise ValueError("unsafe archive member")


def metadata_identity(raw, version):
    message = BytesParser().parsebytes(raw)
    if message.get_all("Name") != ["scitex-agent-container"] and message.get_all(
        "Name"
    ) != ["scitex_agent_container"]:
        raise ValueError("artifact distribution differs")
    if message.get_all("Version") != [version]:
        raise ValueError("artifact version differs")


def wheel_identity(raw, version):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) > 20000:
            raise ValueError("duplicate or excessive wheel members")
        for item in archive.infolist():
            safe_member(item.filename)
            mode = stat.S_IFMT(item.external_attr >> 16)
            if mode not in {0, stat.S_IFREG, stat.S_IFDIR} or item.file_size > 67108864:
                raise ValueError("unsupported wheel member")
        if sum(item.file_size for item in archive.infolist()) > 1073741824:
            raise ValueError("wheel expanded size exceeded")
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        records = [name for name in names if name.endswith(".dist-info/RECORD")]
        owner = "scitex_agent_container-" + version + ".dist-info"
        if metadata != [owner + "/METADATA"] or records != [owner + "/RECORD"]:
            raise ValueError("wheel metadata and RECORD owners differ")
        metadata_identity(archive.read(metadata[0]), version)
        seen = set()
        for name, digest, size in csv.reader(
            io.StringIO(archive.read(records[0]).decode())
        ):
            if name in seen or name not in names:
                raise ValueError("wheel RECORD membership differs")
            seen.add(name)
            if name == records[0]:
                if digest or size:
                    raise ValueError("wheel RECORD self entry differs")
                continue
            body = archive.read(name)
            expected = "sha256=" + base64.urlsafe_b64encode(
                hashlib.sha256(body).digest()
            ).decode().rstrip("=")
            if digest != expected or size != str(len(body)):
                raise ValueError("wheel RECORD hash or size differs")
        if seen != set(names):
            raise ValueError("wheel has unrecorded members")
        if not {
            "scitex_agent_container/__init__.py",
            "_scitex_agent_container_bootstrap/__init__.py",
            "scitex_agent_container/_bundled/hatch_build.py",
        }.issubset(names):
            raise ValueError("wheel lost required public payload")
        return {"members": len(names), "record_members": len(seen)}


def sdist_identity(raw, version):
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        entries = archive.getmembers()
        names = [item.name for item in entries]
        if len(names) != len(set(names)) or len(names) > 20000:
            raise ValueError("duplicate or excessive sdist members")
        for item in entries:
            safe_member(item.name)
            if not (item.isfile() or item.isdir()) or item.size > 67108864:
                raise ValueError("unsupported sdist member")
        if sum(item.size for item in entries) > 1073741824:
            raise ValueError("sdist expanded size exceeded")
        root = "scitex_agent_container-" + version
        if {PurePosixPath(name).parts[0] for name in names} != {root}:
            raise ValueError("sdist root differs")
        required = {
            root + "/" + name
            for name in (
                "src/scitex_agent_container/__init__.py",
                "src/_scitex_agent_container_bootstrap/__init__.py",
                "scripts/hatch_build.py",
                "PKG-INFO",
                "pyproject.toml",
            )
        }
        if not required.issubset(names) or any(
            not archive.getmember(name).isfile() for name in required
        ):
            raise ValueError("sdist lost required public payload")
        metadata_identity(archive.extractfile(root + "/PKG-INFO").read(), version)
        project = tomllib.loads(
            archive.extractfile(root + "/pyproject.toml").read().decode()
        )["project"]
        if (
            project["version"] != version
            or normalized_name(project["name"]) != "scitex-agent-container"
        ):
            raise ValueError("sdist project identity differs")
        return {"members": len(entries)}
