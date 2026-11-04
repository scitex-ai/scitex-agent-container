"""The secrets pool read, and whether a MISS in it means anything.

Covers ``runtimes/_secret_pool.read_pool``. A HIT is self-validating; the
question with three answers is what a MISS means, and ``PoolRead.trusted`` is
that answer.

WHY THIS FLAG EXISTS AT ALL (card
``sac-cct-token-slot-mismatch-and-env-fold-20260812``): after a relocation,
three consecutive diagnoses said "there is no token on compute-04". The pool
file was on that host, complete. What was missing was ``SAC_SECRETS_ENVRC`` in
the LAUNCHING process, so sac read the bare process env and could not tell the
difference. The operator's correction — 「04 にトークンが無い」と私は言ったが誤り。
**起動プロセスに無かった**が正しい。この区別がバグそのもの。 — is the whole
specification for these tests.

Real temp pool files and real bash sourcing, exactly as
``test__cct_token_pool.py`` does — no mocks (PA-306). STX-TQ002 AAA markers,
STX-TQ007 one assert per test. Slot names use a ``ZZ_``-prefixed namespace so
an operator shell's real pool vars can never collide with the fixtures.

Named ``test__secret_pool.py`` for the PS-202/PS-204 mirror against
``src/scitex_agent_container/runtimes/_secret_pool.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from scitex_agent_container.runtimes._secret_pool import (
    _SECRETS_ENVRC_VAR,
    _pool_env,
    read_pool,
)


@pytest.fixture
def secrets_envrc() -> Iterator[None]:
    """Save/restore ``SAC_SECRETS_ENVRC`` so a test may set it freely."""
    saved = os.environ.get(_SECRETS_ENVRC_VAR)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(_SECRETS_ENVRC_VAR, None)
        else:
            os.environ[_SECRETS_ENVRC_VAR] = saved


def _real_pool_file(tmp_path: Path, body: str) -> None:
    """Write a REAL secrets file and point ``SAC_SECRETS_ENVRC`` at it."""
    pool = tmp_path / "pool.src"
    pool.write_text(body, encoding="utf-8")
    os.environ[_SECRETS_ENVRC_VAR] = str(pool)


# ---------------------------------------------------------------------------
# a conclusive read
# ---------------------------------------------------------------------------


def test_a_sourced_secret_file_is_a_conclusive_read(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — a real pool file that a real bash can source.
    _real_pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_TRUST=zz-value\n")
    # Act
    read = read_pool()
    # Assert — sac read the pool it meant to read, so a miss would mean something.
    assert read.trusted is True


def test_a_sourced_secret_file_yields_its_slots(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange
    _real_pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_PRESENT=zz-value\n")
    # Act
    read = read_pool()
    # Assert
    assert read.env.get("CCT_BOT_TOKEN_ZZ_PRESENT") == "zz-value"


def test_a_conclusive_read_carries_no_complaint(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange
    _real_pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_QUIET=zz-value\n")
    # Act
    read = read_pool()
    # Assert — ``detail`` is the reason a read is UNtrusted; a good read has none.
    assert read.detail == ""


# ---------------------------------------------------------------------------
# an INCONCLUSIVE read — the relocation shape
# ---------------------------------------------------------------------------


def test_no_resolvable_secret_file_is_an_inconclusive_read(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — the var is set (so the $HOME default is not consulted) but
    # points nowhere, which is how a unit / ssh / cron caller sees the pool.
    os.environ[_SECRETS_ENVRC_VAR] = str(tmp_path / "absent.src")
    # Act
    read = read_pool()
    # Assert — sac never opened a pool file, so it learned nothing about absence.
    assert read.trusted is False


def test_an_inconclusive_read_still_carries_the_process_env(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — the process env can still PROVE a slot present; it just cannot
    # prove one absent, which is why the flag is separate from the mapping.
    os.environ[_SECRETS_ENVRC_VAR] = str(tmp_path / "absent.src")
    os.environ["ZZ_POOL_PROBE"] = "zz-here"
    try:
        # Act
        read = read_pool()
    finally:
        os.environ.pop("ZZ_POOL_PROBE", None)
    # Assert
    assert read.env.get("ZZ_POOL_PROBE") == "zz-here"


def test_an_inconclusive_read_blames_the_launching_process(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — the detail must point at the vantage point, not at the host,
    # because "there is no token on this host" was the wrong diagnosis three
    # times running.
    os.environ[_SECRETS_ENVRC_VAR] = str(tmp_path / "absent.src")
    # Act
    read = read_pool()
    # Assert
    assert "LAUNCHING PROCESS" in read.detail


def test_an_inconclusive_read_refuses_to_claim_absence(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange
    os.environ[_SECRETS_ENVRC_VAR] = str(tmp_path / "absent.src")
    # Act
    read = read_pool()
    # Assert
    assert "never proves one ABSENT" in read.detail


# ---------------------------------------------------------------------------
# back-compat
# ---------------------------------------------------------------------------


def test_pool_env_still_returns_the_bare_mapping(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — callers that only ask "is this slot here?" keep working.
    _real_pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_COMPAT=zz-value\n")
    # Act
    env = _pool_env()
    # Assert
    assert env.get("CCT_BOT_TOKEN_ZZ_COMPAT") == "zz-value"


def _bare_pool_read(tmp_path: Path, **overrides: str) -> dict:
    """Exercise real pool sourcing in a child with no inherited credentials."""
    import json
    import subprocess
    import sys

    source = Path(__file__).resolve().parents[3] / "src"
    script = """
