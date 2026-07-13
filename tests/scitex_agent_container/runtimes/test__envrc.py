"""Evaluate ``to_home/.envrc`` into env vars for ``--env-file`` injection.

Covers ``_envrc.{eval_envrc, fold_envrc_into_env}``: a direnv-style ``.envrc``
is a shell script apptainer ``--env-file`` can't parse, so ``deploy_to_home``
evaluates it host-side (after the sibling ``.env``) and folds the net env into
the materialised ``.env``. Real bash + tmp files — no mocks (PA-306).
STX-TQ002 AAA-marker + STX-TQ007 one-assert.

Named ``test__envrc.py`` for the PS-204 §2 orphan-test mirror against
``src/scitex_agent_container/runtimes/_envrc.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from scitex_agent_container.runtimes._envrc import (
    EnvrcEvalError,
    eval_envrc,
    eval_envrc_cascade,
    fold_envrc_cascade_into_env,
    fold_envrc_into_env,
)

_SECRETS_VAR = "SAC_SECRETS_ENVRC"


@pytest.fixture
def secrets_envrc() -> Iterator[None]:
    """Save/restore ``SAC_SECRETS_ENVRC`` so a test may set it freely."""
    saved = os.environ.get(_SECRETS_VAR)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(_SECRETS_VAR, None)
        else:
            os.environ[_SECRETS_VAR] = saved


def test_eval_envrc_captures_exported_var(tmp_path: Path) -> None:
    # Arrange
    envrc = tmp_path / ".envrc"
    envrc.write_text("export FIGRECIPE_FOO=bar\n", encoding="utf-8")
    # Act
    out = eval_envrc(envrc)
    # Assert
    assert out.get("FIGRECIPE_FOO") == "bar"


def test_eval_envrc_evaluates_command_substitution(tmp_path: Path) -> None:
    # Arrange
    envrc = tmp_path / ".envrc"
    envrc.write_text("export COMPUTED=$(printf abc)\n", encoding="utf-8")
    # Act
    out = eval_envrc(envrc)
    # Assert
    assert out.get("COMPUTED") == "abc"


def test_eval_envrc_sees_base_env_first(tmp_path: Path) -> None:
    # Arrange — .envrc references a var defined in the sibling .env.
    base = tmp_path / ".env"
    base.write_text("BASE_TOKEN=xyz\n", encoding="utf-8")
    envrc = tmp_path / ".envrc"
    envrc.write_text('export DERIVED="${BASE_TOKEN}-suffix"\n', encoding="utf-8")
    # Act
    out = eval_envrc(envrc, base_env=base)
    # Assert
    assert out.get("DERIVED") == "xyz-suffix"


def test_eval_envrc_raises_on_nonzero_exit(tmp_path: Path) -> None:
    # Arrange — a .envrc that fails (exit 1).
    envrc = tmp_path / ".envrc"
    envrc.write_text("echo boom >&2\nexit 1\n", encoding="utf-8")
    # Act
    # Assert
    with pytest.raises(EnvrcEvalError):
        eval_envrc(envrc)


def test_fold_envrc_writes_combined_env_file(tmp_path: Path) -> None:
    # Arrange — dest has both .env and .envrc; the .envrc adds a var.
    (tmp_path / ".env").write_text("FROM_ENV=1\n", encoding="utf-8")
    (tmp_path / ".envrc").write_text("export FROM_ENVRC=2\n", encoding="utf-8")
    # Act
    fold_envrc_into_env(tmp_path)
    # Assert — the folded .env carries BOTH sources.
    text = (tmp_path / ".env").read_text()
    assert "FROM_ENV=1" in text and "FROM_ENVRC=2" in text


def test_fold_envrc_noop_when_no_envrc(tmp_path: Path) -> None:
    # Arrange — only a .env, no .envrc.
    env_file = tmp_path / ".env"
    env_file.write_text("ONLY=env\n", encoding="utf-8")
    # Act
    fold_envrc_into_env(tmp_path)
    # Assert — .env left exactly as materialised.
    assert env_file.read_text() == "ONLY=env\n"


def test_fold_envrc_sets_owner_only_perms(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / ".envrc").write_text("export SECRET=s\n", encoding="utf-8")
    # Act
    fold_envrc_into_env(tmp_path)
    # Assert — the folded .env is chmod 0600.
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


def test_eval_envrc_cascade_later_layer_overrides_earlier(tmp_path: Path) -> None:
    # Arrange — two layers set the same var; the later (higher-precedence) wins.
    low = tmp_path / "low"
    high = tmp_path / "high"
    low.mkdir()
    high.mkdir()
    (low / ".envrc").write_text("export CCT_BOT_TOKEN=low\n", encoding="utf-8")
    (high / ".envrc").write_text("export CCT_BOT_TOKEN=high\n", encoding="utf-8")
    # Act
    out = eval_envrc_cascade([low / ".envrc", high / ".envrc"])
    # Assert
    assert out.get("CCT_BOT_TOKEN") == "high"


def test_eval_envrc_cascade_skips_none_and_missing(tmp_path: Path) -> None:
    # Arrange — one real layer amid a None and a nonexistent entry.
    real = tmp_path / "real"
    real.mkdir()
    (real / ".envrc").write_text("export ONLY=here\n", encoding="utf-8")
    # Act
    out = eval_envrc_cascade([None, tmp_path / "ghost" / ".envrc", real / ".envrc"])
    # Assert
    assert out.get("ONLY") == "here"


def test_eval_envrc_cascade_base_env_visible_to_layer(tmp_path: Path) -> None:
    # Arrange — a layer derives its token from a var set in the base .env.
    base = tmp_path / ".env"
    base.write_text("POOL_TODO=tok-todo\n", encoding="utf-8")
    layer = tmp_path / "layer"
    layer.mkdir()
    (layer / ".envrc").write_text(
        'export CCT_BOT_TOKEN="${POOL_TODO}"\n', encoding="utf-8"
    )
    # Act
    out = eval_envrc_cascade([layer / ".envrc"], base_env=base)
    # Assert
    assert out.get("CCT_BOT_TOKEN") == "tok-todo"


def test_fold_envrc_cascade_writes_combined_env_file(tmp_path: Path) -> None:
    # Arrange — dest .env plus a higher-precedence external layer .envrc.
    dest = tmp_path / "home"
    dest.mkdir()
    (dest / ".env").write_text("FROM_ENV=1\n", encoding="utf-8")
    layer = tmp_path / "proj"
    layer.mkdir()
    (layer / ".envrc").write_text("export FROM_LAYER=2\n", encoding="utf-8")
    # Act
    fold_envrc_cascade_into_env(dest, [layer / ".envrc"])
    # Assert — the folded .env carries BOTH sources.
    text = (dest / ".env").read_text()
    assert "FROM_ENV=1" in text and "FROM_LAYER=2" in text


def test_secrets_preamble_resolves_referenced_secret(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — a secret file in scope; the .envrc references its var.
    secret = tmp_path / "secret.env"
    secret.write_text("export SECRET_TOK=abc123\n", encoding="utf-8")
    os.environ[_SECRETS_VAR] = str(secret)
    envrc = tmp_path / ".envrc"
    envrc.write_text('export PUBLIC="$SECRET_TOK"\n', encoding="utf-8")
    # Act
    out = eval_envrc(envrc)
    # Assert — the .envrc reference resolved to the real secret value.
    assert out.get("PUBLIC") == "abc123"


def test_secrets_preamble_does_not_leak_source_secret(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — same setup as the resolve test.
    secret = tmp_path / "secret.env"
    secret.write_text("export SECRET_TOK=abc123\n", encoding="utf-8")
    os.environ[_SECRETS_VAR] = str(secret)
    envrc = tmp_path / ".envrc"
    envrc.write_text('export PUBLIC="$SECRET_TOK"\n', encoding="utf-8")
    # Act
    out = eval_envrc(envrc)
    # Assert — the source secret var is NOT folded (cancels in the diff).
    assert "SECRET_TOK" not in out


def test_empty_unresolved_reference_is_dropped(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — SAC_SECRETS_ENVRC unset; .envrc references an undefined var,
    # so the export resolves to an empty string.
    os.environ.pop(_SECRETS_VAR, None)
    envrc = tmp_path / ".envrc"
    envrc.write_text('export PUBLIC="$SECRET_TOK"\n', encoding="utf-8")
    # Act
    out = eval_envrc(envrc)
    # Assert — an empty value is DROPPED (not folded as ""), so it cannot shadow
    # a real value a later layer supplies under another spelling.
    assert "PUBLIC" not in out


def test_fold_omits_empty_valued_var(tmp_path: Path) -> None:
    # Arrange — .envrc exports one real var and one that resolves empty.
    (tmp_path / ".envrc").write_text(
        'export REAL=ok\nexport EMPTY="$UNSET_SOURCE"\n', encoding="utf-8"
    )
    # Act
    fold_envrc_into_env(tmp_path)
    # Assert — the empty var is not written into the folded .env.
    assert "EMPTY=" not in (tmp_path / ".env").read_text()


def test_cascade_drops_legacy_identity_alias_from_base_env(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — a stale legacy alias (SCITEX_TODO_AGENT) already sits in the
    # base .env (as it does for the affected agents); the .envrc sets only the
    # current _ID name. This mirrors the self-perpetuating-loop that keeps the
    # legacy var alive across deploys.
    os.environ.pop(_SECRETS_VAR, None)
    base = tmp_path / ".env"
    base.write_text(
        "SCITEX_TODO_AGENT=someagent\nSCITEX_TODO_AGENT_ID=someagent\n",
        encoding="utf-8",
    )
    envrc = tmp_path / ".envrc"
    envrc.write_text('export SCITEX_TODO_AGENT_ID="someagent"\n', encoding="utf-8")
    # Act — the real fold path used in production (base .env sourced as base_env).
    out = eval_envrc_cascade([envrc], base_env=base)
    # Assert — the deprecated alias is dropped (scitex-todo MCP hard-rejects it).
    assert "SCITEX_TODO_AGENT" not in out


def test_cascade_keeps_current_id_var_from_base_env(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — same setup as the drop test.
    os.environ.pop(_SECRETS_VAR, None)
    base = tmp_path / ".env"
    base.write_text(
        "SCITEX_TODO_AGENT=someagent\nSCITEX_TODO_AGENT_ID=someagent\n",
        encoding="utf-8",
    )
    envrc = tmp_path / ".envrc"
    envrc.write_text('export SCITEX_TODO_AGENT_ID="someagent"\n', encoding="utf-8")
    # Act
    out = eval_envrc_cascade([envrc], base_env=base)
    # Assert — the CURRENT identity var survives (container --env-file needs it).
    assert out.get("SCITEX_TODO_AGENT_ID") == "someagent"


def test_fold_cascade_rewrites_env_without_legacy_alias(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — a stale legacy alias in the materialised .env; the real
    # fold_envrc_cascade_into_env rewrites the file in place.
    os.environ.pop(_SECRETS_VAR, None)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SCITEX_TODO_AGENT=someagent\nSCITEX_TODO_AGENT_ID=someagent\n",
        encoding="utf-8",
    )
    envrc = tmp_path / ".envrc"
    envrc.write_text('export SCITEX_TODO_AGENT_ID="someagent"\n', encoding="utf-8")
    # Act
    fold_envrc_cascade_into_env(tmp_path, [envrc])
    # Assert — the rewritten .env no longer carries the fatal legacy alias.
    assert "SCITEX_TODO_AGENT=" not in env_file.read_text()
