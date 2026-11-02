"""Exercise real files and the exact sourced release runtime verifier."""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / ".github/ci/verify-release-runtime.sh"


def verify(path, expected):
    return subprocess.run(["bash", "-ec", '. "$1"; sac_verified_file "$2" "$3"', "verify", str(LIBRARY), str(path), expected], capture_output=True, text=True, timeout=5, env={"PATH": os.defpath})


def test_real_regular_file_with_reviewed_bytes_is_accepted(tmp_path):
    # Arrange
    path = tmp_path / "runtime"
    path.write_bytes(b"owned runtime fixture")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    # Act
    result = verify(path, digest)
    # Assert
    assert (result.returncode, result.stdout.strip()) == (0, str(path))


def test_changed_runtime_bytes_refuse(tmp_path):
    # Arrange
    path = tmp_path / "runtime"
    path.write_bytes(b"different bytes")
    # Act
    result = verify(path, hashlib.sha256(b"qualified bytes").hexdigest())
    # Assert
    assert result.returncode != 0


def test_missing_runtime_file_refuses(tmp_path):
    # Arrange
    path = tmp_path / "missing-runtime"
    # Act
    result = verify(path, hashlib.sha256(b"qualified bytes").hexdigest())
    # Assert
    assert result.returncode != 0


def test_directory_cannot_substitute_for_runtime_file(tmp_path):
    # Arrange
    path = tmp_path / "runtime-directory"
    path.mkdir()
    # Act
    result = verify(path, hashlib.sha256(b"qualified bytes").hexdigest())
    # Assert
    assert result.returncode != 0


def test_fixed_runtime_entrypoint_refuses_unqualified_image_before_execution(tmp_path):
    # Arrange
    image = tmp_path / "unqualified.sif"
    image.write_bytes(b"unqualified image")
    env = {"PATH": os.defpath, "HOME": str(tmp_path), "SCITEX_CI_SIF": str(image), "SCITEX_CI_APPTAINER": "/usr/bin/false"}
    # Act
    result = subprocess.run(["bash", "-ec", '. "$1"; sac_release_runtime', "verify", str(LIBRARY)], env=env, capture_output=True, text=True, timeout=5)
    # Assert
    assert result.returncode != 0 and "qualified AA583 bytes" in result.stderr


def test_every_supported_release_minor_hard_requires_private_postgres():
    # Arrange
    path = ROOT / ".github/workflows/pypi-publish-and-github-release-on-tag.yml"
    workflow = yaml.safe_load(path.read_text())
    # Act
    job = workflow["jobs"]["test"]
    # Assert
    assert (job["strategy"]["matrix"]["python-version"], job["env"]["SAC_TEST_PG_REQUIRED"]) == (["3.11", "3.12", "3.13"], "1")


@pytest.mark.parametrize("job", ["test", "build"])
@pytest.mark.parametrize("requested,valid", [("v0.38.6", True), ("v0.38.6$(touch SENTINEL)", False)])
def test_release_version_is_literal_data_before_source_execution(tmp_path, job, requested, valid):
    # Arrange
    workflow = yaml.safe_load((ROOT / ".github/workflows/pypi-publish-and-github-release-on-tag.yml").read_text())
    step = next(row for row in workflow["jobs"][job]["steps"] if row.get("name") == "Resolve version")
    output = tmp_path / "output"
    env = {"PATH": os.defpath, "RELEASE_EVENT": "workflow_dispatch",
           "REQUESTED_VERSION": requested, "GITHUB_OUTPUT": str(output)}
    # Act
    result = subprocess.run(["bash", "-ec", step["run"]], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=5)
    # Assert
    assert ((result.returncode == 0), (tmp_path / "SENTINEL").exists()) == (valid, False)


def pg_selection(env):
    source = (ROOT / ".github/ci/exec-in-sif.sh").read_text()
    start = source.index('CI_PG_SIF=""')
    end = source.index('if [ -z "$CI_PG_SIF" ]; then', start)
    body = source[start:end] + '\nprintf "%s\\n" "$CI_PG_SIF"\n'
    return subprocess.run(["bash", "-ec", body], env=env,
                          capture_output=True, text=True, timeout=5)


def test_default_ci_retains_its_existing_pg_discovery_rule(tmp_path):
    # Arrange
    directory = tmp_path / ".scitex/pg"
    directory.mkdir(parents=True)
    (directory / "postgres-a.sif").write_bytes(b"ordinary discovery fixture a")
    selected = directory / "postgres-b.sif"
    selected.write_bytes(b"ordinary discovery fixture b")
    # Act
    result = pg_selection({"HOME": str(tmp_path), "PATH": os.defpath})
    # Assert
    assert (result.returncode, result.stdout.strip()) == (0, str(selected))


def test_invalid_release_pg_override_cannot_fall_back_to_ordinary_discovery(tmp_path):
    # Arrange
    directory = tmp_path / ".scitex/pg"
    directory.mkdir(parents=True)
    (directory / "postgres-fallback.sif").write_bytes(b"ordinary fallback fixture")
    invalid = tmp_path / "invalid-release.sif"
    invalid.write_bytes(b"unqualified release image")
    env = {"HOME": str(tmp_path), "PATH": os.defpath,
           "SAC_RELEASE_PG_SIF": str(invalid), "APPTAINER": "/usr/bin/true"}
    # Act
    result = pg_selection(env)
    # Assert
    assert result.returncode != 0
