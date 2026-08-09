"""Regression tests — a credential bind that mounts nothing, silently.

Operator incident (2026-08-09, scitex-compute-04): every agent spec bound
``/home/ywatanabe/.config/gh`` into the container read-only. The directory
existed but held ONLY ``config.yml`` — ``hosts.yml``, which carries the
``oauth_token``, was absent. The bind SUCCEEDED, so inside the container
``gh`` reported "not logged in", indistinguishable from "never granted a
token". All 12 agents on the host believed no GitHub token existed; one
told the operator it could not merge its own PR. Every pre-existing check
passed, because they all ask ``Path(src).exists()`` — and the source DID
exist. Source-exists is not capability-delivered.

Fix: ``_apptainer_bind_guard.validate_capability_binds``, wired into
``build_run_argv`` where the spec binds are known. A bind whose
destination is in the named ``CAPABILITY_BINDS`` set REFUSES the start
when its proof file is missing; every other absent bind source gets a
``logging.error`` and the start continues (a host-specific data mount
must not ground the fleet).

No mocks / no monkeypatch (PA-306/307). Real ``load_config`` on a tmp
spec; real directories under ``tmp_path``. AAA blocks, markers on their
own line, one assert per test.
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._apptainer_bind_guard import (
    BindCapabilityError,
    spec_binds_checked,
    validate_capability_binds,
)
from scitex_agent_container.runtimes._apptainer_build_argv import build_run_argv
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

# ---------------------------------------------------------------------------
# Fixtures (mirror test__apptainer_argv_guard.py — real HOME + bearer token)
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
    # guard doesn't turn a bind test into a RuntimeError.
    token_dir = _isolate_home / ".scitex" / "agent-container" / "tokens"
    token_dir.mkdir(parents=True, exist_ok=True)
    token_path = token_dir / f"listen-{socket.gethostname()}.token"
    token_path.write_text("test-bearer-token-not-a-secret\n", encoding="utf-8")
    return token_path


def _write_spec(tmp_path: Path, binds_yaml: str) -> Path:
    spec_dir = tmp_path / "agents" / "agt"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / "spec.yaml"
    spec.write_text(
        explicitize_yaml(
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
            f"{binds_yaml}"
            "  health:\n"
            "    enabled: true\n"
            "    interval: 60\n"
            "  restart:\n"
            "    policy: on-failure\n"
            "    max_retries: 3\n"
            "  claude:\n"
            "    model: claude-opus-4-8[1m]\n"
        ),
        encoding="utf-8",
    )
    return spec


def _gh_bind_yaml(source: Path) -> str:
    return f"    binds:\n      - {source}:/home/agent/.config/gh:ro\n"


def _gh_dir_without_hosts_yml(tmp_path: Path) -> Path:
    """Reproduce the incident on disk: config.yml present, hosts.yml absent."""
    gh_dir = tmp_path / "ghconf"
    gh_dir.mkdir()
    (gh_dir / "config.yml").write_text("version: 1\n", encoding="utf-8")
    return gh_dir


def _gh_dir_with_hosts_yml(tmp_path: Path) -> Path:
    gh_dir = _gh_dir_without_hosts_yml(tmp_path)
    (gh_dir / "hosts.yml").write_text(
        "github.com:\n    oauth_token: not-a-real-token\n", encoding="utf-8"
    )
    return gh_dir


# ---------------------------------------------------------------------------
# build_run_argv — end-to-end (real spec → guard runs before any launch)
# ---------------------------------------------------------------------------


def test_missing_gh_source_refuses_the_start(
    tmp_path: Path, listen_bearer_token: Path
) -> None:
    # Arrange — the spec declares a gh bind whose host source does not exist.
    absent = tmp_path / "no-such-gh-dir"
    cfg = load_config(str(_write_spec(tmp_path, _gh_bind_yaml(absent))))
    # Act
    build = lambda: build_run_argv(
        cfg, state_dir=tmp_path / "st", sif_path=tmp_path / "x.sif", tui=True
    )
    # Assert — refused loudly BEFORE any container is launched.
    with pytest.raises(BindCapabilityError):
        build()


def test_gh_source_without_hosts_yml_refuses_the_start(
    tmp_path: Path, listen_bearer_token: Path
) -> None:
    # Arrange — the exact incident: the directory exists, holds config.yml,
    # and carries no hosts.yml (so no oauth_token).
    gh_dir = _gh_dir_without_hosts_yml(tmp_path)
    cfg = load_config(str(_write_spec(tmp_path, _gh_bind_yaml(gh_dir))))
    # Act
    build = lambda: build_run_argv(
        cfg, state_dir=tmp_path / "st", sif_path=tmp_path / "x.sif", tui=True
    )
    # Assert
    with pytest.raises(BindCapabilityError):
        build()


def test_correct_gh_source_reaches_the_argv(
    tmp_path: Path, listen_bearer_token: Path
) -> None:
    # Arrange — hosts.yml present: the capability really is delivered.
    gh_dir = _gh_dir_with_hosts_yml(tmp_path)
    cfg = load_config(str(_write_spec(tmp_path, _gh_bind_yaml(gh_dir))))
    # Act
    argv = build_run_argv(
        cfg, state_dir=tmp_path / "st", sif_path=tmp_path / "x.sif", tui=True
    )
    # Assert — the guard passed and the bind was emitted verbatim.
    assert f"{gh_dir}:/home/agent/.config/gh:ro" in argv


def test_optional_bind_missing_source_still_builds(
    tmp_path: Path, listen_bearer_token: Path
) -> None:
    # Arrange — a host-specific data mount that is absent on this host.
    absent = tmp_path / "no-such-data-dir"
    binds = f"    binds:\n      - {absent}:/data:rw\n"
    cfg = load_config(str(_write_spec(tmp_path, binds)))
    # Act — must NOT refuse: grounding a fleet on an optional mount would
    # be worse than the bug this guard exists to fix.
    argv = build_run_argv(
        cfg, state_dir=tmp_path / "st", sif_path=tmp_path / "x.sif", tui=True
    )
    # Assert
    assert f"{absent}:/data:rw" in argv


# ---------------------------------------------------------------------------
# validate_capability_binds — the message the operator has to read
# ---------------------------------------------------------------------------


def test_error_message_names_the_spec_file(tmp_path: Path) -> None:
    # Arrange
    gh_dir = _gh_dir_without_hosts_yml(tmp_path)
    spec = _write_spec(tmp_path, _gh_bind_yaml(gh_dir))
    cfg = load_config(str(spec))
    message = ""
    try:
        validate_capability_binds(cfg, list(cfg.apptainer.binds))
    except BindCapabilityError as exc:
        message = str(exc)
    # Act
    names_spec = str(spec) in message
    # Assert — the operator knows which file to open without asking.
    assert names_spec is True


def test_error_message_names_the_host_path(tmp_path: Path) -> None:
    # Arrange
    gh_dir = _gh_dir_without_hosts_yml(tmp_path)
    cfg = load_config(str(_write_spec(tmp_path, _gh_bind_yaml(gh_dir))))
    message = ""
    try:
        validate_capability_binds(cfg, list(cfg.apptainer.binds))
    except BindCapabilityError as exc:
        message = str(exc)
    # Act
    names_path = str(gh_dir / "hosts.yml") in message
    # Assert — the operator knows which disk path to check.
    assert names_path is True


def test_error_message_names_the_remedy(tmp_path: Path) -> None:
    # Arrange
    gh_dir = _gh_dir_without_hosts_yml(tmp_path)
    cfg = load_config(str(_write_spec(tmp_path, _gh_bind_yaml(gh_dir))))
    message = ""
    try:
        validate_capability_binds(cfg, list(cfg.apptainer.binds))
    except BindCapabilityError as exc:
        message = str(exc)
    # Act
    names_remedy = "gh auth login" in message
    # Assert
    assert names_remedy is True


def test_missing_optional_bind_logs_an_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    absent = tmp_path / "no-such-data-dir"
    binds = f"    binds:\n      - {absent}:/data:rw\n"
    cfg = load_config(str(_write_spec(tmp_path, binds)))
    # Act
    with caplog.at_level(logging.ERROR):
        validate_capability_binds(cfg, list(cfg.apptainer.binds))
    # Assert — loud, even though it did not refuse.
    assert str(absent) in caplog.text


def test_present_optional_bind_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    present = tmp_path / "data-dir"
    present.mkdir()
    binds = f"    binds:\n      - {present}:/data:rw\n"
    cfg = load_config(str(_write_spec(tmp_path, binds)))
    # Act
    with caplog.at_level(logging.ERROR):
        validate_capability_binds(cfg, list(cfg.apptainer.binds))
    # Assert — no noise for a bind that is fine.
    assert caplog.text == ""


def test_missing_credentials_file_bind_refuses(tmp_path: Path) -> None:
    # Arrange — a spec-declared account credential bind pointing nowhere.
    absent = tmp_path / "accounts" / "someone" / ".credentials.json"
    binds = f"    binds:\n      - {absent}:/home/agent/.claude/.credentials.json:ro\n"
    cfg = load_config(str(_write_spec(tmp_path, binds)))
    # Act
    check = lambda: validate_capability_binds(cfg, list(cfg.apptainer.binds))
    # Assert
    with pytest.raises(BindCapabilityError):
        check()


def test_spec_binds_checked_returns_declared_binds(tmp_path: Path) -> None:
    # Arrange — the entry point build_run_argv actually calls: read + gate.
    gh_dir = _gh_dir_with_hosts_yml(tmp_path)
    cfg = load_config(str(_write_spec(tmp_path, _gh_bind_yaml(gh_dir))))
    # Act
    binds = spec_binds_checked(cfg)
    # Assert
    assert binds == [f"{gh_dir}:/home/agent/.config/gh:ro"]


def test_spec_binds_checked_refuses_empty_gh_dir(tmp_path: Path) -> None:
    # Arrange — the gate must not be skippable via the read helper.
    gh_dir = _gh_dir_without_hosts_yml(tmp_path)
    cfg = load_config(str(_write_spec(tmp_path, _gh_bind_yaml(gh_dir))))
    # Act
    read = lambda: spec_binds_checked(cfg)
    # Assert
    with pytest.raises(BindCapabilityError):
        read()


def test_single_path_gh_bind_is_checked(tmp_path: Path) -> None:
    # Arrange — the colonless spec form the parser also accepts; src == dst,
    # so it is the SAME credential bind and must not slip past the guard.
    gh_dir = tmp_path / ".config" / "gh"
    gh_dir.mkdir(parents=True)
    (gh_dir / "config.yml").write_text("version: 1\n", encoding="utf-8")
    cfg = load_config(str(_write_spec(tmp_path, "    binds: []\n")))
    # Act
    check = lambda: validate_capability_binds(cfg, [str(gh_dir)])
    # Assert
    with pytest.raises(BindCapabilityError):
        check()


def test_trailing_slash_destination_is_checked(tmp_path: Path) -> None:
    # Arrange — cosmetic trailing slash must not defeat the rule match.
    gh_dir = _gh_dir_without_hosts_yml(tmp_path)
    cfg = load_config(str(_write_spec(tmp_path, "    binds: []\n")))
    bind = f"{gh_dir}:/home/agent/.config/gh/:ro"
    # Act
    check = lambda: validate_capability_binds(cfg, [bind])
    # Assert
    with pytest.raises(BindCapabilityError):
        check()


def test_empty_bind_entry_is_ignored(tmp_path: Path) -> None:
    # Arrange — a blank entry carries no source and no destination.
    cfg = load_config(str(_write_spec(tmp_path, "    binds: []\n")))
    # Act
    result = validate_capability_binds(cfg, ["   "])
    # Assert — no raise, returns None.
    assert result is None
