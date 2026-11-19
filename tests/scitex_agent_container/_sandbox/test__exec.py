"""Plain-command sealed run (``sac sandbox exec``): validation and argv/env composition.

Pure pieces only (no apptainer): ``parse_bind``, ``build_request``,
``caller_env``, ``container_argv`` and ``plan``. The run itself is covered
live in ``tests/integration/test_sandbox_exec_live.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._sandbox._exec import (
    build_request,
    caller_env,
    container_argv,
    parse_bind,
    plan,
)
from scitex_agent_container._sandbox._layout import RunPaths

RUN_ROOT = "/srv/sac-exec-unit-runs"  # validated only; never created


def _request(tmp_path: Path, **kw):
    image = tmp_path / "img.sif"
    image.write_bytes(b"sif")
    args = {
        "apptainer": "/usr/bin/apptainer",
        "image": str(image),
        "argv": ["python3", "-c", "print(1+1)"],
        "run_root": RUN_ROOT,
        "timeout_s": 30,
    }
    args.update(kw)
    return build_request(**args)


def test_bind_defaults_to_read_only(tmp_path):
    # Arrange
    text = f"{tmp_path}:/data"
    # Act
    bind = parse_bind(text)
    # Assert
    assert bind == f"{tmp_path.resolve()}:/data:ro"


def test_bind_keeps_explicit_read_write(tmp_path):
    # Arrange
    text = f"{tmp_path}:/data:rw"
    # Act
    bind = parse_bind(text)
    # Assert
    assert bind.endswith(":/data:rw")


def test_bind_of_missing_source_is_refused(tmp_path):
    # Arrange
    text = f"{tmp_path}/absent:/data"
    # Act
    # Assert
    with pytest.raises(ValueError, match="existing absolute path"):
        parse_bind(text)


def test_bind_over_the_gateway_dir_is_refused(tmp_path):
    # Arrange
    text = f"{tmp_path}:/run/sac-sandbox/bin"
    # Act
    # Assert
    with pytest.raises(ValueError, match="reserved"):
        parse_bind(text)


def test_bind_over_the_run_home_is_refused(tmp_path):
    # Arrange
    text = f"{tmp_path}:/home/agent"
    # Act
    # Assert
    with pytest.raises(ValueError, match="reserved"):
        parse_bind(text)


def test_bind_with_relative_destination_is_refused(tmp_path):
    # Arrange
    text = f"{tmp_path}:data"
    # Act
    # Assert
    with pytest.raises(ValueError, match="DST must be absolute"):
        parse_bind(text)


def test_request_has_no_model_key(tmp_path):
    # Arrange
    kw: dict = {}
    # Act
    req = _request(tmp_path, **kw)
    # Assert
    assert req.spec.model_key is None


def test_request_egress_defaults_to_nothing(tmp_path):
    # Arrange
    kw: dict = {}
    # Act
    req = _request(tmp_path, **kw)
    # Assert
    assert req.spec.egress_allow == ()


def test_request_parses_egress_entries(tmp_path):
    # Arrange
    entries = ["PyPI.org:443"]
    # Act
    req = _request(tmp_path, egress_allow=entries)
    # Assert
    assert req.spec.egress_allow_strings == ["pypi.org:443"]


def test_request_refuses_a_tmp_run_root(tmp_path):
    # Arrange
    root = "/tmp/sac-runs"
    # Act
    # Assert
    with pytest.raises(ValueError, match="never tmp"):
        _request(tmp_path, run_root=root)


def test_request_refuses_an_empty_command(tmp_path):
    # Arrange
    argv: list[str] = []
    # Act
    # Assert
    with pytest.raises(ValueError, match="no command"):
        _request(tmp_path, argv=argv)


def test_request_refuses_a_zero_timeout(tmp_path):
    # Arrange
    timeout = 0
    # Act
    # Assert
    with pytest.raises(ValueError, match="--timeout"):
        _request(tmp_path, timeout_s=timeout)


def test_request_refuses_missing_apptainer(tmp_path):
    # Arrange
    apptainer = None
    # Act
    # Assert
    with pytest.raises(ValueError, match="apptainer is not on PATH"):
        _request(tmp_path, apptainer=apptainer)


def test_request_refuses_a_bad_env_name(tmp_path):
    # Arrange
    names = ["NOT-A-NAME"]
    # Act
    # Assert
    with pytest.raises(ValueError, match="--env-allow"):
        _request(tmp_path, env_allow=names)


def test_request_refuses_a_missing_image(tmp_path):
    # Arrange
    image = str(tmp_path / "absent.sif")
    # Act
    # Assert
    with pytest.raises(ValueError, match="--image"):
        _request(tmp_path, image=image)


def test_allowed_caller_variable_is_passed(tmp_path):
    # Arrange
    req = _request(tmp_path, env_allow=["PASS_ME"])
    # Act
    env, _ = caller_env(req, {"PASS_ME": "v", "SECRET_X": "s"})
    # Assert
    assert env["PASS_ME"] == "v"


def test_unlisted_caller_variable_is_not_passed(tmp_path):
    # Arrange
    req = _request(tmp_path, env_allow=["PASS_ME"])
    # Act
    env, _ = caller_env(req, {"PASS_ME": "v", "SECRET_X": "s"})
    # Assert
    assert "SECRET_X" not in env


def test_unlisted_caller_variable_is_recorded_as_dropped(tmp_path):
    # Arrange
    req = _request(tmp_path, env_allow=["PASS_ME"])
    # Act
    _, record = caller_env(req, {"PASS_ME": "v", "SECRET_X": "s"})
    # Assert
    assert record["dropped"] == ["SECRET_X"]


def test_unset_requested_variable_is_recorded_as_missing(tmp_path):
    # Arrange
    req = _request(tmp_path, env_allow=["NOT_SET"])
    # Act
    _, record = caller_env(req, {})
    # Assert
    assert record["missing"] == ["NOT_SET"]


def test_command_always_gets_a_utf8_locale(tmp_path):
    # Arrange
    req = _request(tmp_path)
    # Act
    env, _ = caller_env(req, {})
    # Assert
    assert env == {"LANG": "C.UTF-8"}


@pytest.mark.parametrize(
    "value", ["a\nB=b", "$HOME", "it's", 'say "x"', "a\\b", "`id`"]
)
def test_value_apptainer_would_rewrite_is_refused(tmp_path, value):
    # Arrange
    req = _request(tmp_path, env_allow=["PASS_ME"])
    # Act
    # Assert
    with pytest.raises(ValueError, match="apptainer would rewrite"):
        caller_env(req, {"PASS_ME": value})


def test_value_with_other_punctuation_is_passed(tmp_path):
    # Arrange
    value = "a b!*?~#%&()[]{}<>|;:,.=+-_@^/ end"
    req = _request(tmp_path, env_allow=["PASS_ME"])
    # Act
    env, _ = caller_env(req, {"PASS_ME": value})
    # Assert
    assert env["PASS_ME"] == value


def test_container_has_no_network_of_its_own(tmp_path):
    # Arrange
    req = _request(tmp_path)
    # Act
    argv = container_argv(req, RunPaths(tmp_path / "run"))
    # Assert
    assert argv[argv.index("--network") + 1] == "none"


def test_container_gets_the_requested_bind(tmp_path):
    # Arrange
    req = _request(tmp_path, binds=[f"{tmp_path}:/bundle"])
    # Act
    argv = container_argv(req, RunPaths(tmp_path / "run"))
    # Assert
    assert f"{tmp_path.resolve()}:/bundle:ro" in argv


def test_container_binds_only_the_run_and_requested_paths(tmp_path):
    # Arrange
    run = tmp_path / "run"
    req = _request(tmp_path, binds=[f"{tmp_path}:/bundle"])
    argv = container_argv(req, RunPaths(run))
    # Act
    sources = [argv[i + 1].split(":")[0] for i, a in enumerate(argv) if a == "--bind"]
    # Assert
    assert {s for s in sources if not s.startswith(str(run))} == {
        "/dev/net/tun",
        str(tmp_path.resolve()),
    }


def test_container_command_runs_behind_the_gateway(tmp_path):
    # Arrange
    req = _request(tmp_path)
    # Act
    argv = container_argv(req, RunPaths(tmp_path / "run"))
    # Assert
    assert argv[argv.index(req.image) + 1 :][-4:] == [
        "--",
        "python3",
        "-c",
        "print(1+1)",
    ]


def test_container_reads_env_only_from_the_run_file(tmp_path):
    # Arrange
    rp = RunPaths(tmp_path / "run")
    req = _request(tmp_path)
    # Act
    argv = container_argv(req, rp, rp.secrets / "env-00")
    # Assert
    assert [a for a in argv if a.startswith("--env")] == ["--env-file"]


def test_container_starts_in_the_requested_directory(tmp_path):
    # Arrange
    req = _request(tmp_path, pwd="/bundle")
    # Act
    argv = container_argv(req, RunPaths(tmp_path / "run"))
    # Assert
    assert argv[argv.index("--pwd") + 1] == "/bundle"


def test_plan_does_not_create_the_run_root(tmp_path):
    # Arrange
    req = _request(tmp_path)
    # Act
    plan(req)
    # Assert
    assert not Path(RUN_ROOT).exists()
