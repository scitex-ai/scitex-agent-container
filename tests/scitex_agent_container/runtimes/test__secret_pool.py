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
from contextlib import contextmanager
from pathlib import Path

import pytest

from scitex_agent_container.runtimes import _secret_pool as secret_pool_mod
from scitex_agent_container.runtimes._secret_pool import (
    _ALLOWED_SECRET_NAMES,
    _ALLOWED_SECRET_PREFIXES,
    _SECRETS_ENVRC_VAR,
    _parse_secret_file,
    _pool_env,
    read_pool,
)


@contextmanager
def _swap(name: str, value):
    saved = getattr(secret_pool_mod, name)
    setattr(secret_pool_mod, name, value)
    try:
        yield
    finally:
        setattr(secret_pool_mod, name, saved)


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
    pool.chmod(0o600)
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


# ---------------------------------------------------------------------------
# hostile pool files fail closed without executing or disclosing content
# ---------------------------------------------------------------------------


_DISALLOWED_SUBPROCESS_HOOKS = [
    "BASH_ENV",
    "ENV",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "PATH",
    "SHELLOPTS",
    "BASHOPTS",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONUSERBASE",
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
    "JDK_JAVA_OPTIONS",
    "SSH_ASKPASS",
    "PERL5DB",
    "RUSTC_WRAPPER",
    "GIT_EXEC_PATH",
    "GIT_SSH_COMMAND",
    "DYLD_INSERT_LIBRARIES",
    "ZDOTDIR",
]


@pytest.mark.parametrize("name", _DISALLOWED_SUBPROCESS_HOOKS)
def test_subprocess_hook_variable_names_are_filtered(name: str) -> None:
    # Arrange
    content = f"{name}=attacker-controlled\n"

    # Act
    parsed = _parse_secret_file(content)

    # Assert
    assert parsed == {}


@pytest.mark.parametrize("name", sorted(_ALLOWED_SECRET_NAMES))
def test_every_fixed_allowed_secret_name_still_parses(name: str) -> None:
    # Arrange
    content = f"{name}=ordinary-value\n"

    # Act
    parsed = _parse_secret_file(content)

    # Assert
    assert parsed == {name: "ordinary-value"}


@pytest.mark.parametrize("prefix", _ALLOWED_SECRET_PREFIXES)
def test_every_allowed_secret_prefix_still_parses(prefix: str) -> None:
    # Arrange
    name = f"{prefix}ZZ_POSITIVE"

    # Act
    parsed = _parse_secret_file(f"{name}=ordinary-value\n")

    # Assert
    assert parsed == {name: "ordinary-value"}


def test_host_owned_qwen_token_name_is_an_allowed_secret(
    env_save_restore,
) -> None:
    # Arrange — this name is host policy, not pool/spec data, and is resolved
    # at read time so a long-lived listener honours its host configuration.
    env_save_restore.set("SAC_QWEN_GATEWAY_TOKEN_ENV", "SAC_LOCAL_GPTOSS_KEY")

    # Act
    parsed = _parse_secret_file("SAC_LOCAL_GPTOSS_KEY=ordinary-value\n")

    # Assert
    assert parsed == {"SAC_LOCAL_GPTOSS_KEY": "ordinary-value"}


