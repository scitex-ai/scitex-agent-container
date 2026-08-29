"""Tests for the P1 credential-exposure fix (:mod:`runtimes._apptainer_secret_env`).

Security property under test: a live secret VALUE (Anthropic/OpenAI API
key, ``sac listen`` bearer, telegram bot token, ...) must NEVER appear in
the ``apptainer exec`` argv sac builds — that argv becomes the command of
a tmux ``bash -c '<...>'`` pane and is exposed via the WORLD-READABLE
``/proc/<pid>/cmdline``. Secrets must instead travel through a ``0600``
``--env-file``, so the value still reaches the container (apptainer reads
the file at exec) but is not readable by other local users.

CI cannot verify this by running a container ("is /proc/<pid>/cmdline
free of the token?"), so it is pinned here at the argv-construction layer.

No mocks / no ``monkeypatch`` — real ``AgentConfig`` specs, real
``build_run_argv`` argv assembly, a real on-disk env-file with real
permission bits, and real ``os.environ`` set/restore in fixtures (the
production code reads the real environment).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._apptainer_runtime import (
    ApptainerContainerRuntime,
)
from scitex_agent_container.runtimes._apptainer_secret_env import (
    is_secret_env_key,
    redact_secret_env_to_file,
    secret_env_file_path,
)

# Fake sentinels — NEVER real credentials. Unique strings we can grep the
# built argv for; deliberately NOT ``sk-ant``-shaped to keep intent clear.
_ANTHROPIC_SENTINEL = "ZZZ-sentinel-anthropic-2f8e-must-not-appear-in-argv"
_BEARER_SENTINEL = "ZZZ-sentinel-listen-bearer-7c1d-must-not-appear-in-argv"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---------------------------------------------------------------------------
# is_secret_env_key predicate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "key",
    [
        "ANTHROPIC_API_KEY",
        "SAC_ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "SAC_OPENAI_API_KEY",
        "SAC_LISTEN_BEARER",
        "CCT_BOT_TOKEN",
        "DEEPSEEK_API_KEY",
        "MY_PASSWORD",
        "SOME_SECRET",
        "AWS_ACCESS_KEY",
    ],
)
def test_secret_keys_detected(key: str) -> None:
    # Arrange
    subject = key
    # Act
    detected = is_secret_env_key(subject)
    # Assert
    assert detected is True


@pytest.mark.parametrize(
    "key",
    [
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CONFIG_DIR",
        "SAC_PROVIDER",
        "SCITEX_AGENT_CONTAINER_YAML_DIRS",
        "SAC_NAME",
        "SAC_LISTEN_BASE_URL",
        "DIRENV_CONFIG",
        "UV_PROJECT_ENVIRONMENT",
        "ANTHROPIC_MODEL",
        "SCITEX_AGENT_CONTAINER_STATE_DB",
    ],
)
def test_non_secret_keys_not_detected(key: str) -> None:
    # Arrange
    subject = key
    # Act
    detected = is_secret_env_key(subject)
    # Assert
    assert detected is False


# ---------------------------------------------------------------------------
# redact_secret_env_to_file — pure sweep (single secret + a non-secret)
# ---------------------------------------------------------------------------
@pytest.fixture
def single_sweep(tmp_path: Path) -> SimpleNamespace:
    """Sweep one secret (+ one non-secret) --env; return argv/out/file."""
    argv = [
        "apptainer", "exec",
        "--env", f"SAC_LISTEN_BEARER={_BEARER_SENTINEL}",
        "--env", "SAC_NAME=x",
        "img.sif", "cmd",
    ]
    out = redact_secret_env_to_file(list(argv), state_dir=tmp_path)
    return SimpleNamespace(
        argv_in=argv,
        out=out,
        joined=" ".join(out),
        ef=secret_env_file_path(tmp_path),
    )


def test_secret_value_absent_from_argv(single_sweep: SimpleNamespace) -> None:
    # Arrange
    joined = single_sweep.joined
    # Act
    leaked = _BEARER_SENTINEL in joined
    # Assert
    assert leaked is False


def test_secret_key_transport_absent_from_argv(
    single_sweep: SimpleNamespace,
) -> None:
    # Arrange
    joined = single_sweep.joined
    # Act
    leaked = "SAC_LISTEN_BEARER" in joined
    # Assert
    assert leaked is False


def test_non_secret_env_flag_preserved(single_sweep: SimpleNamespace) -> None:
    # Arrange
    out = single_sweep.out
    # Act
    present = "SAC_NAME=x" in out
    # Assert
    assert present is True


def test_exactly_one_env_file_appended(single_sweep: SimpleNamespace) -> None:
    # Arrange
    out = single_sweep.out
    # Act
    count = out.count("--env-file")
    # Assert
    assert count == 1


def test_env_file_flag_precedes_path(single_sweep: SimpleNamespace) -> None:
    # Arrange
    out, ef = single_sweep.out, str(single_sweep.ef)
    # Act
    flag_before = out[out.index(ef) - 1] if ef in out else None
    # Assert
    assert flag_before == "--env-file"


def test_env_file_is_owner_only(single_sweep: SimpleNamespace) -> None:
    # Arrange
    ef = single_sweep.ef
    # Act
    mode = _mode(ef)
    # Assert
    assert mode == 0o600


def test_env_file_delivers_secret_to_container(
    single_sweep: SimpleNamespace,
) -> None:
    # Arrange
    ef = single_sweep.ef
    # Act
    content = ef.read_text()
    # Assert
    assert f"SAC_LISTEN_BEARER={_BEARER_SENTINEL}" in content


def test_secret_dir_is_owner_only(single_sweep: SimpleNamespace) -> None:
    # Arrange
    secrets_dir = single_sweep.ef.parent
    # Act
    mode = _mode(secrets_dir)
    # Assert
    assert mode == 0o700


def test_input_argv_not_mutated(single_sweep: SimpleNamespace) -> None:
    # Arrange
    expected = [
        "apptainer", "exec",
        "--env", f"SAC_LISTEN_BEARER={_BEARER_SENTINEL}",
        "--env", "SAC_NAME=x",
        "img.sif", "cmd",
    ]
    # Act
    unchanged = single_sweep.argv_in == expected
    # Assert
    assert unchanged is True


# ---------------------------------------------------------------------------
# No secret present → argv untouched, no file created
# ---------------------------------------------------------------------------
@pytest.fixture
def no_secret_sweep(tmp_path: Path) -> SimpleNamespace:
    argv = ["apptainer", "exec", "--env", "SAC_NAME=x", "img.sif", "cmd"]
    out = redact_secret_env_to_file(list(argv), state_dir=tmp_path)
    return SimpleNamespace(argv_in=argv, out=out, tmp_path=tmp_path)


def test_no_secret_leaves_argv_equal(no_secret_sweep: SimpleNamespace) -> None:
    # Arrange
    res = no_secret_sweep
    # Act
    equal = res.out == res.argv_in
    # Assert
    assert equal is True


def test_no_secret_creates_no_file(no_secret_sweep: SimpleNamespace) -> None:
    # Arrange
    path = secret_env_file_path(no_secret_sweep.tmp_path)
    # Act
    exists = path.exists()
    # Assert
    assert exists is False


# ---------------------------------------------------------------------------
# Multiple secrets from mixed sources → all swept
# ---------------------------------------------------------------------------
@pytest.fixture
def multi_sweep(tmp_path: Path) -> SimpleNamespace:
    argv = [
        "apptainer", "exec",
        "--env", f"SAC_ANTHROPIC_API_KEY={_ANTHROPIC_SENTINEL}",
        "--env", f"SAC_LISTEN_BEARER={_BEARER_SENTINEL}",
        "--env", "CCT_BOT_TOKEN=123:abc",
        "img.sif", "cmd",
    ]
    out = redact_secret_env_to_file(list(argv), state_dir=tmp_path)
    return SimpleNamespace(
        joined=" ".join(out), content=secret_env_file_path(tmp_path).read_text()
    )


def test_no_multi_secret_value_in_argv(multi_sweep: SimpleNamespace) -> None:
    # Arrange
    values = [_ANTHROPIC_SENTINEL, _BEARER_SENTINEL, "123:abc"]
    # Act
    leaked = any(v in multi_sweep.joined for v in values)
    # Assert
    assert leaked is False


def test_all_multi_secrets_in_env_file(multi_sweep: SimpleNamespace) -> None:
    # Arrange
    lines = [
        f"SAC_ANTHROPIC_API_KEY={_ANTHROPIC_SENTINEL}",
        f"SAC_LISTEN_BEARER={_BEARER_SENTINEL}",
        "CCT_BOT_TOKEN=123:abc",
    ]
    # Act
    all_present = all(line in multi_sweep.content for line in lines)
    # Assert
    assert all_present is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------
def test_duplicate_secret_key_keeps_last_value(tmp_path: Path) -> None:
    # Arrange
    argv = [
        "apptainer", "exec",
        "--env", "SAC_ANTHROPIC_API_KEY=first",
        "--env", "SAC_ANTHROPIC_API_KEY=second",
        "img.sif",
    ]
    # Act
    redact_secret_env_to_file(argv, state_dir=tmp_path)
    # Assert — only one line, carrying the LAST value (apptainer --env
    # last-wins), so "first" is gone entirely.
    assert secret_env_file_path(tmp_path).read_text() == "SAC_ANTHROPIC_API_KEY=second\n"


def test_value_containing_equals_round_trips(tmp_path: Path) -> None:
    # Arrange
    argv = ["apptainer", "exec", "--env", "X_TOKEN=a=b=c", "img.sif"]
    # Act
    redact_secret_env_to_file(argv, state_dir=tmp_path)
    # Assert
    assert "X_TOKEN=a=b=c" in secret_env_file_path(tmp_path).read_text()


# ---------------------------------------------------------------------------
# A pre-existing loose-perm file is re-hardened (relocating a secret to a
# 0644 file would just MOVE the leak).
# ---------------------------------------------------------------------------
@pytest.fixture
def reharden_sweep(tmp_path: Path) -> Path:
    path = secret_env_file_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("STALE=1\n")
    os.chmod(path, 0o644)
    argv = [
        "apptainer", "exec",
        "--env", f"SAC_LISTEN_BEARER={_BEARER_SENTINEL}",
        "img.sif",
    ]
    redact_secret_env_to_file(argv, state_dir=tmp_path)
    return path


def test_preexisting_loose_file_forced_to_0600(reharden_sweep: Path) -> None:
    # Arrange
    path = reharden_sweep
    # Act
    mode = _mode(path)
    # Assert
    assert mode == 0o600


def test_preexisting_loose_file_rewritten_fresh(reharden_sweep: Path) -> None:
    # Arrange
    path = reharden_sweep
    # Act
    stale_present = "STALE" in path.read_text()
    # Assert
    assert stale_present is False


# ---------------------------------------------------------------------------
# Integration through the REAL build_run_argv (no mocks)
# ---------------------------------------------------------------------------
@pytest.fixture
def built_argv(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """Build a real launch argv with a fake Anthropic key in the host env.

    Sets the env the way ``_apptainer_auth.auth_argv`` reads it, builds
    the argv through the real runtime, and restores the env on teardown.
    """
    keys = ("SAC_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ[k] = _ANTHROPIC_SENTINEL
    try:
        cfg = AgentConfig(
            name="sec-x", runtime="apptainer", workdir=str(tmp_path / "wd")
        )
        state_dir = tmp_path / "state"
        argv = ApptainerContainerRuntime().build_run_argv(
            cfg, state_dir=state_dir, sif_path=tmp_path / "x.sif"
        )
        yield SimpleNamespace(
            argv=argv,
            joined=" ".join(argv),
            ef=secret_env_file_path(state_dir),
        )
    finally:
        for k, original in saved.items():
            if original is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = original


def test_build_argv_has_no_secret_value(built_argv: SimpleNamespace) -> None:
    # Arrange
    joined = built_argv.joined
    # Act — KEY regression: the live token value in the launcher argv.
    leaked = _ANTHROPIC_SENTINEL in joined
    # Assert
    assert leaked is False


def test_build_argv_has_no_secret_key_transport(
    built_argv: SimpleNamespace,
) -> None:
    # Arrange
    joined = built_argv.joined
    # Act — the bare `--env ...ANTHROPIC_API_KEY=` transport is gone too
    # (covers both ANTHROPIC_API_KEY and SAC_ANTHROPIC_API_KEY).
    leaked = "ANTHROPIC_API_KEY=" in joined
    # Assert
    assert leaked is False


def test_build_argv_points_env_file_at_secret_file(
    built_argv: SimpleNamespace,
) -> None:
    # Arrange
    argv, ef = built_argv.argv, str(built_argv.ef)
    # Act
    flag_before = argv[argv.index(ef) - 1] if ef in argv else None
    # Assert
    assert flag_before == "--env-file"


def test_build_argv_secret_file_is_0600(built_argv: SimpleNamespace) -> None:
    # Arrange
    ef = built_argv.ef
    # Act
    mode = _mode(ef)
    # Assert
    assert mode == 0o600


def test_build_argv_container_still_receives_keys(
    built_argv: SimpleNamespace,
) -> None:
    # Arrange
    ef = built_argv.ef
    # Act — apptainer reads this file at exec, so delivery is preserved.
    content = ef.read_text()
    # Assert
    assert content.count(_ANTHROPIC_SENTINEL) == 2


def test_build_argv_keeps_non_secret_env_flag(
    built_argv: SimpleNamespace,
) -> None:
    # Arrange
    argv = built_argv.argv
    # Act — a curated non-secret --env must NOT be swept (no over-reach).
    present = "SCITEX_AGENT_CONTAINER_STATE_DB=/state/state.db" in argv
    # Assert
    assert present is True


# ---------------------------------------------------------------------------
# The GLUED ``--env=KEY=VALUE`` spelling is swept too
#
# THE HOLE THIS PINS. The sweep recognised only the SPLIT ``["--env", "K=V"]``
# form while ``_apptainer_env_dedup`` also knew the GLUED ``--env=K=V``, so a
# spec using the glued spelling — LIVE across this fleet's ``raw_args`` — put
# its secret straight into the world-readable launcher argv while every test
# here still passed. Measured: 1 exposed pid before, 0 after. Both modules now
# share ``_apptainer_env_dedup.env_pair_at`` (pinned in that module's mirror).
# ---------------------------------------------------------------------------
_GLUED_SENTINEL = "ZZZ-sentinel-glued-9c1d-must-not-appear-in-argv"


@pytest.fixture
def glued_sweep(tmp_path: Path) -> SimpleNamespace:
    """Sweep a GLUED secret --env= (plus a glued non-secret)."""
    argv = [
        "apptainer", "exec",
        f"--env=SAC_ANTHROPIC_API_KEY={_GLUED_SENTINEL}",
        "--env=SAC_NAME=x",
        "img.sif", "cmd",
    ]
    out = redact_secret_env_to_file(list(argv), state_dir=tmp_path)
    return SimpleNamespace(
        out=out,
        joined=" ".join(out),
        ef=secret_env_file_path(tmp_path),
    )


def test_glued_secret_value_absent_from_argv(glued_sweep: SimpleNamespace) -> None:
    # Arrange
    joined = glued_sweep.joined
    # Act
    leaked = _GLUED_SENTINEL in joined
    # Assert
    assert leaked is False


def test_glued_secret_flag_removed_from_argv(glued_sweep: SimpleNamespace) -> None:
    # Arrange
    out = glued_sweep.out
    # Act — the whole single token goes, not just its value.
    remaining = [tok for tok in out if tok.startswith("--env=SAC_ANTHROPIC_API_KEY")]
    # Assert
    assert remaining == []


def test_glued_secret_value_reaches_the_env_file(
    glued_sweep: SimpleNamespace,
) -> None:
    # Arrange
    ef = glued_sweep.ef
    # Act — delivery preserved: apptainer reads this file at exec.
    content = ef.read_text()
    # Assert
    assert f"SAC_ANTHROPIC_API_KEY={_GLUED_SENTINEL}" in content


# (The 0600 mode of the env-file is already pinned for the split sweep above;
# both spellings land in the SAME file, so it is one property, not two.)


def test_glued_non_secret_env_flag_is_kept(glued_sweep: SimpleNamespace) -> None:
    # Arrange
    out = glued_sweep.out
    # Act — no over-reach: a glued NON-secret keeps its place and spelling.
    present = "--env=SAC_NAME=x" in out
    # Assert
    assert present is True
