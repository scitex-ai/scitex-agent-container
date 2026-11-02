"""Real child coverage survives local fixture IO and atomic shard publication."""

import os
from pathlib import Path
import subprocess
import sys

from coverage import Coverage, CoverageData
from coverage.exceptions import DataError
import pytest

from tests.scitex_agent_container._runners._a2a_proxy_workspace import (
    coverage_environment,
    publish_coverage,
)


@pytest.fixture(params=("file", "serialized"))
def child_coverage_case(tmp_path, request):
    repo = Path(__file__).resolve().parents[3]
    local = tmp_path / "local"
    local.mkdir()
    original = tmp_path / "collector" / ".coverage"
    environment = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(repo / "src"),
        "COVERAGE_PROCESS_START": str(repo / "pyproject.toml"),
        "COVERAGE_FILE": str(original),
    }
    if request.param == "serialized":
        config = Coverage(config_file=repo / "pyproject.toml", data_file=original)
        environment["COVERAGE_PROCESS_CONFIG"] = config.config.serialize()
    child_env, target = coverage_environment(environment, local)
    return local, original, child_env, target


def test_real_child_coverage_is_published_with_proxy_lines_and_branch_data(
    child_coverage_case,
):
    # Arrange — execute real argument parsing, not manufactured coverage rows.
    local, original, child_env, target = child_coverage_case
    code = (
        "from scitex_agent_container._runners.a2a_proxy import _parse_argv; "
        "_parse_argv(['--name','owned-coverage-proof','--upstream','http://127.0.0.1:1'])"
    )
    # Act
    subprocess.run([sys.executable, "-c", code], env=child_env, check=True, timeout=5)
    publish_coverage(local, target)
    proxy_lines = []
    branches = []
    for shard in original.parent.iterdir():
        data = CoverageData(basename=str(shard))
        data.read()
        proxy_lines.extend(
            data.lines(name) or []
            for name in data.measured_files()
            if name.endswith("/_runners/a2a_proxy.py")
        )
        branches.append(data.has_arcs())
    # Assert
    assert (any(proxy_lines), all(branches)) == (True, True)


def test_serialized_parent_options_change_only_the_coverage_destination(tmp_path):
    # Arrange — serialized config overrides a conflicting FILE variable in coverage.
    local = tmp_path / "local"
    local.mkdir()
    parent = Coverage(
        config_file=False,
        data_file=tmp_path / "original" / ".coverage",
        branch=True,
        source=["scitex_agent_container"],
        data_suffix=True,
    )
    parent.set_option("run:dynamic_context", "test_function")
    serialized = parent.config.serialize()
    before = Coverage(config_file=":data:" + serialized)
    environment = {
        "COVERAGE_PROCESS_CONFIG": serialized,
        "COVERAGE_FILE": str(tmp_path / "ignored-by-serialized-config"),
    }
    # Act
    child_env, target = coverage_environment(environment, local)
    after = Coverage(config_file=":data:" + child_env["COVERAGE_PROCESS_CONFIG"])
    before_options = {
        k: v for k, v in vars(before.config).items() if not k.startswith("_")
    }
    after_options = {
        k: v for k, v in vars(after.config).items() if not k.startswith("_")
    }
    after_options["data_file"] = before_options["data_file"]
    # Assert
    assert (target, after_options) == (
        Path(before.get_option("run:data_file")),
        before_options,
    )


def test_corrupt_coverage_shard_refuses_publication(tmp_path):
    # Arrange
    local = tmp_path / "local"
    (local / "coverage").mkdir(parents=True)
    (local / "coverage" / ".coverage.corrupt").write_bytes(
        b"not a SQLite coverage file"
    )
    original = tmp_path / "collector" / ".coverage"

    # Act
    def publish():
        return publish_coverage(local, original)

    # Assert
    with pytest.raises(DataError):
        publish()
