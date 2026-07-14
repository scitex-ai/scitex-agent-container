"""Regression tests — the stray ``--fakeroot`` file in the project root.

Operator bug (2026-06-25): a 1-byte (single NULL) file literally named
``--fakeroot`` kept appearing in the PROJECT ROOT. Root cause: when a
value-taking apptainer flag (``--overlay`` / ``--bind`` / ``--env-file``
/ …) is emitted with NO value — e.g. operator ``raw_args: ["--overlay"]``
directly before sac's own ``--fakeroot`` — apptainer's CLI parser
swallows the NEXT token (``--fakeroot``) as the missing value and creates
a relative stub file at it. The TUI runtime shell-runs the argv with
``cwd = expanded_workdir`` (the project root for the maintainer agent),
so the stub lands in the repo.

Fix: ``_apptainer_argv_guard.validate_flag_argv`` fails loud (no
band-aid) when the assembled apptainer flag region has a value-taking
flag missing its value, BEFORE the argv is ever launched. Wired into
``build_run_argv`` just before the SIF is appended.

No mocks / no monkeypatch (PA-306/307). Real ``load_config`` on a tmp
spec for the end-to-end cases; direct argv lists for the unit cases.
AAA blocks, markers on their own line, one assert per test.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._apptainer_argv_guard import (
    ApptainerArgvError,
    validate_flag_argv,
)
from scitex_agent_container.runtimes._apptainer_build_argv import build_run_argv

# ---------------------------------------------------------------------------
# Fixtures (mirror test__apptainer_build_argv.py — real HOME + bearer token)
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolate_home(tmp_path: Path) -> Iterator[Path]:
    # Arrange-side fixture: keep the bearer-token resolver off the real ~.
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


@pytest.fixture
def listen_bearer_token(_isolate_home: Path) -> Path:
    # Materialise a real listen bearer so build_run_argv's listen-env
    # guard doesn't turn an argv-shape test into a RuntimeError.
    token_dir = _isolate_home / ".scitex" / "agent-container" / "tokens"
    token_dir.mkdir(parents=True, exist_ok=True)
    token_path = token_dir / f"listen-{socket.gethostname()}.token"
    token_path.write_text("test-bearer-token-not-a-secret\n", encoding="utf-8")
    return token_path


def _write_spec(tmp_path: Path, raw_args_yaml: str) -> Path:
    spec_dir = tmp_path / "agents" / "agt"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / "spec.yaml"
    spec.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "metadata:\n"
        "  labels:\n"
        "    project: t\n"
        '    sac-builtin: "off"\n'
        "spec:\n"
        "  runtime: tui\n"
        "  host: ${HOSTNAME}\n"
        "  workdir: /tmp/agt-work\n"
        "  apptainer:\n"
        "    image: /x.sif\n"
        "    fakeroot: true\n"
        "    binds: []\n"
        f"{raw_args_yaml}"
        "  health:\n"
        "    enabled: true\n"
        "    interval: 60\n"
        "  restart:\n"
        "    policy: on-failure\n"
        "    max_retries: 3\n"
        "  claude:\n"
        "    model: claude-opus-4-8[1m]\n",
        encoding="utf-8",
    )
    return spec


# ---------------------------------------------------------------------------
# validate_flag_argv — unit cases (direct argv lists)
# ---------------------------------------------------------------------------


def test_valid_fakeroot_argv_passes_without_raising() -> None:
    # Arrange — a well-formed flag region: --fakeroot sits before the SIF.
    argv = ["apptainer", "exec", "--containall", "--fakeroot", "/x.sif", "bash"]
    # Act
    result = validate_flag_argv(argv)
    # Assert — a no-op returns None (did not raise).
    assert result is None


def test_overlay_missing_value_swallowing_fakeroot_raises() -> None:
    # Arrange — operator --overlay with no value, sac --fakeroot next.
    argv = ["apptainer", "exec", "--overlay", "--fakeroot", "/x.sif", "bash"]
    # Act
    run = lambda: validate_flag_argv(argv)
    # Assert
    with pytest.raises(ApptainerArgvError):
        run()


def test_bind_missing_value_swallowing_next_flag_raises() -> None:
    # Arrange — --bind with no value, another flag (--cleanenv) next.
    argv = ["apptainer", "exec", "--bind", "--cleanenv", "/x.sif", "bash"]
    # Act
    run = lambda: validate_flag_argv(argv)
    # Assert
    with pytest.raises(ApptainerArgvError):
        run()


def test_error_message_names_the_offending_flag() -> None:
    # Arrange
    argv = ["apptainer", "exec", "--overlay", "--fakeroot", "/x.sif"]
    message = ""
    try:
        validate_flag_argv(argv)
    except ApptainerArgvError as exc:
        message = str(exc)
    # Act
    names_cause = "--overlay" in message
    # Assert — the diagnostic points at the real cause (--overlay).
    assert names_cause is True


def test_valid_bind_with_value_before_fakeroot_passes() -> None:
    # Arrange — --bind has its value; --fakeroot is well-positioned.
    argv = ["apptainer", "exec", "--bind", "/a:/a", "--fakeroot", "/x.sif"]
    # Act
    result = validate_flag_argv(argv)
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# build_run_argv — end-to-end (real spec → guard runs before launch)
# ---------------------------------------------------------------------------


def test_build_run_argv_well_positioned_fakeroot_does_not_raise(
    tmp_path: Path, listen_bearer_token: Path
) -> None:
    # Arrange — operator raw_args puts --fakeroot in a VALID position
    # (a value-flag --bind with its value comes first).
    raw = "    raw_args:\n      - --bind\n      - /a:/a\n      - --fakeroot\n"
    cfg = load_config(str(_write_spec(tmp_path, raw)))
    # Act
    argv = build_run_argv(
        cfg, state_dir=tmp_path / "st", sif_path=tmp_path / "x.sif", tui=True
    )
    # Assert — the guard passed; --fakeroot made it into the argv.
    assert "--fakeroot" in argv


def test_build_run_argv_well_positioned_fakeroot_precedes_a_value(
    tmp_path: Path, listen_bearer_token: Path
) -> None:
    # Arrange — same valid raw_args; --bind /a:/a satisfies its value so
    # --fakeroot is never swallowed.
    raw = "    raw_args:\n      - --bind\n      - /a:/a\n      - --fakeroot\n"
    cfg = load_config(str(_write_spec(tmp_path, raw)))
    argv = build_run_argv(
        cfg, state_dir=tmp_path / "st", sif_path=tmp_path / "x.sif", tui=True
    )
    idx = argv.index("--bind")
    # Act — the token AFTER the first --bind is its value, not --fakeroot.
    following = argv[idx + 1]
    # Assert
    assert not following.startswith("--")


def test_build_run_argv_overlay_no_value_before_fakeroot_raises(
    tmp_path: Path, listen_bearer_token: Path
) -> None:
    # Arrange — operator raw_args declares a bare --overlay (no value);
    # sac then appends its own --fakeroot right after, reproducing the bug.
    raw = "    raw_args:\n      - --overlay\n"
    cfg = load_config(str(_write_spec(tmp_path, raw)))
    # Act
    build = lambda: build_run_argv(
        cfg, state_dir=tmp_path / "st", sif_path=tmp_path / "x.sif", tui=True
    )
    # Assert — refused loudly BEFORE any launch / file creation.
    with pytest.raises(ApptainerArgvError):
        build()


def test_build_run_argv_curated_fakeroot_from_yaml_is_emitted(
    tmp_path: Path, listen_bearer_token: Path
) -> None:
    # Arrange — spec sets apptainer.fakeroot: true (parsed now); no
    # raw_args. The curated iso-prepend must emit --fakeroot.
    cfg = load_config(str(_write_spec(tmp_path, "")))
    # Act
    argv = build_run_argv(
        cfg, state_dir=tmp_path / "st", sif_path=tmp_path / "x.sif", tui=True
    )
    # Assert — the YAML fakeroot key reached the launch argv.
    assert "--fakeroot" in argv


def test_build_run_argv_no_stray_fakeroot_file_in_cwd(
    tmp_path: Path, listen_bearer_token: Path, clean_cwd
) -> None:
    # Arrange — build a valid fakeroot argv while cwd is a clean dir; the
    # guard + well-formed argv must not (and cannot, being pure) create a
    # stray ``--fakeroot`` file in cwd.
    cfg = load_config(str(_write_spec(tmp_path, "")))
    build_run_argv(
        cfg, state_dir=tmp_path / "st", sif_path=tmp_path / "x.sif", tui=True
    )
    # Act
    stray = clean_cwd / "--fakeroot"
    # Assert
    assert not stray.exists()


@pytest.fixture
def clean_cwd(tmp_path: Path) -> Iterator[Path]:
    """chdir into a clean dir for the duration of the test, no monkeypatch.

    A real ``os.chdir`` swap (saved + restored in a yield-fixture) so the
    no-stray-file assertion runs with cwd pointed at an empty directory —
    proving the argv assembly never writes ``--fakeroot`` relative to cwd.
    """
    clean = tmp_path / "clean-cwd"
    clean.mkdir()
    saved = os.getcwd()
    os.chdir(clean)
    try:
        yield clean
    finally:
        os.chdir(saved)
