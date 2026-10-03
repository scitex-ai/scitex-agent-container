"""Pure SAC release controls: real public Git, ZIP/TAR and owned shell fixtures."""

from __future__ import annotations

import base64
import configparser
import contextlib
import copy
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

import tomllib

if not __debug__:
    raise RuntimeError("Release controls require real assertions")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SPEC = importlib.util.spec_from_file_location(
    "sac_release_identity_controls", HERE / "release-identity.py"
)
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)
PREFIX = "repos/scitex-ai/scitex-agent-container"
OWNER = "scitex_agent_container-0.29.4.dist-info"
VERSION = "0.29.4"
TAG = "v" + VERSION
RUN = "773311"
ATTEMPT = "2"
STAMP = "src/scitex_agent_container/_provenance/_build_info.py"
# Genuine static Hatchling 1.27 metadata projection of exact 121bf pyproject,
# independently produced without executing a build hook or product import.
GENERATED_METADATA = base64.b64decode(
    "TWV0YWRhdGEtVmVyc2lvbjogMi40Ck5hbWU6IHNjaXRleC1hZ2VudC1jb250YWluZXIKVmVyc2lvbjogMC4yOS40ClN1bW1hcnk6IERlY2xhcmF0aXZlIFlBTUwtYmFzZWQgZnJhbWV3b3JrIGZvciBkZWZpbmluZywgbWFuYWdpbmcsIGFuZCBvcmNoZXN0cmF0aW5nIEFJIGNvZGluZyBhZ2VudCBpbnN0YW5jZXMKUHJvamVjdC1VUkw6IEhvbWVwYWdlLCBodHRwczovL2dpdGh1Yi5jb20veXdhdGFuYWJlMTk4OS9zY2l0ZXgtYWdlbnQtY29udGFpbmVyClByb2plY3QtVVJMOiBSZXBvc2l0b3J5LCBodHRwczovL2dpdGh1Yi5jb20veXdhdGFuYWJlMTk4OS9zY2l0ZXgtYWdlbnQtY29udGFpbmVyLmdpdApQcm9qZWN0LVVSTDogSXNzdWVzLCBodHRwczovL2dpdGh1Yi5jb20veXdhdGFuYWJlMTk4OS9zY2l0ZXgtYWdlbnQtY29udGFpbmVyL2lzc3VlcwpQcm9qZWN0LVVSTDogRG9jdW1lbnRhdGlvbiwgaHR0cHM6Ly9zY2l0ZXgtYWdlbnQtY29udGFpbmVyLnJlYWR0aGVkb2NzLmlvCkF1dGhvci1lbWFpbDogWXVzdWtlIFdhdGFuYWJlIDx5d2F0YW5hYmVAc2NpdGV4LmFpPgpMaWNlbnNlLUV4cHJlc3Npb246IEFHUEwtMy4wLW9ubHkKTGljZW5zZS1GaWxlOiBMSUNFTlNFCktleXdvcmRzOiBhZ2VudCxjbGF1ZGUtY29kZSxjb250YWluZXIsb3JjaGVzdHJhdGlvbixzY2l0ZXgseWFtbApDbGFzc2lmaWVyOiBEZXZlbG9wbWVudCBTdGF0dXMgOjogMyAtIEFscGhhCkNsYXNzaWZpZXI6IEVudmlyb25tZW50IDo6IENvbnNvbGUKQ2xhc3NpZmllcjogSW50ZW5kZWQgQXVkaWVuY2UgOjogRGV2ZWxvcGVycwpDbGFzc2lmaWVyOiBPcGVyYXRpbmcgU3lzdGVtIDo6IE9TIEluZGVwZW5kZW50CkNsYXNzaWZpZXI6IFByb2dyYW1taW5nIExhbmd1YWdlIDo6IFB5dGhvbiA6OiAzCkNsYXNzaWZpZXI6IFByb2dyYW1taW5nIExhbmd1YWdlIDo6IFB5dGhvbiA6OiAzLjExCkNsYXNzaWZpZXI6IFByb2dyYW1taW5nIExhbmd1YWdlIDo6IFB5dGhvbiA6OiAzLjEyCkNsYXNzaWZpZXI6IFByb2dyYW1taW5nIExhbmd1YWdlIDo6IFB5dGhvbiA6OiAzLjEzCkNsYXNzaWZpZXI6IFRvcGljIDo6IFNvZnR3YXJlIERldmVsb3BtZW50IDo6IExpYnJhcmllcwpSZXF1aXJlcy1QeXRob246ID49My4xMQpSZXF1aXJlcy1EaXN0OiBhMmEtc2RrW2h0dHAtc2VydmVyXT49MS4wLjIKUmVxdWlyZXMtRGlzdDogY2xhdWRlLWFnZW50LXNkaz49MC4xLjAKUmVxdWlyZXMtRGlzdDogY2xpY2s+PTguMgpSZXF1aXJlcy1EaXN0OiBodHRweD49MC4yOC4xClJlcXVpcmVzLURpc3Q6IHByb3RvYnVmPDYKUmVxdWlyZXMtRGlzdDogcHN1dGlsPj01LjkKUmVxdWlyZXMtRGlzdDogcHN5Y29wZ1tiaW5hcnldPj0zLjEKUmVxdWlyZXMtRGlzdDogcHlkYW50aWM8Myw+PTIuMTAKUmVxdWlyZXMtRGlzdDogcHl5YW1sPj02LjAKUmVxdWlyZXMtRGlzdDogcmljaD49MTMuMApSZXF1aXJlcy1EaXN0OiBydWFtZWwteWFtbD49MC4xOApSZXF1aXJlcy1EaXN0OiBzY2l0ZXgtY2FyZHM+PTAuNTMuNApSZXF1aXJlcy1EaXN0OiBzY2l0ZXgtY29uZmlnPj0wLjMuMApSZXF1aXJlcy1EaXN0OiBzY2l0ZXgtY29udGFpbmVyPj0wLjQuMApSZXF1aXJlcy1EaXN0OiBzY2l0ZXgtZGV2Pj0wLjYxLjAKUmVxdWlyZXMtRGlzdDogc2NpdGV4LWxvZ2dpbmc+PTAuMS41ClJlcXVpcmVzLURpc3Q6IHNjaXRleC1zc2g+PTEuMC4wClJlcXVpcmVzLURpc3Q6IHV2aWNvcm4+PTAuMjcKUmVxdWlyZXMtRGlzdDogd2Vic29ja2V0czwxNiw+PTE1ClByb3ZpZGVzLUV4dHJhOiBhbGwKUmVxdWlyZXMtRGlzdDogY2xhdWRlLWNvZGUtdGVsZWdyYW1tZXI+PTAuNi4xOyBleHRyYSA9PSAnYWxsJwpSZXF1aXJlcy1EaXN0OiBkamFuZ28+PTQuMjsgZXh0cmEgPT0gJ2FsbCcKUmVxdWlyZXMtRGlzdDogZmFzdG1jcDw0LD49Mi4wOyBleHRyYSA9PSAnYWxsJwpSZXF1aXJlcy1EaXN0OiBmYXN0bWNwPDQsPj0zLjA7IGV4dHJhID09ICdhbGwnClJlcXVpcmVzLURpc3Q6IG1jcDwyLD49MS4yOTsgZXh0cmEgPT0gJ2FsbCcKUmVxdWlyZXMtRGlzdDogbXlzdC1wYXJzZXI+PTIuMDsgZXh0cmEgPT0gJ2FsbCcKUmVxdWlyZXMtRGlzdDogb3BlbmFpLWFnZW50cz49MC4xOS4wOyBleHRyYSA9PSAnYWxsJwpSZXF1aXJlcy1EaXN0OiBvcGVuYWktY29kZXg+PTAuMTQ0LjQ7IGV4dHJhID09ICdhbGwnClJlcXVpcmVzLURpc3Q6IHByZS1jb21taXQ+PTMuNS4wOyBleHRyYSA9PSAnYWxsJwpSZXF1aXJlcy1EaXN0OiBweXRlc3QtYXN5bmNpbz49MC4yMzsgZXh0cmEgPT0gJ2FsbCcKUmVxdWlyZXMtRGlzdDogcHl0ZXN0LWNvdj49NC4wLjA7IGV4dHJhID09ICdhbGwnClJlcXVpcmVzLURpc3Q6IHB5dGVzdC10aW1lb3V0Pj0yLjA7IGV4dHJhID09ICdhbGwnClJlcXVpcmVzLURpc3Q6IHB5dGVzdC14ZGlzdD49My4wLjA7IGV4dHJhID09ICdhbGwnClJlcXVpcmVzLURpc3Q6IHB5dGVzdDw5LjAuMCw+PTcuMC4wOyBleHRyYSA9PSAnYWxsJwpSZXF1aXJlcy1EaXN0OiBzY2l0ZXgtZ2VuYWlbZ2F0ZXdheV0+PTAuMS40OyBleHRyYSA9PSAnYWxsJwpSZXF1aXJlcy1EaXN0OiBzY2l0ZXgtZ2l0Pj0wLjMuMDsgZXh0cmEgPT0gJ2FsbCcKUmVxdWlyZXMtRGlzdDogc2NpdGV4LWhwYz49MC42LjI7IGV4dHJhID09ICdhbGwnClJlcXVpcmVzLURpc3Q6IHNjaXRleC1ub3RpZmljYXRpb24+PTAuMi45OyBleHRyYSA9PSAnYWxsJwpSZXF1aXJlcy1EaXN0OiBzY2l0ZXgtc2RrW2d1aV0+PTAuMy4xOyBleHRyYSA9PSAnYWxsJwpSZXF1aXJlcy1EaXN0OiBzcGhpbngtYXV0b2RvYy10eXBlaGludHM+PTEuMjU7IGV4dHJhID09ICdhbGwnClJlcXVpcmVzLURpc3Q6IHNwaGlueC1jb3B5YnV0dG9uPj0wLjU7IGV4dHJhID09ICdhbGwnClJlcXVpcmVzLURpc3Q6IHNwaGlueC1ydGQtdGhlbWU+PTIuMDsgZXh0cmEgPT0gJ2FsbCcKUmVxdWlyZXMtRGlzdDogc3BoaW54Pj03LjA7IGV4dHJhID09ICdhbGwnClByb3ZpZGVzLUV4dHJhOiBjb2RleApSZXF1aXJlcy1EaXN0OiBzY2l0ZXgtZ2VuYWlbZ2F0ZXdheV0+PTAuMS40OyBleHRyYSA9PSAnY29kZXgnClByb3ZpZGVzLUV4dHJhOiBjb2RleC1zZGsKUmVxdWlyZXMtRGlzdDogb3BlbmFpLWNvZGV4Pj0wLjE0NC40OyBleHRyYSA9PSAnY29kZXgtc2RrJwpQcm92aWRlcy1FeHRyYTogZGV2ClJlcXVpcmVzLURpc3Q6IGRqYW5nbz49NC4yOyBleHRyYSA9PSAnZGV2JwpSZXF1aXJlcy1EaXN0OiBmYXN0bWNwPDQsPj0zLjA7IGV4dHJhID09ICdkZXYnClJlcXVpcmVzLURpc3Q6IG1jcDwyLD49MS4yOTsgZXh0cmEgPT0gJ2RldicKUmVxdWlyZXMtRGlzdDogb3BlbmFpLWNvZGV4Pj0wLjE0NC40OyBleHRyYSA9PSAnZGV2JwpSZXF1aXJlcy1EaXN0OiBwcmUtY29tbWl0Pj0zLjUuMDsgZXh0cmEgPT0gJ2RldicKUmVxdWlyZXMtRGlzdDogcHl0ZXN0LWFzeW5jaW8+PTAuMjM7IGV4dHJhID09ICdkZXYnClJlcXVpcmVzLURpc3Q6IHB5dGVzdC1jb3Y+PTQuMC4wOyBleHRyYSA9PSAnZGV2JwpSZXF1aXJlcy1EaXN0OiBweXRlc3QtdGltZW91dD49Mi4wOyBleHRyYSA9PSAnZGV2JwpSZXF1aXJlcy1EaXN0OiBweXRlc3QteGRpc3Q+PTMuMC4wOyBleHRyYSA9PSAnZGV2JwpSZXF1aXJlcy1EaXN0OiBweXRlc3Q8OS4wLjAsPj03LjAuMDsgZXh0cmEgPT0gJ2RldicKUmVxdWlyZXMtRGlzdDogc2NpdGV4LWdpdD49MC4zLjA7IGV4dHJhID09ICdkZXYnClJlcXVpcmVzLURpc3Q6IHNjaXRleC1ocGM+PTAuNi4yOyBleHRyYSA9PSAnZGV2JwpSZXF1aXJlcy1EaXN0OiBzY2l0ZXgtc2RrW2d1aV0+PTAuMy4xOyBleHRyYSA9PSAnZGV2JwpQcm92aWRlcy1FeHRyYTogZG9jcwpSZXF1aXJlcy1EaXN0OiBteXN0LXBhcnNlcj49Mi4wOyBleHRyYSA9PSAnZG9jcycKUmVxdWlyZXMtRGlzdDogc3BoaW54LWF1dG9kb2MtdHlwZWhpbnRzPj0xLjI1OyBleHRyYSA9PSAnZG9jcycKUmVxdWlyZXMtRGlzdDogc3BoaW54LWNvcHlidXR0b24+PTAuNTsgZXh0cmEgPT0gJ2RvY3MnClJlcXVpcmVzLURpc3Q6IHNwaGlueC1ydGQtdGhlbWU+PTIuMDsgZXh0cmEgPT0gJ2RvY3MnClJlcXVpcmVzLURpc3Q6IHNwaGlueD49Ny4wOyBleHRyYSA9PSAnZG9jcycKUHJvdmlkZXMtRXh0cmE6IGd1aQpSZXF1aXJlcy1EaXN0OiBkamFuZ28+PTQuMjsgZXh0cmEgPT0gJ2d1aScKUmVxdWlyZXMtRGlzdDogc2NpdGV4LXNka1tndWldPj0wLjMuMTsgZXh0cmEgPT0gJ2d1aScKUHJvdmlkZXMtRXh0cmE6IG1jcApSZXF1aXJlcy1EaXN0OiBmYXN0bWNwPDQsPj0yLjA7IGV4dHJhID09ICdtY3AnClJlcXVpcmVzLURpc3Q6IG1jcDwyLD49MS4yOTsgZXh0cmEgPT0gJ21jcCcKUHJvdmlkZXMtRXh0cmE6IG5vdGlmeQpSZXF1aXJlcy1EaXN0OiBzY2l0ZXgtbm90aWZpY2F0aW9uPj0wLjIuOTsgZXh0cmEgPT0gJ25vdGlmeScKUHJvdmlkZXMtRXh0cmE6IG9wZW5haQpSZXF1aXJlcy1EaXN0OiBvcGVuYWktYWdlbnRzPj0wLjE5LjA7IGV4dHJhID09ICdvcGVuYWknClByb3ZpZGVzLUV4dHJhOiBzbHVybQpSZXF1aXJlcy1EaXN0OiBzY2l0ZXgtaHBjPj0wLjYuMjsgZXh0cmEgPT0gJ3NsdXJtJwpQcm92aWRlcy1FeHRyYTogdGVsZWdyYW0KUmVxdWlyZXMtRGlzdDogY2xhdWRlLWNvZGUtdGVsZWdyYW1tZXI+PTAuNi4xOyBleHRyYSA9PSAndGVsZWdyYW0nCkRlc2NyaXB0aW9uLUNvbnRlbnQtVHlwZTogdGV4dC9tYXJrZG93bgo="
)
GENERATED_ENTRIES = {
    "scitex.apps": {
        "agents": "scitex_agent_container._django.apps:AgentContainerDashboardConfig"
    },
    "scitex_dev.docs": {"scitex-agent-container": "scitex_agent_container"},
    "scitex_dev.skills": {"scitex-agent-container": "scitex_agent_container"},
    "scitex_dev.linter.plugins": {
        "scitex-agent-container": "scitex_agent_container._linter_plugin:get_plugin"
    },
    "scitex_dev.jobs": {
        "scitex-agent-container": "scitex_agent_container._jobs._jobs_plugin:provide_jobs"
    },
    "scitex_dev.system_deps": {
        "scitex-agent-container": "scitex_agent_container._system_deps:provide"
    },
    "scitex_dev.store.plugins": {
        "scitex-agent-container": "scitex_agent_container._store_plugin:provide"
    },
    "scitex_cards.hooks": {
        "scitex-agent-container": "scitex_agent_container._listen._card_event_delivery:deliver_card_event"
    },
    "scitex_dev.hooks": {
        "scitex-agent-container": "scitex_agent_container._claude_hooks_plugin:provide_hooks"
    },
    "console_scripts": {
        "scitex-agent-container": "_scitex_agent_container_bootstrap:cli_entry_point",
        "sac": "_scitex_agent_container_bootstrap:cli_entry_point",
        "sac-statusline": "scitex_agent_container.statusline:main",
    },
}