def test_symlink_pool_file_is_rejected(tmp_path: Path, secrets_envrc: None) -> None:
    # Arrange
    target = tmp_path / "outside.src"
    target.write_text("ZZ_SECRET=must-not-escape\n", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "pool.src"
    link.symlink_to(target)
    os.environ[_SECRETS_ENVRC_VAR] = str(link)

    # Act
    read = read_pool()

    # Assert
    assert read.trusted is False and "ZZ_SECRET" not in read.env


def test_world_writable_pool_file_is_rejected(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange
    pool = tmp_path / "pool.src"
    pool.write_text("ZZ_SECRET=must-not-escape\n", encoding="utf-8")
    pool.chmod(0o606)
    os.environ[_SECRETS_ENVRC_VAR] = str(pool)

    # Act
    read = read_pool()

    # Assert
    assert read.trusted is False and "ZZ_SECRET" not in read.env


def test_group_readable_pool_file_is_rejected(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange
    pool = tmp_path / "pool.src"
    pool.write_text("ZZ_SECRET=must-not-escape\n", encoding="utf-8")
    pool.chmod(0o640)
    os.environ[_SECRETS_ENVRC_VAR] = str(pool)

    # Act
    read = read_pool()

    # Assert
    assert read.trusted is False and "ZZ_SECRET" not in read.env


def test_pool_file_not_owned_by_effective_user_is_rejected(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange
    pool = tmp_path / "pool.src"
    pool.write_text("ZZ_SECRET=must-not-escape\n", encoding="utf-8")
    pool.chmod(0o600)
    os.environ[_SECRETS_ENVRC_VAR] = str(pool)

    # Act
    with _swap("_effective_uid", lambda: pool.stat().st_uid + 1):
        read = read_pool()

    # Assert
    assert read.trusted is False and "ZZ_SECRET" not in read.env


def test_pool_replacement_with_symlink_at_open_is_rejected(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange
    pool = tmp_path / "pool.src"
    pool.write_text("ZZ_SECRET=original\n", encoding="utf-8")
    pool.chmod(0o600)
    attacker = tmp_path / "attacker.src"
    attacker.write_text("ZZ_SECRET=must-not-escape\n", encoding="utf-8")
    attacker.chmod(0o600)
    os.environ[_SECRETS_ENVRC_VAR] = str(pool)
    real_open = secret_pool_mod._open_secret_fd
    swapped = False

    def swap_then_open(path, flags):
        nonlocal swapped
        if not swapped and Path(path) == pool:
            swapped = True
            pool.unlink()
            pool.symlink_to(attacker)
        return real_open(path, flags)

    # Act
    with _swap("_open_secret_fd", swap_then_open):
        read = read_pool()

    # Assert
    assert read.trusted is False and "ZZ_SECRET" not in read.env


def test_regular_pool_replaced_by_fifo_is_rejected_without_blocking_open(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — resolve_secret_files has already observed a regular file when
    # the open seam substitutes a FIFO.  Refuse to call the real open unless
    # the production flags make that adversarial open non-blocking.
    pool = tmp_path / "pool.src"
    pool.write_text("ZZ_SECRET=original\n", encoding="utf-8")
    pool.chmod(0o600)
    os.environ[_SECRETS_ENVRC_VAR] = str(pool)
    real_open = secret_pool_mod._open_secret_fd

    def swap_then_open(path, flags):
        if not flags & os.O_NONBLOCK:
            raise RuntimeError("FIFO substitution would block this open")
        pool.unlink()
        os.mkfifo(pool, 0o600)
        return real_open(path, flags)

    # Act
    with _swap("_open_secret_fd", swap_then_open):
        read = read_pool()

    # Assert
    assert read.trusted is False and "ZZ_SECRET" not in read.env


def test_regular_pool_replacement_with_another_regular_inode_is_rejected(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — ownership, mode and file type all still look valid after the
    # swap; only descriptor/path identity distinguishes the attacker file.
    pool = tmp_path / "pool.src"
    pool.write_text("ZZ_SECRET=original\n", encoding="utf-8")
    pool.chmod(0o600)
    attacker = tmp_path / "attacker.src"
    attacker.write_text("ZZ_ATTACKER=must-not-escape\n", encoding="utf-8")
    attacker.chmod(0o600)
    os.environ[_SECRETS_ENVRC_VAR] = str(pool)
    real_open = secret_pool_mod._open_secret_fd

    def swap_then_open(path, flags):
        os.replace(attacker, pool)
        return real_open(path, flags)

    # Act
    with _swap("_open_secret_fd", swap_then_open):
        read = read_pool()

    # Assert
    assert read.trusted is False and "ZZ_ATTACKER" not in read.env


def test_shell_code_in_pool_is_rejected_and_not_executed(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange
    canary = tmp_path / "executed"
    pool = tmp_path / "pool.src"
    pool.write_text(f"ZZ_SECRET=safe; touch {canary}\n", encoding="utf-8")
    pool.chmod(0o600)
    os.environ[_SECRETS_ENVRC_VAR] = str(pool)

    # Act
    read = read_pool()

    # Assert
    assert read.trusted is False and not canary.exists()


def test_pool_parse_error_never_discloses_secret_content(
    tmp_path: Path, secrets_envrc: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    marker = "RAW-SECRET-MUST-NOT-APPEAR"
    pool = tmp_path / "pool.src"
    pool.write_text(f"echo {marker} >&2\n", encoding="utf-8")
    pool.chmod(0o600)
    os.environ[_SECRETS_ENVRC_VAR] = str(pool)

    # Act
    read = read_pool()

    # Assert
    assert marker not in read.detail and marker not in caplog.text