import sys,json
sys.path.insert(0,sys.argv[1])
from scitex_agent_container.runtimes._secret_pool import read_pool
pool=read_pool()
print(json.dumps({'trusted':pool.trusted,
 'canonical':pool.env.get('OPENCODE_GO_API_KEY_01')=='owned-canonical-fixture',
 'legacy':pool.env.get('ZZ_LEGACY_POOL')=='owned-legacy-fixture'}))
"""
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "SCITEX_DIR": str(tmp_path / "diagnostics"),
        **overrides,
    }
    child = subprocess.run(
        [sys.executable, "-B", "-I", "-c", script, str(source)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(child.stdout)


def test_bare_process_sources_canonical_api_directory(tmp_path: Path) -> None:
    # Arrange
    api = tmp_path / ".bash.d/secrets/000_ENV/api_keys"
    api.mkdir(parents=True)
    (api / "provider.src").write_text(
        "export OPENCODE_GO_API_KEY_01=owned-canonical-fixture\n"
    )
    # Act
    result = _bare_pool_read(tmp_path)
    # Assert
    assert result == {"trusted": True, "canonical": True, "legacy": False}


def test_canonical_rotation_wins_legacy_and_inherited_values(tmp_path: Path) -> None:
    # Arrange
    legacy = tmp_path / ".bash.d/secrets/010_scitex"
    canonical = tmp_path / ".bash.d/secrets/000_ENV/api_keys"
    legacy.mkdir(parents=True)
    canonical.mkdir(parents=True)
    (legacy / "99_old_copy.src").write_text(
        "export OPENCODE_GO_API_KEY_01=owned-old-fixture\n"
        "export ZZ_LEGACY_POOL=owned-legacy-fixture\n"
    )
    (canonical / "00_provider.src").write_text(
        "export OPENCODE_GO_API_KEY_01=owned-canonical-fixture\n"
    )
    # Act
    result = _bare_pool_read(tmp_path, OPENCODE_GO_API_KEY_01="owned-inherited-fixture")
    # Assert
    assert result == {"trusted": True, "canonical": True, "legacy": True}


def test_bare_process_keeps_legacy_only_pool_compatible(tmp_path: Path) -> None:
    # Arrange
    legacy = tmp_path / ".bash.d/secrets/010_scitex"
    legacy.mkdir(parents=True)
    (legacy / "legacy.src").write_text("export ZZ_LEGACY_POOL=owned-legacy-fixture\n")
    # Act
    result = _bare_pool_read(tmp_path)
    # Assert
    assert result == {"trusted": True, "canonical": False, "legacy": True}


def test_explicit_pool_overrides_both_default_directories(tmp_path: Path) -> None:
    # Arrange
    api = tmp_path / ".bash.d/secrets/000_ENV/api_keys"
    api.mkdir(parents=True)
    (api / "provider.src").write_text(
        "export OPENCODE_GO_API_KEY_01=owned-canonical-fixture\n"
    )
    explicit = tmp_path / "explicit.src"
    explicit.write_text("export ZZ_LEGACY_POOL=owned-legacy-fixture\n")
    # Act
    result = _bare_pool_read(tmp_path, SAC_SECRETS_ENVRC=str(explicit))
    # Assert
    assert result == {"trusted": True, "canonical": False, "legacy": True}


def _listen_fixture(
    tmp_path: Path,
    *,
    source_import: bool = True,
    symlink: bool = False,
    launcher: str = "python",
) -> tuple[Path, dict]:
    """Own every destination and prevent any real systemctl operation."""
    import sys

    root = Path(__file__).resolve().parents[3]
    binaries = tmp_path / "bin"
    binaries.mkdir()
    console = binaries / "sac"
    console.write_text(f"#!{sys.executable}\nraise AssertionError('not invoked')\n")
    console.chmod(0o755)
    if launcher == "non-python":
        console.write_text("#!/bin/sh\nexit 91\n")
    elif launcher == "dangling":
        console.unlink()
        console.symlink_to(tmp_path / "absent-console")
    elif launcher == "missing-interpreter":
        console.write_text(f"#!{tmp_path}/absent/python\n")
    if symlink:
        link = tmp_path / "pipx-sac"
        link.symlink_to(console)
        console = link
    systemctl = binaries / "systemctl"
    systemctl.write_text('#!/bin/sh\ntouch "$OWN_SYSTEMCTL_MARKER"\nexit 97\n')
    systemctl.chmod(0o755)
    env = {
        "HOME": str(tmp_path),
        "PATH": str(binaries) + ":/usr/bin:/bin",
        "SAC_BIN": str(console),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "SCITEX_DIR": str(tmp_path / "diagnostics"),
        "OWN_SYSTEMCTL_MARKER": str(tmp_path / "systemctl-called"),
    }
    if source_import:
        env["PYTHONPATH"] = str(root / "src")
    return root / "scripts/systemd/install-sac-listen.sh", env


def _listen_source_value(script: Path, env: dict):
    """Run the actual enumeration functions, without installer mutations."""
    import subprocess

    text = script.read_text()
    functions = []
    for name in ["resolve_sac_python", "secrets_envrc_value"]:
        start = text.index(name + "() {")
        functions.append(text[start : text.index("\n}\n", start) + 3])
    shell = "log() { printf '%s\\n' \"$*\" >&2; }\n" + "\n".join(functions)
    return subprocess.run(
        ["bash", "-c", shell + "\nsecrets_envrc_value"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("symlink", [False, True])
def test_listen_installer_delegates_to_console_python(tmp_path: Path, symlink) -> None:
    # Arrange
    from scitex_agent_container.runtimes._envrc import resolve_secret_files

    script, env = _listen_fixture(tmp_path, symlink=symlink)
    for directory in ["010_scitex", "000_ENV/api_keys"]:
        target = tmp_path / ".bash.d/secrets" / directory
        target.mkdir(parents=True)
        (target / "provider.src").write_text("exit 91 # enumeration must not source\n")
    expected = ":".join(str(p) for p in resolve_secret_files(environ={}, home=tmp_path))
    # Act
    child = _listen_source_value(script, env)
    # Assert
    assert (child.returncode, child.stdout) == (0, expected)


def test_listen_explicit_override_does_not_probe_unrelated_python(
    tmp_path: Path,
) -> None:
    # Arrange
    script, env = _listen_fixture(tmp_path)
    Path(env["SAC_BIN"]).write_text("#!/bin/sh\nexit 91\n")
    explicit = str(tmp_path / "missing.src") + ":" + str(tmp_path / "first.src")
    env["SAC_SECRETS_ENVRC"] = explicit
    # Act
    child = _listen_source_value(script, env)
    # Assert
    assert (child.returncode, child.stdout) == (0, explicit)


@pytest.mark.parametrize("launcher", ["non-python", "dangling", "missing-interpreter"])
def test_listen_unsupported_launcher_refuses_before_writes(
    tmp_path: Path, launcher
) -> None:
    # Arrange
    import subprocess

    script, env = _listen_fixture(tmp_path, launcher=launcher)
    # Act
    child = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=30
    )
    # Assert
    assert (
        child.returncode != 0,
        (tmp_path / "config").exists(),
        (tmp_path / "systemctl-called").exists(),
        "ERROR:" in child.stderr,
    ) == (True, False, False, True)


def test_listen_missing_resolver_refuses_before_writes(tmp_path: Path) -> None:
    # Arrange
    import subprocess
    import venv

    script, env = _listen_fixture(tmp_path, source_import=False)
    empty = tmp_path / "empty-python"
    venv.EnvBuilder(with_pip=False).create(empty)
    Path(env["SAC_BIN"]).write_text(f"#!{empty}/bin/python\n")
    # Act
    child = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=30
    )
    # Assert
    assert (
        child.returncode != 0,
        (tmp_path / "config").exists(),
        (tmp_path / "systemctl-called").exists(),
        "own Python environment" in child.stderr,
    ) == (True, False, False, True)
