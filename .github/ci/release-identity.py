#!/usr/bin/env python3
"""Bind a SAC release to one tag, source commit and two verified artifacts."""

import argparse
import ast
import base64
import configparser
import csv
import datetime
import hashlib
import io
import json
import os
import re
import signal
import stat
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

import tomllib

REPOSITORY = "scitex-ai/scitex-agent-container"
TAG = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
PROOF = "release-proof.json"


def api(path, status=200, *, open_url=urllib.request.urlopen):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request("https://api.github.com/" + path, headers=headers)
    with open_url(request, timeout=10) as response:
        if response.status != status:
            raise ValueError("GitHub identity response did not match")
        if status == 204:
            return {"status": 204}
        body = response.read(1048577)
        if len(body) > 1048576:
            raise ValueError("GitHub identity response too large")
        return json.loads(body)


def release_context():
    # The existing shared runner-admission gate owns membership. The caller
    # passes its exact output; this helper does not invent another member model.
    if (
        os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
        or os.environ.get("RELEASE_AUTHORIZED") != "true"
    ):
        raise ValueError("shared release admission is not confirmed")
    if os.environ.get("GITHUB_EVENT_NAME") not in {"push", "workflow_dispatch"}:
        raise ValueError("release event is unsupported")


def resolve(tag, query=api):
    if not TAG.fullmatch(tag):
        raise ValueError("release requires an existing vX.Y.Z tag")
    prefix = "repos/" + REPOSITORY
    ref = query(prefix + "/git/ref/tags/" + tag)
    if ref.get("ref") != "refs/tags/" + tag:
        raise ValueError("returned tag ref differs")
    item = ref["object"]
    seen = set()
    for _ in range(5):
        digest = item.get("sha", "")
        if not SHA.fullmatch(digest) or digest in seen:
            raise ValueError("invalid or cyclic tag object")
        seen.add(digest)
        if item.get("type") == "commit":
            break
        if item.get("type") != "tag":
            raise ValueError("tag does not resolve to a commit")
        item = query(prefix + "/git/tags/" + digest)["object"]
    else:
        raise ValueError("tag annotation depth exceeded")
    commit = item["sha"]
    comparison = query(prefix + "/compare/" + commit + "...develop")
    if comparison.get("base_commit", {}).get("sha") != commit or comparison.get(
        "status"
    ) not in {"ahead", "identical"}:
        raise ValueError("release source is not on the accepted develop history")
    content = query(prefix + "/contents/pyproject.toml?ref=" + commit)
    if content.get("encoding") != "base64":
        raise ValueError("unsupported metadata encoding")
    raw = base64.b64decode(content["content"].replace("\n", ""), validate=True)
    if len(raw) > 131072 or hashlib.sha1(
        b"blob " + str(len(raw)).encode() + b"\0" + raw
    ).hexdigest() != content.get("sha"):
        raise ValueError("release metadata Git identity differs")
    project = tomllib.loads(raw.decode())["project"]
    if (
        normalized_name(project["name"]) != "scitex-agent-container"
        or project["version"] != tag[1:]
    ):
        raise ValueError("tag and project metadata differ")
    return {"tag": tag, "commit": commit, "version": tag[1:]}