@contextlib.contextmanager
def child_environment(values):
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


@contextlib.contextmanager
def arguments(values):
    previous = sys.argv
    sys.argv = list(values)
    try:
        yield
    finally:
        sys.argv = previous


@contextlib.contextmanager
def refuses(expected, pattern=None):
    observation = {}
    try:
        yield observation
    except expected as error:
        observation.update(type=type(error), reason=str(error))
        if pattern is not None and re.search(pattern, str(error)) is None:
            raise AssertionError("wrong refusal reason: " + str(error)) from error
    else:
        raise AssertionError("expected genuine refusal")


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        timeout=15,
        env={
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    ).stdout


def entry_bytes():
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read_dict(GENERATED_ENTRIES)
    stream = io.StringIO()
    parser.write(stream)
    return stream.getvalue().encode()


def stamp_bytes(root, commit):
    # Use the actual public normal code-hash producer, rather than the verifier.
    spec = importlib.util.spec_from_file_location(
        "public_sac_code_hash", ROOT / "src/scitex_agent_container/_provenance/_hash.py"
    )
    hashing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hashing)
    stamp = {
        "version": VERSION,
        "commit": commit,
        "commit_source": "env",
        "code_hash": hashing.code_hash(root / "src/scitex_agent_container"),
        "built_at": "2026-10-03T20:00:00Z",
    }
    return b'"""Generated build identity."""\nSTAMP = ' + repr(stamp).encode() + b"\n"


