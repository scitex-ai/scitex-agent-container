"""Bind release artifact payload to the exact public Git source tree.

Loaded by path from release-identity.py (``python -I``); release_metadata
and release_archive are registered in sys.modules before this executes.
"""

import ast
import datetime
import hashlib
import io
import json
import os
import re
import signal
import subprocess
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

import tomllib
from release_archive import SHA, safe_member
from release_metadata import metadata_source_identity

# Tracked CLI vocabulary that installed-sac `sac image build` needs (5a0c3eea).
CLI_DICTIONARY = ".scitex/dev/cli-audit-dict.yaml"


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
    # The only reviewed sdist force-include maps the CLI vocabulary onto its
    # own tracked path: it pins a wheel build input into the sdist without
    # adding a member. Whole-sdist membership in source_payload_identity must
    # still equal the tracked tree exactly, so this admits no extra payload.
    if build["targets"]["sdist"] != {
        "exclude": [".git"],
        "hooks": {"custom": {"path": "scripts/hatch_build.py"}},
        "force-include": {CLI_DICTIONARY: CLI_DICTIONARY},
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
            # In-process worktree policy assets (reviewed; must resolve via
            # Path(__file__) from an installed sac on hosts with no checkout).
            "_baseline_assets/worktree_policy/worktree-policy.json",
            "_baseline_assets/worktree_policy/generated/AGENTS.worktree-policy.md",
            "_baseline_assets/worktree_policy/generated/CLAUDE.worktree-policy.md",
            "_baseline_assets/worktree_policy/generated/HERMES.worktree-policy.md",
        )
    }
    force.update(
        {
            CLI_DICTIONARY: "scitex_agent_container/_cli_audit_dict.yaml",
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
    stamp_path = "src/scitex_agent_container/_provenance/_build_info.py"
    for path, (mode, oid) in entries.items():
        if mode not in {"100644", "100755"}:
            raise ValueError("unsupported public source member")
        if any(path.startswith(package + "/") for package in packages):
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
        # The reviewed default Hatch selection includes every tracked regular
        # source member. Its only extra payload is the normal generated stamp;
        # PKG-INFO has already been independently bound to public metadata.
        if actual != set(entries) | {stamp_path}:
            raise ValueError("sdist public source membership differs")
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