def regular_bytes(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("artifact must be a regular file")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 268435456:
            raise ValueError("artifact type or size is invalid")
        raw = stream.read()
        after = os.fstat(stream.fileno())
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or path.lstat().st_ino != before.st_ino:
        raise ValueError("artifact changed while reading")
    return raw


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


def normalized_name(value):
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?", value):
        raise ValueError("unsupported dependency name")
    return re.sub(r"[-_.]+", "-", value).lower()


def specifier_identity(value):
    value = value.strip()
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    if not value:
        return ()
    parts = [part.strip().lower() for part in value.split(",")]
    if any(
        not re.fullmatch(r"(?:===|==|!=|~=|<=|>=|<|>)\s*[a-z0-9.*+!_-]+", part)
        for part in parts
    ):
        raise ValueError("unsupported dependency version constraint")
    return tuple(sorted({re.sub(r"\s+", "", part) for part in parts}))


def marker_identity(value):
    if not value:
        return ()
    token = re.compile(
        r"""\s*("[^"\\]*"|'[^'\\]*'|===|==|!=|~=|<=|>=|[<>()]|[A-Za-z_][A-Za-z0-9_]*)"""
    )
    tokens = []
    position = 0
    while position < len(value):
        match = token.match(value, position)
        if not match:
            if value[position:].strip():
                raise ValueError("unsupported dependency marker")
            break
        tokens.append(match[1])
        position = match.end()
    if len(tokens) > 128:
        raise ValueError("dependency marker is excessive")
    position = 0
    variables = {
        "python_version",
        "python_full_version",
        "os_name",
        "sys_platform",
        "platform_release",
        "platform_system",
        "platform_version",
        "platform_machine",
        "platform_python_implementation",
        "implementation_name",
        "implementation_version",
        "extra",
    }

    def operand():
        nonlocal position
        if position >= len(tokens):
            raise ValueError("incomplete dependency marker")
        item = tokens[position]
        position += 1
        if item[:1] in {"'", '"'}:
            return ("literal", item[1:-1])
        if item not in variables:
            raise ValueError("unknown dependency marker variable")
        return ("variable", item)

    def atom(depth):
        nonlocal position
        if depth > 16 or position >= len(tokens):
            raise ValueError("incomplete dependency marker")
        if tokens[position] == "(":
            position += 1
            result = expression(depth + 1)
            if position >= len(tokens) or tokens[position] != ")":
                raise ValueError("unclosed dependency marker")
            position += 1
            return result
        left = operand()
        if position >= len(tokens):
            raise ValueError("incomplete dependency marker")
        operator = tokens[position]
        position += 1
        if operator == "not":
            if position >= len(tokens) or tokens[position] != "in":
                raise ValueError("unsupported dependency marker operator")
            position += 1
            operator = "not in"
        if operator not in {
            "===",
            "==",
            "!=",
            "~=",
            "<=",
            ">=",
            "<",
            ">",
            "in",
            "not in",
        }:
            raise ValueError("unsupported dependency marker operator")
        right = operand()
        if left == ("variable", "extra") and right[0] == "literal":
            right = ("literal", normalized_name(right[1]))
        if right == ("variable", "extra") and left[0] == "literal":
            left = ("literal", normalized_name(left[1]))
        return ("compare", left, operator, right)

    def combine(operator, values):
        flat = []
        for item in values:
            flat.extend(item[1:] if item[0] == operator else [item])
        return flat[0] if len(flat) == 1 else (operator, *sorted(set(flat)))

    def conjunction(depth):
        nonlocal position
        values = [atom(depth)]
        while position < len(tokens) and tokens[position] == "and":
            position += 1
            values.append(atom(depth))
        return combine("and", values)

    def expression(depth):
        nonlocal position
        values = [conjunction(depth)]
        while position < len(tokens) and tokens[position] == "or":
            position += 1
            values.append(conjunction(depth))
        return combine("or", values)

    result = expression(0)
    if position != len(tokens):
        raise ValueError("unsupported dependency marker suffix")
    return result


def requirement_identity(value):
    requirement, separator, marker = value.partition(";")
    match = re.fullmatch(
        r"\s*([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[([^]]+)\])?\s*(.*?)\s*", requirement
    )
    if not match or "@" in requirement:
        raise ValueError("unsupported dependency requirement")
    extras = tuple(
        sorted(
            {
                normalized_name(x.strip())
                for x in (match[2] or "").split(",")
                if x.strip()
            }
        )
    )
    return (
        normalized_name(match[1]),
        extras,
        specifier_identity(match[3]),
        marker_identity(marker if separator else ""),
    )


def declared_metadata(project):
    """Follow the reviewed Hatch static metadata and recursive-extra contract."""
    if project.get("dynamic"):
        raise ValueError("dynamic release metadata is not qualified")
    core = {requirement_identity(value) for value in project.get("dependencies", [])}
    groups = {}
    inherited = {}
    for name, requirements in project.get("optional-dependencies", {}).items():
        name = normalized_name(name)
        if name in groups:
            raise ValueError("ambiguous source extra")
        groups[name] = set()
        inherited[name] = set()
        for value in requirements:
            row = requirement_identity(value)
            if row[0] == normalized_name(project["name"]):
                if row[2] or row[3]:
                    raise ValueError("conditional self-extra is not qualified")
                inherited[name].update(row[1])
            else:
                groups[name].add(row)
    resolved = set()

    def resolve_group(name, active):
        if name not in groups or name in active:
            raise ValueError("unknown or cyclic source extra")
        if name not in resolved:
            for child in inherited[name]:
                resolve_group(child, active | {name})
                groups[name].update(groups[child])
            resolved.add(name)

    for name in groups:
        resolve_group(name, set())
    expected = set(core)
    for extra, requirements in groups.items():
        extra_marker = marker_identity("extra == '" + extra + "'")
        for name, extras, specifier, marker in requirements:
            if marker:
                parts = list(marker[1:]) if marker[0] == "and" else [marker]
                marker = ("and", *sorted(set([*parts, extra_marker])))
            else:
                marker = extra_marker
            expected.add((name, extras, specifier, marker))
    entries = {
        group: dict(values) for group, values in project.get("entry-points", {}).items()
    }
    for key, group in (("scripts", "console_scripts"), ("gui-scripts", "gui_scripts")):
        if project.get(key):
            if group in entries:
                raise ValueError("ambiguous source entry-point group")
            entries[group] = dict(project[key])
    return {
        "requires_python": specifier_identity(project.get("requires-python", "")),
        "extras": set(groups),
        "requirements": expected,
        "entries": entries,
    }


def metadata_source_identity(wheel_raw, sdist_raw, entry_points, project):
    if any(key in project for key in ("import-names", "import-namespaces")):
        raise ValueError("source import declarations are not qualified")
    expected = declared_metadata(project)
    for raw in (wheel_raw, sdist_raw):
        headers = BytesParser().parsebytes(raw)
        if headers.get_all("Metadata-Version") not in [
            [x] for x in ("2.1", "2.2", "2.3", "2.4", "2.5")
        ]:
            raise ValueError("unsupported generated metadata version")
        if any(
            headers.get_all(key) is not None
            for key in ("Import-Name", "Import-Namespace")
        ):
            raise ValueError("generated import declarations are not qualified")
        python = headers.get_all("Requires-Python", [])
        if len(python) != (1 if expected["requires_python"] else 0) or (
            python and specifier_identity(python[0]) != expected["requires_python"]
        ):
            raise ValueError("source Requires-Python differs")
        extras = headers.get_all("Provides-Extra", [])
        if (
            len(extras) != len({normalized_name(x) for x in extras})
            or {normalized_name(x) for x in extras} != expected["extras"]
        ):
            raise ValueError("source extras differ")
        requirements = [
            requirement_identity(x) for x in headers.get_all("Requires-Dist", [])
        ]
        if (
            len(requirements) != len(set(requirements))
            or set(requirements) != expected["requirements"]
        ):
            raise ValueError("source runtime requirements differ")
        if headers.get_all("License-File", []) != ["LICENSE"]:
            raise ValueError("source license declaration differs")
        if headers.get_all("Dynamic"):
            raise ValueError("dynamic artifact metadata is not qualified")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(
            entry_points.decode("utf-8") if entry_points is not None else ""
        )
    except (UnicodeDecodeError, configparser.Error) as error:
        raise ValueError("invalid entry-point metadata") from error
    actual = {name: dict(parser.items(name, raw=True)) for name in parser.sections()}
    if parser.defaults() or actual != expected["entries"]:
        raise ValueError("source entry points differ")
    return {
        "runtime_requirements": len(expected["requirements"]),
        "extras": len(expected["extras"]),
        "entry_point_groups": len(expected["entries"]),
    }


def git_read(argv, source_root, stdin=None):
    process = subprocess.Popen(
        ["git", "-C", str(source_root), *argv],
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, _ = process.communicate(stdin, timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise ValueError("public source read timed out") from None
    if process.returncode:
        raise ValueError("public source Git read failed")
    return stdout


def declared_public_omissions(project):
    build = project["tool"]["hatch"]["build"]
    if build["targets"]["sdist"] != {
        "exclude": [".git"],
        "hooks": {"custom": {"path": "scripts/hatch_build.py"}},
    } or build.get("ignore-vcs", False):
        raise ValueError("reviewed SAC sdist selection changed")
    if project["build-system"] != {
        "requires": ["hatchling<1.28"],
        "build-backend": "hatchling.build",
    }:
        raise ValueError("reviewed SAC backend changed")
    for target in ("wheel", "sdist"):
        if (
            build["targets"][target].get("hooks", {}).get("custom", {}).get("path")
            != "scripts/hatch_build.py"
        ):
            raise ValueError("reviewed SAC provenance hook changed")
    return set()


def source_payload_identity(wheel_raw, sdist_raw, commit, source_root=Path(".")):
    """Compare public payload membership and bytes with this exact Git commit."""
    if not SHA.fullmatch(commit):
        raise ValueError("public source commit is malformed")
    rows = git_read(["ls-tree", "-r", "-z", "--full-tree", commit], source_root).split(
        b"\0"
    )
    entries = {}
    for row in filter(None, rows):
        metadata, path_raw = row.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        path = path_raw.decode()
        safe_member(path)
        if path in entries or kind != "blob" or not SHA.fullmatch(oid):
            raise ValueError("public source tree is ambiguous")
        entries[path] = (mode, oid)
    if len(entries) > 20000:
        raise ValueError("public source tree exceeds bounds")
    ids = sorted({oid for _, oid in entries.values()})
    frames = (
        git_read(
            ["cat-file", "--batch-check"], source_root, ("\n".join(ids) + "\n").encode()
        )
        .decode()
        .splitlines()
    )
    if len(frames) != len(ids):
        raise ValueError("public source size inventory differs")
    sizes = []
    for oid, frame in zip(ids, frames, strict=True):
        values = frame.split()
        if (
            len(values) != 3
            or values[:2] != [oid, "blob"]
            or not values[2].isdigit()
            or int(values[2]) > 67108864
        ):
            raise ValueError("public source object size differs")
        sizes.append(int(values[2]))
    if sum(sizes) > 268435456:
        raise ValueError("public source byte inventory exceeds bounds")
    stream = io.BytesIO(
        git_read(["cat-file", "--batch"], source_root, ("\n".join(ids) + "\n").encode())
    )
    bodies = {}
    for expected in ids:
        header = stream.readline().decode().strip().split()
        if (
            len(header) != 3
            or header[:2] != [expected, "blob"]
            or not header[2].isdigit()
            or int(header[2]) > 67108864
        ):
            raise ValueError("public source object frame differs")
        body = stream.read(int(header[2]))
        if (
            stream.read(1) != b"\n"
            or hashlib.sha1(b"blob " + header[2].encode() + b"\0" + body).hexdigest()
            != expected
        ):
            raise ValueError("public source object bytes differ")
        bodies[expected] = body
    if stream.read(1):
        raise ValueError("public source object stream has extra data")
    if entries.get("pyproject.toml", ("", ""))[0] not in {"100644", "100755"}:
        raise ValueError("public source metadata is missing")
    project = tomllib.loads(bodies[entries["pyproject.toml"][1]].decode())

    declared_public_omissions(project)
    config = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    packages = ["src/scitex_agent_container", "src/_scitex_agent_container_bootstrap"]
    force = {
        "src/scitex_agent_container/" + path: "scitex_agent_container/" + path
        for path in (
            "cron/post-merge-pull.sh",
            "systemd/sac-listen.service.template",
            "systemd/sac.accounts-refresh.service.template",
            "systemd/sac.accounts-refresh.timer.template",
            "systemd/README.md",
        )
    }
    force.update(
        {
            "pyproject.toml": "scitex_agent_container/_bundled/pyproject.toml",
            "README.md": "scitex_agent_container/_bundled/README.md",
            "scripts/hatch_build.py": "scitex_agent_container/_bundled/hatch_build.py",
        }
    )
    if (
        config.get("packages") != packages
        or config.get("force-include") != force
        or config.get("exclude")
    ):
        raise ValueError("reviewed SAC package mapping changed")
    expected = {}
    source_paths = set(force)
    stamp_path = "src/scitex_agent_container/_provenance/_build_info.py"
    for path, (mode, oid) in entries.items():
        if mode not in {"100644", "100755"}:
            raise ValueError("unsupported public source member")
        if any(path.startswith(package + "/") for package in packages):
            source_paths.add(path)
            if path != stamp_path:
                expected[path.removeprefix("src/")] = bodies[oid]
    for source, destination in force.items():
        if source not in entries:
            raise ValueError("missing force-included public source")
        body = bodies[entries[source][1]]
        if destination in expected and expected[destination] != body:
            raise ValueError("colliding public package member")
        expected[destination] = body
    if not expected:
        raise ValueError("public source package membership is empty")
    stamp_name = stamp_path.removeprefix("src/")
    with zipfile.ZipFile(io.BytesIO(wheel_raw)) as wheel:
        actual = {
            name
            for name in wheel.namelist()
            if name.startswith(
                ("scitex_agent_container/", "_scitex_agent_container_bootstrap/")
            )
            and not name.endswith("/")
        }
        if actual != set(expected) | {stamp_name}:
            raise ValueError("whole wheel public source membership differs")
        for name, body in expected.items():
            if wheel.read(name) != body:
                raise ValueError("wheel public source bytes differ")
        owner = "scitex_agent_container-" + project["project"]["version"] + ".dist-info"
        allowed = {
            owner + "/" + name
            for name in (
                "METADATA",
                "WHEEL",
                "RECORD",
                "entry_points.txt",
                "licenses/LICENSE",
            )
        }
        if any(name not in actual and name not in allowed for name in wheel.namelist()):
            raise ValueError("wheel contains undeclared payload or generated metadata")
        wheel_metadata = wheel.read(owner + "/METADATA")
        headers = BytesParser().parsebytes(wheel.read(owner + "/WHEEL"))
        if (
            headers.get_all("Wheel-Version") != ["1.0"]
            or headers.get_all("Root-Is-Purelib") != ["true"]
            or headers.get_all("Tag") != ["py3-none-any"]
        ):
            raise ValueError("wheel format differs from reviewed pure package")
        if wheel.read(owner + "/licenses/LICENSE") != bodies[entries["LICENSE"][1]]:
            raise ValueError("wheel license source bytes differ")
        entry_points = (
            wheel.read(owner + "/entry_points.txt")
            if owner + "/entry_points.txt" in wheel.namelist()
            else None
        )
        wheel_stamp = provenance_identity(
            wheel.read(stamp_name),
            commit,
            project["project"]["version"],
            entries,
            bodies,
        )
    with tarfile.open(fileobj=io.BytesIO(sdist_raw), mode="r:gz") as sdist:
        members = sdist.getmembers()
        root = PurePosixPath(members[0].name).parts[0]
        sdist_metadata = sdist.extractfile(root + "/PKG-INFO").read()
        generated_metadata = metadata_source_identity(
            wheel_metadata, sdist_metadata, entry_points, project["project"]
        )
        actual = set()
        for item in members:
            if item.isdir():
                continue
            relative = item.name.removeprefix(root + "/")
            if relative == "PKG-INFO":
                continue
            if relative == stamp_path:
                provenance_identity(
                    sdist.extractfile(item).read(),
                    commit,
                    project["project"]["version"],
                    entries,
                    bodies,
                )
            elif (
                relative not in entries
                or entries[relative][0] not in {"100644", "100755"}
                or not item.isfile()
            ):
                raise ValueError("sdist contains undeclared public payload")
            elif sdist.extractfile(item).read() != bodies[entries[relative][1]]:
                raise ValueError("sdist public source bytes differ")
            actual.add(relative)
        if not (
            source_paths
            | {
                "README.md",
                "CHANGELOG.md",
                "LICENSE",
                "pyproject.toml",
                "scripts/hatch_build.py",
                stamp_path,
            }
        ).issubset(actual):
            raise ValueError("sdist public source membership is incomplete")
    manifest = [
        {"path": name, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        for name, body in sorted(expected.items())
    ]
    return {
        "git_commit": commit,
        "generated_metadata": generated_metadata,
        "provenance": wheel_stamp,
        "wheel_public_members": len(manifest),
        "source_membership_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest(),
        "sdist_public_members": len(actual),
    }


def artifact_proof(directory, tag, commit, run, attempt, source_root=Path(".")):
    if (
        not TAG.fullmatch(tag)
        or not SHA.fullmatch(commit)
        or not run.isdigit()
        or not attempt.isdigit()
    ):
        raise ValueError("release identity is malformed")
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("artifact directory is unsafe")
    files = sorted(path for path in directory.iterdir() if path.name != PROOF)
    if (
        len(files) != 2
        or sum(path.name.endswith(".whl") for path in files) != 1
        or sum(path.name.endswith(".tar.gz") for path in files) != 1
    ):
        raise ValueError("release requires exactly one wheel and one sdist")
    rows = []
    payloads = {}
    for path in files:
        if path.name not in {
            "scitex_agent_container-" + tag[1:] + "-py3-none-any.whl",
            "scitex_agent_container-" + tag[1:] + ".tar.gz",
        }:
            raise ValueError("artifact filename version differs")
        raw = regular_bytes(path)
        payloads["wheel" if path.name.endswith(".whl") else "sdist"] = raw
        identity = (
            wheel_identity(raw, tag[1:])
            if path.name.endswith(".whl")
            else sdist_identity(raw, tag[1:])
        )
        rows.append(
            {
                "name": path.name,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                **identity,
            }
        )
    source_identity = source_payload_identity(
        payloads["wheel"], payloads["sdist"], commit, source_root
    )
    return {
        "schema": "scitex-agent-container-release/v1",
        "repository": REPOSITORY,
        "tag": tag,
        "commit": commit,
        "run": run,
        "attempt": attempt,
        "files": rows,
        "source_membership": source_identity,
    }


def provenance_identity(raw, commit, version, entries, bodies):
    # Do not execute generated Python. Only the normal hook's literal stamp is admitted.
    tree = ast.parse(raw)
    if (
        len(tree.body) != 2
        or not isinstance(tree.body[0], ast.Expr)
        or not isinstance(tree.body[0].value, ast.Constant)
        or not isinstance(tree.body[0].value.value, str)
    ):
        raise ValueError("unsupported generated provenance body")
    assignment = tree.body[1]
    if (
        not isinstance(assignment, ast.Assign)
        or len(assignment.targets) != 1
        or not isinstance(assignment.targets[0], ast.Name)
        or assignment.targets[0].id != "STAMP"
    ):
        raise ValueError("unsupported generated provenance assignment")
    stamp = ast.literal_eval(assignment.value)
    if not isinstance(stamp, dict) or set(stamp) != {
        "version",
        "commit",
        "commit_source",
        "code_hash",
        "built_at",
    }:
        raise ValueError("generated provenance keys differ")
    digest = hashlib.blake2b(digest_size=16)
    prefix = "src/scitex_agent_container/"
    for path in sorted(entries):
        relative = path.removeprefix(prefix)
        if (
            not path.startswith(prefix)
            or not path.endswith(".py")
            or relative == "_provenance/_build_info.py"
            or any(
                x in {"__pycache__", "_bundled"} for x in PurePosixPath(relative).parts
            )
        ):
            continue
        body = bodies[entries[path][1]]
        digest.update(
            relative.encode() + b"\0" + str(len(body)).encode() + b"\0" + body
        )
    if (
        stamp["version"] != version
        or stamp["commit"] != commit
        or stamp["commit_source"] not in {"env", "git", "inherited"}
        or stamp["code_hash"] != digest.hexdigest()
    ):
        raise ValueError("generated provenance source identity differs")
    if not isinstance(stamp["built_at"], str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", stamp["built_at"]
    ):
        raise ValueError("generated provenance clock differs")
    datetime.datetime.strptime(stamp["built_at"], "%Y-%m-%dT%H:%M:%SZ")
    return stamp


def run_witness(commit, run, attempt, query=api, *, tests=True):
    if (
        not SHA.fullmatch(commit)
        or not re.fullmatch(r"[1-9][0-9]*", run)
        or not re.fullmatch(r"[1-9][0-9]*", attempt)
    ):
        raise ValueError("release run identity is malformed")
    prefix = "repos/" + REPOSITORY + "/actions/runs/" + run + "/attempts/" + attempt
    current = query(prefix)
    if (
        current.get("head_sha") != commit
        or current.get("id") != int(run)
        or current.get("run_attempt") != int(attempt)
        or current.get("repository", {}).get("full_name") != REPOSITORY
        or current.get("event") not in {"push", "workflow_dispatch"}
        or current.get("path", "").split("@", 1)[0]
        != ".github/workflows/pypi-publish-and-github-release-on-tag.yml"
    ):
        raise ValueError("current release run/source differs")
    actors = {
        current.get(key, {}).get("login", "") for key in ("actor", "triggering_actor")
    }
    expected = {
        os.environ.get("GITHUB_ACTOR", ""),
        os.environ.get("GITHUB_TRIGGERING_ACTOR", ""),
    }
    if actors != expected or any(not LOGIN.fullmatch(x) for x in actors):
        raise ValueError("current release actors differ")
    witness = {
        "run": run,
        "attempt": attempt,
        "head_sha": commit,
        "actors": sorted(actors),
    }
    if not tests:
        return witness
    path = "scitex-ai/.github/.github/workflows/ci-sif-matrix.yml"
    callees = [
        row
        for row in current.get("referenced_workflows", [])
        if row.get("path", "").split("@", 1)[0] == path
    ]
    if (
        len(callees) != 1
        or not SHA.fullmatch(callees[0].get("sha", ""))
        or callees[0].get("ref") != "refs/heads/main"
    ):
        raise ValueError("fixed shared test callee witness is missing")
    document = query(prefix + "/jobs?per_page=100")
    jobs = document.get("jobs", [])
    if (
        type(document.get("total_count")) is not int
        or document["total_count"] != len(jobs)
        or len(jobs) > 100
    ):
        raise ValueError("release job inventory is incomplete")
    completed = []
    for version in ("3.11", "3.12", "3.13"):
        name = "test / test-sac-py" + version
        rows = [row for row in jobs if row.get("name") == name]
        if (
            len(rows) != 1
            or rows[0].get("status") != "completed"
            or rows[0].get("conclusion") != "success"
            or rows[0].get("head_sha") != commit
            or rows[0].get("run_id") != int(run)
            or rows[0].get("run_attempt") != int(attempt)
            or type(rows[0].get("id")) is not int
        ):
            raise ValueError("all three genuine release test legs have not succeeded")
        completed.append({"version": version, "job": rows[0]["id"], "name": name})
    witness.update({"test_callee": callees[0], "tests": completed})
    return witness


def index_identity(document, proof):
    info = document.get("info", {})
    if (
        normalized_name(info.get("name", "")) != "scitex-agent-container"
        or info.get("version") != proof["tag"][1:]
    ):
        raise ValueError("PyPI served project identity differs")
    rows = document.get("urls", [])
    if len(rows) != len(proof["files"]):
        raise ValueError("PyPI served artifact membership differs")
    for artifact in proof["files"]:
        matches = [row for row in rows if row.get("filename") == artifact["name"]]
        if (
            len(matches) != 1
            or matches[0].get("digests", {}).get("sha256") != artifact["sha256"]
            or matches[0].get("size") != artifact["bytes"]
            or matches[0].get("packagetype")
            != ("bdist_wheel" if artifact["name"].endswith(".whl") else "sdist")
        ):
            raise ValueError("PyPI served artifact bytes differ")
    return {"version": info["version"], "files": len(rows)}


def verify_index(proof, *, open_url=urllib.request.urlopen, pause=time.sleep):
    url = "https://pypi.org/pypi/scitex-agent-container/" + proof["tag"][1:] + "/json"
    for attempt in range(18):
        try:
            with open_url(url, timeout=20) as response:
                raw = response.read(1048577)
                if response.status != 200 or len(raw) > 1048576:
                    raise ValueError("PyPI served metadata response differs")
                document = json.loads(raw)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        else:
            return index_identity(document, proof)
        if attempt != 17:
            pause(5)
    raise ValueError("PyPI has not served the qualified version/artifact bytes")


def transport_witness(commit, run, digest, query=api):
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("uploaded artifact digest is missing")
    document = query(
        "repos/" + REPOSITORY + "/actions/runs/" + run + "/artifacts?per_page=100"
    )
    rows = document.get("artifacts", [])
    if document.get("total_count") != len(rows) or len(rows) > 100:
        raise ValueError("uploaded artifact inventory is incomplete")
    selected = [row for row in rows if row.get("name") == "dist-" + commit]
    if (
        len(selected) != 1
        or selected[0].get("expired") is not False
        or selected[0].get("digest") != "sha256:" + digest
        or selected[0].get("workflow_run", {}).get("id") != int(run)
        or selected[0].get("workflow_run", {}).get("head_sha") != commit
    ):
        raise ValueError("uploaded artifact run/source/digest differs")
    return {
        "name": selected[0]["name"],
        "digest": selected[0]["digest"],
        "id": selected[0]["id"],
    }


def main(query=api):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=[
            "resolve",
            "check-tests",
            "write-proof",
            "verify-proof",
            "verify-index",
        ],
    )
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit")
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--revalidate", action="store_true")
    args = parser.parse_args()
    release_context()
    if args.mode == "resolve":
        identity = resolve(args.tag, query)
        # The fixed shared test callee checks out github.sha. Dispatching an
        # older tag from another ref must refuse before any test/build effect.
        if os.environ.get("GITHUB_SHA") != identity["commit"]:
            raise ValueError("workflow event and requested tag commit differ")
        run_witness(
            identity["commit"],
            os.environ.get("GITHUB_RUN_ID", ""),
            os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            query,
            tests=False,
        )
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            for key, value in identity.items():
                stream.write(key + "=" + value + "\n")
        print(json.dumps(identity))
        return
    if git_read(["rev-parse", "HEAD"], Path(".")).decode().strip() != args.commit:
        raise ValueError("checkout and release commit differ")
    witness = run_witness(
        args.commit or "",
        os.environ.get("GITHUB_RUN_ID", ""),
        os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        query,
    )
    if args.mode == "check-tests":
        print(json.dumps(witness))
        return
    proof = artifact_proof(
        args.dist,
        args.tag,
        args.commit or "",
        os.environ.get("GITHUB_RUN_ID", ""),
        os.environ.get("GITHUB_RUN_ATTEMPT", ""),
    )
    proof["run_witness"] = witness
    path = args.dist / PROOF
    if args.mode == "write-proof":
        with os.fdopen(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as stream:
            json.dump(proof, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
    elif json.loads(regular_bytes(path)) != proof:
        raise ValueError("artifact proof or byte identity differs")
    if args.mode in {"verify-proof", "verify-index"}:
        transport_witness(
            args.commit,
            os.environ["GITHUB_RUN_ID"],
            os.environ.get("RELEASE_ARTIFACT_DIGEST", ""),
            query,
        )
    if args.revalidate:
        if os.environ.get("GITHUB_SHA") != args.commit or resolve(args.tag, query) != {
            "tag": args.tag,
            "commit": args.commit,
            "version": args.tag[1:],
        }:
            raise ValueError("release authority changed after build")
    if args.mode == "verify-index":
        print(json.dumps(verify_index(proof)))
    else:
        print(json.dumps(proof))


if __name__ == "__main__":
    main()