def zip_bytes(
    files, *, omit_record=False, record_mutation=None, modes=None, duplicate=None
):
    files = dict(files)
    output = io.StringIO(newline="")
    rows = csv.writer(output, lineterminator="\n")
    record = OWNER + "/RECORD"
    for name, data in sorted(files.items()):
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        )
        rows.writerow((name, "sha256=" + digest, str(len(data))))
    rows.writerow((record, "", ""))
    if not omit_record:
        files[record] = output.getvalue().encode()
    if record_mutation:
        files[record] = record_mutation(files[record])
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (modes or {}).get(name, stat.S_IFREG | 0o644) << 16
            archive.writestr(info, data)
        if duplicate:
            archive.writestr(duplicate, files[duplicate])
    return raw.getvalue()


def tar_bytes(files, *, extra=None):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz", compresslevel=1) as archive:
        for name, data in files.items():
            item = tarfile.TarInfo("scitex_agent_container-" + VERSION + "/" + name)
            item.mode = 0o644
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
        if extra is not None:
            archive.addfile(extra, io.BytesIO(b"x") if extra.isfile() else None)
    return raw.getvalue()


def archives(root, commit):
    raw = git(root, "archive", commit)
    source = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for item in archive:
            if item.isdir():
                continue
            if not item.isfile():
                raise AssertionError("public fixture has unsupported member")
            source[item.name] = archive.extractfile(item).read()
    source[STAMP] = stamp_bytes(root, commit)
    config = tomllib.loads(source["pyproject.toml"].decode())["tool"]["hatch"]["build"][
        "targets"
    ]["wheel"]
    wheel = {
        name.removeprefix("src/"): data
        for name, data in source.items()
        if any(name.startswith(package + "/") for package in config["packages"])
    }
    wheel.update(
        {
            destination: source[name]
            for name, destination in config["force-include"].items()
        }
    )
    wheel.update(
        {
            OWNER + "/METADATA": GENERATED_METADATA,
            OWNER
            + "/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            OWNER + "/entry_points.txt": entry_bytes(),
            OWNER + "/licenses/LICENSE": source["LICENSE"],
        }
    )
    source["PKG-INFO"] = GENERATED_METADATA
    return wheel, source


class ReleaseFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="sac-release-pure-")
        cls.root = Path(cls.temporary.name) / "source"
        cls.root.mkdir()
        project = (ROOT / "pyproject.toml").read_bytes()
        config = tomllib.loads(project.decode())["tool"]["hatch"]["build"]["targets"][
            "wheel"
        ]
        paths = set(config["force-include"]) | {
            "src/scitex_agent_container/__init__.py",
            "src/_scitex_agent_container_bootstrap/__init__.py",
            "src/scitex_agent_container/_provenance/_hash.py",
            "LICENSE",
            "CHANGELOG.md",
        }
        for name in paths:
            path = cls.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                project if name == "pyproject.toml" else (ROOT / name).read_bytes()
            )
        git(cls.root, "init", "--quiet")
        git(cls.root, "add", ".")
        git(
            cls.root,
            "-c",
            "user.name=Pure Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "public fixture",
        )
        cls.commit = git(cls.root, "rev-parse", "HEAD").decode().strip()
        cls.wheel, cls.sdist = archives(cls.root, cls.commit)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def payload(self, wheel=None, sdist=None):
        return RELEASE.source_payload_identity(
            zip_bytes(wheel or self.wheel),
            tar_bytes(sdist or self.sdist),
            self.commit,
            self.root,
        )

    def query(self, *, changes=None, missing=None):
        run = {
            "id": int(RUN),
            "run_attempt": int(ATTEMPT),
            "head_sha": self.commit,
            "repository": {"full_name": RELEASE.REPOSITORY},
            "event": "workflow_dispatch",
            "path": ".github/workflows/pypi-publish-and-github-release-on-tag.yml",
            "actor": {"login": "fixture-member"},
            "triggering_actor": {"login": "fixture-member"},
            "referenced_workflows": [
                {
                    "path": "scitex-ai/.github/.github/workflows/ci-sif-matrix.yml@refs/heads/main",
                    "sha": "b" * 40,
                    "ref": "refs/heads/main",
                }
            ],
        }
        jobs = [
            {
                "id": index,
                "name": "test / test-sac-py" + version,
                "status": "completed",
                "conclusion": "success",
                "head_sha": self.commit,
                "run_id": int(RUN),
                "run_attempt": int(ATTEMPT),
            }
            for index, version in enumerate(("3.11", "3.12", "3.13"), 101)
        ]
        metadata = git(self.root, "show", self.commit + ":pyproject.toml")
        run_prefix = PREFIX + "/actions/runs/" + RUN + "/attempts/" + ATTEMPT
        routes = {
            PREFIX + "/git/ref/tags/" + TAG: {
                "ref": "refs/tags/" + TAG,
                "object": {"type": "commit", "sha": self.commit},
            },
            PREFIX + "/compare/" + self.commit + "...develop": {
                "base_commit": {"sha": self.commit},
                "status": "identical",
            },
            PREFIX + "/contents/pyproject.toml?ref=" + self.commit: {
                "encoding": "base64",
                "content": base64.b64encode(metadata).decode(),
                "sha": hashlib.sha1(
                    b"blob " + str(len(metadata)).encode() + b"\0" + metadata
                ).hexdigest(),
            },
            run_prefix: run,
            run_prefix + "/jobs?per_page=100": {"total_count": 3, "jobs": jobs},
            PREFIX + "/actions/runs/" + RUN + "/artifacts?per_page=100": {
                "total_count": 1,
                "artifacts": [
                    {
                        "name": "dist-" + self.commit,
                        "expired": False,
                        "digest": "sha256:" + "a" * 64,
                        "id": 987,
                        "workflow_run": {"id": int(RUN), "head_sha": self.commit},
                    }
                ],
            },
        }
        if changes:
            changes(routes)
        if missing:
            routes.pop(missing)
        self.calls = []

        def lookup(path, status=200):
            self.calls.append(path)
            assert (status) == (200)
            assert (path) in (routes), "unexpected network authority"
            return copy.deepcopy(routes[path])

        return lookup

    def environment(self, **extra):
        return {
            "GITHUB_REPOSITORY": RELEASE.REPOSITORY,
            "RELEASE_AUTHORIZED": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_SHA": self.commit,
            "GITHUB_ACTOR": "fixture-member",
            "GITHUB_TRIGGERING_ACTOR": "fixture-member",
            "GITHUB_RUN_ID": RUN,
            "GITHUB_RUN_ATTEMPT": ATTEMPT,
            "RELEASE_ARTIFACT_DIGEST": "a" * 64,
            **extra,
        }

    def test_actual_two_package_source_and_force_include_payload_whole_membership(self):
        # Arrange
        commit = git(ROOT, "rev-parse", "HEAD").decode().strip()
        wheel, sdist = archives(ROOT, commit)
        # Act
        result = RELEASE.source_payload_identity(
            zip_bytes(wheel), tar_bytes(sdist), commit, ROOT
        )
        # Assert
        assert (result["wheel_public_members"]) > (1300)

    def test_actual_two_package_source_and_force_include_payload_runtime_requirements(
        self,
    ):
        # Arrange
        commit = git(ROOT, "rev-parse", "HEAD").decode().strip()
        wheel, sdist = archives(ROOT, commit)
        # Act
        result = RELEASE.source_payload_identity(
            zip_bytes(wheel), tar_bytes(sdist), commit, ROOT
        )
        # Assert
        assert (result["generated_metadata"]["runtime_requirements"]) == (70)

    def test_actual_two_package_source_and_force_include_payload_commit(self):
        # Arrange
        commit = git(ROOT, "rev-parse", "HEAD").decode().strip()
        wheel, sdist = archives(ROOT, commit)
        # Act
        result = RELEASE.source_payload_identity(
            zip_bytes(wheel), tar_bytes(sdist), commit, ROOT
        )
        # Assert
        assert (result["provenance"]["commit"]) == (commit)

    def test_real_git_fixture_and_generated_provenance_commit(self):
        # Arrange
        fixture = self.commit
        # Act
        result = self.payload()
        # Assert
        assert (result["git_commit"]) == (fixture)

    def test_real_git_fixture_and_generated_provenance_extras(self):
        # Arrange
        # Act
        result = self.payload()
        # Assert
        assert (result["generated_metadata"]["extras"]) == (11)

    def test_tag_rejects_shell_escape_traversal_and_substring_typed_refusal(self):
        # Arrange
        tags = [
            "v0.29.4;id",
            "v0.29.4\n",
            "refs/tags/v0.29.4",
            "../v0.29.4",
            "v00.29.4",
            "v0.29.4x",
        ]
        # Act
        for tag in tags:
            with self.subTest(tag=tag), refuses(ValueError) as refusal:
                RELEASE.resolve(tag, self.query())
            # Assert
            assert (
                issubclass(refusal["type"], ValueError),
                bool(refusal["reason"]),
            ) == (True, True)

    def test_tag_rejects_shell_escape_traversal_and_substring_no_query(self):
        # Arrange
        tags = [
            "v0.29.4;id",
            "v0.29.4\n",
            "refs/tags/v0.29.4",
            "../v0.29.4",
            "v00.29.4",
            "v0.29.4x",
        ]
        # Act
        for tag in tags:
            with self.subTest(tag=tag), refuses(ValueError):
                RELEASE.resolve(tag, self.query())
            # Assert
        assert (self.calls) == ([])

    def test_lightweight_tag_exact_git_metadata(self):
        # Arrange
        query = self.query()
        # Act
        result = RELEASE.resolve(TAG, query)
        # Assert
        assert (result) == ({"tag": TAG, "commit": self.commit, "version": VERSION})

    def test_annotation_cycle_is_refused(self):
        # Arrange
        def changes(routes):
            routes[PREFIX + "/git/ref/tags/" + TAG]["object"] = {
                "type": "tag",
                "sha": "c" * 40,
            }
            routes[PREFIX + "/git/tags/" + "c" * 40] = {
                "object": {"type": "tag", "sha": "c" * 40}
            }

        # Act
        with refuses(ValueError, "cyclic") as refusal:
            RELEASE.resolve(TAG, self.query(changes=changes))
        # Assert
        assert (issubclass(refusal["type"], ValueError), bool(refusal["reason"])) == (
            True,
            True,
        )

    def test_unaccepted_develop_history_is_refused(self):
        # Arrange
        def changes(routes):
            routes[PREFIX + "/compare/" + self.commit + "...develop"]["status"] = (
                "diverged"
            )

        # Act
        with refuses(ValueError, "develop") as refusal:
            RELEASE.resolve(TAG, self.query(changes=changes))
        # Assert
        assert (issubclass(refusal["type"], ValueError), bool(refusal["reason"])) == (
            True,
            True,
        )

    def test_tag_metadata_source_version_is_refused(self):
        # Arrange
        def changes(routes):
            record = routes[PREFIX + "/contents/pyproject.toml?ref=" + self.commit]
            raw = base64.b64decode(record["content"]).replace(
                b'version = "0.29.4"', b'version = "0.29.40"', 1
            )
            record.update(
                content=base64.b64encode(raw).decode(),
                sha=hashlib.sha1(
                    b"blob " + str(len(raw)).encode() + b"\0" + raw
                ).hexdigest(),
            )

        # Act
        with refuses(ValueError, "metadata differ") as refusal:
            RELEASE.resolve(TAG, self.query(changes=changes))
        # Assert
        assert (issubclass(refusal["type"], ValueError), bool(refusal["reason"])) == (
            True,
            True,
        )

    def test_shared_unknown_admission_refuses_before_query(self):
        # Arrange
        for authorized in ("false", "", "unknown", "True"):
            # Act
            with (
                self.subTest(authorized=authorized),
                child_environment(self.environment(RELEASE_AUTHORIZED=authorized)),
                refuses(ValueError) as refusal,
            ):
                RELEASE.release_context()
            # Assert
            assert (
                issubclass(refusal["type"], ValueError),
                bool(refusal["reason"]),
            ) == (True, True)

    def test_dispatch_older_tag_refuses_before_output_typed_refusal(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            env = self.environment(GITHUB_SHA="f" * 40, GITHUB_OUTPUT=str(output))
            # Act
            with (
                child_environment(env),
                arguments(["release-identity.py", "resolve", "--tag", TAG]),
                refuses(ValueError, "requested tag") as refusal,
            ):
                RELEASE.main(self.query())
            # Assert
            assert (
                issubclass(refusal["type"], ValueError),
                bool(refusal["reason"]),
            ) == (True, True)

    def test_dispatch_older_tag_refuses_before_output_no_output(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            env = self.environment(GITHUB_SHA="f" * 40, GITHUB_OUTPUT=str(output))
            # Act
            with (
                child_environment(env),
                arguments(["release-identity.py", "resolve", "--tag", TAG]),
                refuses(ValueError, "requested tag"),
            ):
                RELEASE.main(self.query())
            # Assert
            assert not (output.exists())

    def test_three_completed_actual_job_witnesses(self):
        # Arrange
        with child_environment(self.environment()):
            # Act
            result = RELEASE.run_witness(self.commit, RUN, ATTEMPT, self.query())
        # Assert
        assert ([row["version"] for row in result["tests"]]) == (
            ["3.11", "3.12", "3.13"]
        )

    def test_stale_run_source_attempt_and_missing_minor_refuse(self):
        # Arrange
        run_key = PREFIX + "/actions/runs/" + RUN + "/attempts/" + ATTEMPT
        mutations = [
            lambda r: r[run_key].update(head_sha="f" * 40),
            lambda r: r[run_key].update(run_attempt=1),
            lambda r: r[run_key + "/jobs?per_page=100"]["jobs"][1].update(
                conclusion="skipped"
            ),
            lambda r: r[run_key + "/jobs?per_page=100"]["jobs"][1].update(
                run_attempt=1
            ),
            lambda r: r[run_key + "/jobs?per_page=100"]["jobs"].pop(),
            lambda r: r[run_key].update(referenced_workflows=[]),
        ]
        # Act
        for mutation in mutations:
            with (
                self.subTest(mutation=mutation),
                child_environment(self.environment()),
                refuses(ValueError) as refusal,
            ):
                RELEASE.run_witness(
                    self.commit, RUN, ATTEMPT, self.query(changes=mutation)
                )
            # Assert
            assert (
                issubclass(refusal["type"], ValueError),
                bool(refusal["reason"]),
            ) == (True, True)

    def test_artifact_transport_digest_source_and_expiration_accepted_id(self):
        # Arrange
        query = self.query()
        # Act
        result = RELEASE.transport_witness(self.commit, RUN, "a" * 64, query)
        # Assert
        assert result["id"] == 987
        # Assert

    def test_artifact_transport_digest_source_and_expiration_forged_transport(self):
        # Arrange
        key = PREFIX + "/actions/runs/" + RUN + "/artifacts?per_page=100"
        mutations = [
            lambda r: r[key]["artifacts"][0].update(digest="sha256:" + "f" * 64),
            lambda r: r[key]["artifacts"][0].update(expired=True),
            lambda r: r[key]["artifacts"][0]["workflow_run"].update(head_sha="f" * 40),
        ]
        # Act
        for mutation in mutations:
            with self.subTest(mutation=mutation), refuses(ValueError) as refusal:
                RELEASE.transport_witness(
                    self.commit, RUN, "a" * 64, self.query(changes=mutation)
                )
            # Assert
            assert (
                issubclass(refusal["type"], ValueError),
                bool(refusal["reason"]),
            ) == (True, True)

    def test_rehashed_whole_wheel_source_tamper_refuses(self):
        # Arrange
        wheel = dict(self.wheel)
        wheel["scitex_agent_container/__init__.py"] += b"\nforged = True\n"
        # Act
        with refuses(ValueError, "source bytes") as refusal:
            self.payload(wheel=wheel)
        # Assert
        assert (issubclass(refusal["type"], ValueError), bool(refusal["reason"])) == (
            True,
            True,
        )

    def test_rehashed_missing_bootstrap_and_extra_payload_refuse(self):
        # Arrange
        for key, remove in (
            ("_scitex_agent_container_bootstrap/__init__.py", True),
            ("scitex_agent_container/foreign.py", False),
            (OWNER + "/unreviewed.json", False),
        ):
            wheel = dict(self.wheel)
            if remove:
                wheel.pop(key)
            else:
                wheel[key] = b"foreign"
            # Act
            with self.subTest(key=key), refuses(ValueError) as refusal:
                self.payload(wheel=wheel)
            # Assert
            assert (
                issubclass(refusal["type"], ValueError),
                bool(refusal["reason"]),
            ) == (True, True)

    def test_rehashed_provenance_commit_hash_and_executable_body_refuse(self):
        # Arrange
        for before, after in (
            (self.commit.encode(), b"f" * 40),
            (b"'code_hash': '", b"'code_hash': 'forged"),
            (b"STAMP =", b"import os\nSTAMP ="),
        ):
            wheel = dict(self.wheel)
            key = STAMP.removeprefix("src/")
            wheel[key] = wheel[key].replace(before, after)
            # Act
            with self.subTest(before=before), refuses(ValueError) as refusal:
                self.payload(wheel=wheel)
            # Assert
            assert (
                issubclass(refusal["type"], ValueError),
                bool(refusal["reason"]),
            ) == (True, True)

    def test_rehashed_metadata_requirement_extra_entry_license_and_wheel_refuse_fixture_input(
        self,
    ):
        # Arrange
        cases = [
            ("METADATA", b"Requires-Dist: click>=8.2", b"Requires-Dist: click>=0.1"),
            (
                "METADATA",
                b"Provides-Extra: all",
                b"Provides-Extra: all\nProvides-Extra: All",
            ),
            ("METADATA", b"License-File: LICENSE", b"License-File: OTHER"),
            (
                "entry_points.txt",
                b"_scitex_agent_container_bootstrap:cli_entry_point",
                b"foreign:main",
            ),
            ("licenses/LICENSE", b"GNU", b"FORGED"),
            ("WHEEL", b"Root-Is-Purelib: true", b"Root-Is-Purelib: false"),
            ("WHEEL", b"Tag: py3-none-any", b"Tag: cp312-cp312-linux_x86_64"),
        ]
        # Act
        present = [
            before in self.wheel[OWNER + "/" + name] for name, before, _ in cases
        ]
        # Assert
        assert present == [True] * len(cases)
        # Assert

    def test_rehashed_metadata_requirement_extra_entry_license_and_wheel_refuse_forged_metadata(
        self,
    ):
        # Arrange
        cases = [
            ("METADATA", b"Requires-Dist: click>=8.2", b"Requires-Dist: click>=0.1"),
            (
                "METADATA",
                b"Provides-Extra: all",
                b"Provides-Extra: all\nProvides-Extra: All",
            ),
            ("METADATA", b"License-File: LICENSE", b"License-File: OTHER"),
            (
                "entry_points.txt",
                b"_scitex_agent_container_bootstrap:cli_entry_point",
                b"foreign:main",
            ),
            ("licenses/LICENSE", b"GNU", b"FORGED"),
            ("WHEEL", b"Root-Is-Purelib: true", b"Root-Is-Purelib: false"),
            ("WHEEL", b"Tag: py3-none-any", b"Tag: cp312-cp312-linux_x86_64"),
        ]
        for name, before, after in cases:
            wheel = dict(self.wheel)
            key = OWNER + "/" + name
            wheel[key] = wheel[key].replace(before, after)
            # Act
            with self.subTest(name=name), refuses(ValueError) as refusal:
                self.payload(wheel=wheel)
            # Assert
            assert (
                issubclass(refusal["type"], ValueError),
                bool(refusal["reason"]),
            ) == (True, True)

    def test_record_tamper_and_archive_special_type_refuse(self):
        # Arrange
        key = "scitex_agent_container/__init__.py"
        variants = [
            zip_bytes(
                self.wheel,
                record_mutation=lambda b: b.replace(b"sha256=", b"sha512=", 1),
            ),
            zip_bytes(self.wheel, modes={key: stat.S_IFLNK | 0o777}),
            zip_bytes({**self.wheel, "../escape": b"x"}),
        ]
        # Act
        for raw in variants:
            with self.subTest(size=len(raw)), refuses(ValueError) as refusal:
                RELEASE.wheel_identity(raw, VERSION)
            # Assert
            assert (
                issubclass(refusal["type"], ValueError),
                bool(refusal["reason"]),
            ) == (True, True)

    def test_tar_escape_link_multi_root_and_untracked_source_refuse_archive_shape(self):
        # Arrange
        variants = []
        for name, kind in (
            ("../escape", tarfile.REGTYPE),
            ("foreign/root", tarfile.REGTYPE),
            ("scitex_agent_container-0.29.4/link", tarfile.SYMTYPE),
        ):
            item = tarfile.TarInfo(name)
            item.type = kind
            item.linkname = "../outside"
            item.size = 1 if kind == tarfile.REGTYPE else 0
            variants.append(tar_bytes(self.sdist, extra=item))
        # Act
        for raw in variants:
            with self.subTest(size=len(raw)), refuses(ValueError) as refusal:
                RELEASE.sdist_identity(raw, VERSION)
            # Assert
            assert (
                issubclass(refusal["type"], ValueError),
                bool(refusal["reason"]),
            ) == (True, True)
        sdist = dict(self.sdist)
        sdist["untracked.py"] = b"unknown"
        with refuses(ValueError) as refusal:
            self.payload(sdist=sdist)
        # Assert

    def test_tar_escape_link_multi_root_and_untracked_source_refuse_untracked_payload(
        self,
    ):
        # Arrange
        variants = []
        for name, kind in (
            ("../escape", tarfile.REGTYPE),
            ("foreign/root", tarfile.REGTYPE),
            ("scitex_agent_container-0.29.4/link", tarfile.SYMTYPE),
        ):
            item = tarfile.TarInfo(name)
            item.type = kind
            item.linkname = "../outside"
            item.size = 1 if kind == tarfile.REGTYPE else 0
            variants.append(tar_bytes(self.sdist, extra=item))
        # Act
        for raw in variants:
            with self.subTest(size=len(raw)), refuses(ValueError) as refusal:
                RELEASE.sdist_identity(raw, VERSION)
            # Assert
        sdist = dict(self.sdist)
        sdist["untracked.py"] = b"unknown"
        with refuses(ValueError) as refusal:
            self.payload(sdist=sdist)
        # Assert
        assert (issubclass(refusal["type"], ValueError), bool(refusal["reason"])) == (
            True,
            True,
        )

    def test_rehashed_sdist_source_version_and_body_refuse(self):
        # Arrange
        for key, before, after in (
            ("pyproject.toml", b'version = "0.29.4"', b'version = "0.29.40"'),
            ("src/scitex_agent_container/__init__.py", b"", b"forged"),
        ):
            sdist = dict(self.sdist)
            sdist[key] = (
                after + sdist[key]
                if not before
                else sdist[key].replace(before, after, 1)
            )
            # Act
            with self.subTest(key=key), refuses(ValueError) as refusal:
                self.payload(sdist=sdist)
            # Assert
            assert (
                issubclass(refusal["type"], ValueError),
                bool(refusal["reason"]),
            ) == (True, True)

    def test_artifact_extra_symlink_and_filename_substring_refuse_accepted_pair(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            wheel = path / "scitex_agent_container-0.29.4-py3-none-any.whl"
            wheel.write_bytes(zip_bytes(self.wheel))
            (path / "scitex_agent_container-0.29.4.tar.gz").write_bytes(
                tar_bytes(self.sdist)
            )
            # Act
            valid = RELEASE.artifact_proof(
                path, TAG, self.commit, RUN, ATTEMPT, self.root
            )
            # Assert
            assert (len(valid["files"])) == (2)
            (path / "extra.txt").write_text("unknown")
            with refuses(ValueError):
                RELEASE.artifact_proof(path, TAG, self.commit, RUN, ATTEMPT, self.root)
            # Assert
            (path / "extra.txt").unlink()
            wheel.rename(
                wheel.with_name("scitex_agent_container-0.29.40-py3-none-any.whl")
            )
            with refuses(ValueError):
                RELEASE.artifact_proof(path, TAG, self.commit, RUN, ATTEMPT, self.root)
            # Assert
            wheel.symlink_to(
                wheel.with_name("scitex_agent_container-0.29.40-py3-none-any.whl")
            )
            with refuses(ValueError):
                RELEASE.regular_bytes(wheel)
            # Assert

    def test_artifact_extra_symlink_and_filename_substring_refuse_extra_payload(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            wheel = path / "scitex_agent_container-0.29.4-py3-none-any.whl"
            wheel.write_bytes(zip_bytes(self.wheel))
            (path / "scitex_agent_container-0.29.4.tar.gz").write_bytes(
                tar_bytes(self.sdist)
            )
            # Act
            RELEASE.artifact_proof(path, TAG, self.commit, RUN, ATTEMPT, self.root)
            # Assert
            (path / "extra.txt").write_text("unknown")
            with refuses(ValueError) as refusal:
                RELEASE.artifact_proof(path, TAG, self.commit, RUN, ATTEMPT, self.root)
            # Assert
            assert (
                issubclass(refusal["type"], ValueError),
                bool(refusal["reason"]),
            ) == (True, True)
            (path / "extra.txt").unlink()
            wheel.rename(
                wheel.with_name("scitex_agent_container-0.29.40-py3-none-any.whl")
            )
            with refuses(ValueError) as refusal:
                RELEASE.artifact_proof(path, TAG, self.commit, RUN, ATTEMPT, self.root)
            # Assert
            wheel.symlink_to(
                wheel.with_name("scitex_agent_container-0.29.40-py3-none-any.whl")
            )
            with refuses(ValueError) as refusal:
                RELEASE.regular_bytes(wheel)
            # Assert

    def test_artifact_extra_symlink_and_filename_substring_refuse_filename_version(
        self,
    ):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            wheel = path / "scitex_agent_container-0.29.4-py3-none-any.whl"
            wheel.write_bytes(zip_bytes(self.wheel))
            (path / "scitex_agent_container-0.29.4.tar.gz").write_bytes(
                tar_bytes(self.sdist)
            )
            # Act
            RELEASE.artifact_proof(path, TAG, self.commit, RUN, ATTEMPT, self.root)
            # Assert
            (path / "extra.txt").write_text("unknown")
            with refuses(ValueError) as refusal:
                RELEASE.artifact_proof(path, TAG, self.commit, RUN, ATTEMPT, self.root)
            # Assert
            (path / "extra.txt").unlink()
            wheel.rename(
                wheel.with_name("scitex_agent_container-0.29.40-py3-none-any.whl")
            )
            with refuses(ValueError) as refusal:
                RELEASE.artifact_proof(path, TAG, self.commit, RUN, ATTEMPT, self.root)
            # Assert
            assert (
                issubclass(refusal["type"], ValueError),
                bool(refusal["reason"]),
            ) == (True, True)
            wheel.symlink_to(
                wheel.with_name("scitex_agent_container-0.29.40-py3-none-any.whl")
            )
            with refuses(ValueError) as refusal:
                RELEASE.regular_bytes(wheel)
            # Assert

    def test_artifact_extra_symlink_and_filename_substring_refuse_symlink(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            wheel = path / "scitex_agent_container-0.29.4-py3-none-any.whl"
            wheel.write_bytes(zip_bytes(self.wheel))
            (path / "scitex_agent_container-0.29.4.tar.gz").write_bytes(
                tar_bytes(self.sdist)
            )
            # Act
            RELEASE.artifact_proof(path, TAG, self.commit, RUN, ATTEMPT, self.root)
            # Assert
            (path / "extra.txt").write_text("unknown")
            with refuses(ValueError) as refusal:
                RELEASE.artifact_proof(path, TAG, self.commit, RUN, ATTEMPT, self.root)
            # Assert
            (path / "extra.txt").unlink()
            wheel.rename(
                wheel.with_name("scitex_agent_container-0.29.40-py3-none-any.whl")
            )
            with refuses(ValueError) as refusal:
                RELEASE.artifact_proof(path, TAG, self.commit, RUN, ATTEMPT, self.root)
            # Assert
            wheel.symlink_to(
                wheel.with_name("scitex_agent_container-0.29.40-py3-none-any.whl")
            )
            with refuses(ValueError) as refusal:
                RELEASE.regular_bytes(wheel)
            # Assert
            assert (
                issubclass(refusal["type"], ValueError),
                bool(refusal["reason"]),
            ) == (True, True)

    def test_proof_roundtrip_and_rehashed_stale_proof_refuse(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory)
            (dist / "scitex_agent_container-0.29.4-py3-none-any.whl").write_bytes(
                zip_bytes(self.wheel)
            )
            (dist / "scitex_agent_container-0.29.4.tar.gz").write_bytes(
                tar_bytes(self.sdist)
            )
            with (
                contextlib.chdir(self.root),
                child_environment(self.environment()),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                for mode in ("write-proof", "verify-proof"):
                    with arguments(
                        [
                            "release-identity.py",
                            mode,
                            "--tag",
                            TAG,
                            "--commit",
                            self.commit,
                            "--dist",
                            str(dist),
                        ]
                    ):
                        # Act
                        RELEASE.main(self.query())
                record = json.loads((dist / RELEASE.PROOF).read_bytes())
                record["run"] = "773310"
                (dist / RELEASE.PROOF).write_text(json.dumps(record))
                # Assert
                with (
                    arguments(
                        [
                            "release-identity.py",
                            "verify-proof",
                            "--tag",
                            TAG,
                            "--commit",
                            self.commit,
                            "--dist",
                            str(dist),
                        ]
                    ),
                    refuses(ValueError, "proof") as refusal,
                ):
                    RELEASE.main(self.query())
                # Assert
                assert (
                    issubclass(refusal["type"], ValueError),
                    bool(refusal["reason"]),
                ) == (True, True)

    def test_served_index_requires_exact_uploaded_bytes_accepted_pair(self):
        # Arrange
        proof = {
            "tag": TAG,
            "files": [
                {"name": "one.whl", "sha256": "a" * 64, "bytes": 10},
                {"name": "two.tar.gz", "sha256": "b" * 64, "bytes": 20},
            ],
        }
        document = {
            "info": {"name": "scitex-agent-container", "version": VERSION},
            "urls": [
                {
                    "filename": row["name"],
                    "digests": {"sha256": row["sha256"]},
                    "size": row["bytes"],
                    "packagetype": "bdist_wheel"
                    if row["name"].endswith(".whl")
                    else "sdist",
                }
                for row in proof["files"]
            ],
        }
        # Act
        result = RELEASE.index_identity(document, proof)
        # Assert
        assert (result["files"]) == (2)
        document["urls"][0]["digests"]["sha256"] = "f" * 64
        with refuses(ValueError):
            RELEASE.index_identity(document, proof)
        # Assert

    def test_served_index_requires_exact_uploaded_bytes_digest_refusal(self):
        # Arrange
        proof = {
            "tag": TAG,
            "files": [
                {"name": "one.whl", "sha256": "a" * 64, "bytes": 10},
                {"name": "two.tar.gz", "sha256": "b" * 64, "bytes": 20},
            ],
        }
        document = {
            "info": {"name": "scitex-agent-container", "version": VERSION},
            "urls": [
                {
                    "filename": row["name"],
                    "digests": {"sha256": row["sha256"]},
                    "size": row["bytes"],
                    "packagetype": "bdist_wheel"
                    if row["name"].endswith(".whl")
                    else "sdist",
                }
                for row in proof["files"]
            ],
        }
        # Act
        RELEASE.index_identity(document, proof)
        # Assert
        document["urls"][0]["digests"]["sha256"] = "f" * 64
        with refuses(ValueError) as refusal:
            RELEASE.index_identity(document, proof)
        # Assert
        assert (issubclass(refusal["type"], ValueError), bool(refusal["reason"])) == (
            True,
            True,
        )

    def test_build_uv_failure_has_no_raw_pip_fallback_or_output_exit(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/ci").mkdir(parents=True)
            (root / "scratch").mkdir()
            (root / "bin").mkdir()
            (root / "pyproject.toml").write_bytes(
                (ROOT / "pyproject.toml").read_bytes()
            )
            # The network authority witness is qualified independently above;
            # this fixture refuses at the actual external install boundary.
            (root / ".github/ci/release-identity.py").write_text(
                'print("owned test witness fixture")\n'
            )
            uv = root / "bin/uv"
            uv.write_text("#!/bin/sh\necho exact-uv-refusal >&2\nexit 23\n")
            uv.chmod(0o755)
            env = {
                **self.environment(),
                "PATH": str(root / "bin") + ":/usr/bin:/bin",
                "BUILD_PYTHON": sys.executable,
                "RELEASE_TAG": TAG,
                "RELEASE_COMMIT": self.commit,
                "RUNNER_TEMP": str(root / "scratch"),
            }
            # Act
            result = subprocess.run(
                ["bash", str(HERE / "build-in-sif.sh"), "3.12"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Assert
            assert (result.returncode) == (23), result.stderr

    def test_build_uv_failure_has_no_raw_pip_fallback_or_output_diagnostic(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/ci").mkdir(parents=True)
            (root / "scratch").mkdir()
            (root / "bin").mkdir()
            (root / "pyproject.toml").write_bytes(
                (ROOT / "pyproject.toml").read_bytes()
            )
            # The network authority witness is qualified independently above;
            # this fixture refuses at the actual external install boundary.
            (root / ".github/ci/release-identity.py").write_text(
                'print("owned test witness fixture")\n'
            )
            uv = root / "bin/uv"
            uv.write_text("#!/bin/sh\necho exact-uv-refusal >&2\nexit 23\n")
            uv.chmod(0o755)
            env = {
                **self.environment(),
                "PATH": str(root / "bin") + ":/usr/bin:/bin",
                "BUILD_PYTHON": sys.executable,
                "RELEASE_TAG": TAG,
                "RELEASE_COMMIT": self.commit,
                "RUNNER_TEMP": str(root / "scratch"),
            }
            # Act
            result = subprocess.run(
                ["bash", str(HERE / "build-in-sif.sh"), "3.12"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Assert
            assert ("exact-uv-refusal") in (result.stderr)

    def test_build_uv_failure_has_no_raw_pip_fallback_or_output_no_artifact(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/ci").mkdir(parents=True)
            (root / "scratch").mkdir()
            (root / "bin").mkdir()
            (root / "pyproject.toml").write_bytes(
                (ROOT / "pyproject.toml").read_bytes()
            )
            # The network authority witness is qualified independently above;
            # this fixture refuses at the actual external install boundary.
            (root / ".github/ci/release-identity.py").write_text(
                'print("owned test witness fixture")\n'
            )
            uv = root / "bin/uv"
            uv.write_text("#!/bin/sh\necho exact-uv-refusal >&2\nexit 23\n")
            uv.chmod(0o755)
            env = {
                **self.environment(),
                "PATH": str(root / "bin") + ":/usr/bin:/bin",
                "BUILD_PYTHON": sys.executable,
                "RELEASE_TAG": TAG,
                "RELEASE_COMMIT": self.commit,
                "RUNNER_TEMP": str(root / "scratch"),
            }
            # Act
            subprocess.run(
                ["bash", str(HERE / "build-in-sif.sh"), "3.12"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Assert
            assert not ((root / "dist").exists())

    def test_build_uv_failure_has_no_raw_pip_fallback_or_output_cleanup(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/ci").mkdir(parents=True)
            (root / "scratch").mkdir()
            (root / "bin").mkdir()
            (root / "pyproject.toml").write_bytes(
                (ROOT / "pyproject.toml").read_bytes()
            )
            # The network authority witness is qualified independently above;
            # this fixture refuses at the actual external install boundary.
            (root / ".github/ci/release-identity.py").write_text(
                'print("owned test witness fixture")\n'
            )
            uv = root / "bin/uv"
            uv.write_text("#!/bin/sh\necho exact-uv-refusal >&2\nexit 23\n")
            uv.chmod(0o755)
            env = {
                **self.environment(),
                "PATH": str(root / "bin") + ":/usr/bin:/bin",
                "BUILD_PYTHON": sys.executable,
                "RELEASE_TAG": TAG,
                "RELEASE_COMMIT": self.commit,
                "RUNNER_TEMP": str(root / "scratch"),
            }
            # Act
            subprocess.run(
                ["bash", str(HERE / "build-in-sif.sh"), "3.12"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Assert
            assert (list((root / "scratch").iterdir())) == ([])

    def test_publisher_refuses_before_oidc_and_installer_exit(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/ci").mkdir(parents=True)
            (root / "scratch").mkdir()
            (root / "bin").mkdir()
            (root / ".github/ci/release-identity.py").write_bytes(
                (HERE / "release-identity.py").read_bytes()
            )
            uv = root / "bin/uv"
            uv.write_text("#!/bin/sh\necho forbidden-installer >&2\nexit 91\n")
            uv.chmod(0o755)
            env = {
                **self.environment(RELEASE_AUTHORIZED="false"),
                "PATH": str(root / "bin") + ":/usr/bin:/bin",
                "PUBLISH_PYTHON": sys.executable,
                "RELEASE_TAG": TAG,
                "RELEASE_COMMIT": self.commit,
                "RUNNER_TEMP": str(root / "scratch"),
            }
            # Act
            result = subprocess.run(
                ["bash", str(HERE / "publish-in-sif.sh"), "3.12"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Assert
            assert (result.returncode) != (0)

    def test_publisher_refuses_before_oidc_and_installer_admission_reason(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/ci").mkdir(parents=True)
            (root / "scratch").mkdir()
            (root / "bin").mkdir()
            (root / ".github/ci/release-identity.py").write_bytes(
                (HERE / "release-identity.py").read_bytes()
            )
            uv = root / "bin/uv"
            uv.write_text("#!/bin/sh\necho forbidden-installer >&2\nexit 91\n")
            uv.chmod(0o755)
            env = {
                **self.environment(RELEASE_AUTHORIZED="false"),
                "PATH": str(root / "bin") + ":/usr/bin:/bin",
                "PUBLISH_PYTHON": sys.executable,
                "RELEASE_TAG": TAG,
                "RELEASE_COMMIT": self.commit,
                "RUNNER_TEMP": str(root / "scratch"),
            }
            # Act
            result = subprocess.run(
                ["bash", str(HERE / "publish-in-sif.sh"), "3.12"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Assert
            assert ("admission is not confirmed") in (result.stderr)

    def test_publisher_refuses_before_oidc_and_installer_no_installer(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/ci").mkdir(parents=True)
            (root / "scratch").mkdir()
            (root / "bin").mkdir()
            (root / ".github/ci/release-identity.py").write_bytes(
                (HERE / "release-identity.py").read_bytes()
            )
            uv = root / "bin/uv"
            uv.write_text("#!/bin/sh\necho forbidden-installer >&2\nexit 91\n")
            uv.chmod(0o755)
            env = {
                **self.environment(RELEASE_AUTHORIZED="false"),
                "PATH": str(root / "bin") + ":/usr/bin:/bin",
                "PUBLISH_PYTHON": sys.executable,
                "RELEASE_TAG": TAG,
                "RELEASE_COMMIT": self.commit,
                "RUNNER_TEMP": str(root / "scratch"),
            }
            # Act
            result = subprocess.run(
                ["bash", str(HERE / "publish-in-sif.sh"), "3.12"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Assert
            assert ("forbidden-installer") not in (result.stderr)

    def test_publisher_refuses_before_oidc_and_installer_cleanup(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/ci").mkdir(parents=True)
            (root / "scratch").mkdir()
            (root / "bin").mkdir()
            (root / ".github/ci/release-identity.py").write_bytes(
                (HERE / "release-identity.py").read_bytes()
            )
            uv = root / "bin/uv"
            uv.write_text("#!/bin/sh\necho forbidden-installer >&2\nexit 91\n")
            uv.chmod(0o755)
            env = {
                **self.environment(RELEASE_AUTHORIZED="false"),
                "PATH": str(root / "bin") + ":/usr/bin:/bin",
                "PUBLISH_PYTHON": sys.executable,
                "RELEASE_TAG": TAG,
                "RELEASE_COMMIT": self.commit,
                "RUNNER_TEMP": str(root / "scratch"),
            }
            # Act
            subprocess.run(
                ["bash", str(HERE / "publish-in-sif.sh"), "3.12"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Assert
            assert (list((root / "scratch").iterdir())) == ([])


if __name__ == "__main__":
    print(
        json.dumps(
            {
                "helper_origin": str(Path(RELEASE.__file__).resolve()),
                "helper_sha256": hashlib.sha256(
                    Path(RELEASE.__file__).read_bytes()
                ).hexdigest(),
                "scope": "pure source/archives/owned fixtures; no live release",
            }
        ),
        flush=True,
    )
    unittest.main()
