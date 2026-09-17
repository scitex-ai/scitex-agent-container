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
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from scitex_agent_container.runtimes._envrc import (
    EnvrcEvalError,
    eval_envrc,
    eval_envrc_cascade,
    fold_envrc_cascade_into_env,
    fold_envrc_into_env,
    resolve_secret_files,
)

_SECRETS_VAR = "SAC_SECRETS_ENVRC"
_SSH_KEYGEN = shutil.which("ssh-keygen")
_SETSID = shutil.which("setsid")
_PERL = shutil.which("perl")
_CARGO = shutil.which("cargo")
_SYSTEM_PYTHON = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else None


def _seed_default_pool(home: Path, *names: str) -> list[Path]:
    """Create ``$HOME/.bash.d/secrets/010_scitex/<name>`` real files."""
    d = home / ".bash.d" / "secrets" / "010_scitex"
    d.mkdir(parents=True)
    out: list[Path] = []
    for n in names:
        f = d / n
        f.write_text("# secret\n", encoding="utf-8")
        out.append(f)
    return out


# ---------------------------------------------------------------------------
# resolve_secret_files — the caller-independent-pool class fix (2026-07-18)
# ---------------------------------------------------------------------------


def test_resolve_secret_files_honours_explicit_var(tmp_path: Path) -> None:
    # Arrange — an explicit SAC_SECRETS_ENVRC wins verbatim over any default.
    explicit = tmp_path / "explicit.src"
    explicit.write_text("# s\n", encoding="utf-8")
    _seed_default_pool(tmp_path, "01_default.src")
    env = {"SAC_SECRETS_ENVRC": str(explicit)}
    # Act
    files = resolve_secret_files(environ=env, home=tmp_path)
    # Assert
    assert files == [explicit]


def test_resolve_secret_files_default_includes_nested_provider_key_pool(
    tmp_path: Path,
) -> None:
    # Arrange — provider API keys live under 000_ENV/api_keys, while the
    # historical CCT/GitHub pool lives under 010_scitex.
    provider = (
        tmp_path
        / ".bash.d/secrets/000_ENV/api_keys/10_llm_opencode.src"
    )
    provider.parent.mkdir(parents=True)
    provider.write_text("OPENCODE_GO_API_KEY=test-only\n", encoding="utf-8")
    standard = _seed_default_pool(tmp_path, "01_agent-container.src")[0]

    # Act
    files = resolve_secret_files(environ={}, home=tmp_path)

    # Assert
    assert files == [provider, standard]


def test_resolve_secret_files_falls_back_to_canonical_default(tmp_path: Path) -> None:
    # Arrange — the var is UNSET (the cron / raw-ssh / timer case), but the
    # operator's standardized secret files exist under $HOME.
    seeded = _seed_default_pool(tmp_path, "01_a.src", "02_b.src")
    # Act — no SAC_SECRETS_ENVRC in the environment at all.
    files = resolve_secret_files(environ={}, home=tmp_path)
    # Assert — the canonical default is loaded (sorted), so the pool is found
    # even though nobody exported the var.
    assert files == sorted(seeded)


def test_resolve_secret_files_empty_var_uses_default(tmp_path: Path) -> None:
    # Arrange — a stray blank export is not a real value; treat as unset.
    seeded = _seed_default_pool(tmp_path, "01_a.src")
    # Act
    files = resolve_secret_files(environ={"SAC_SECRETS_ENVRC": ""}, home=tmp_path)
    # Assert
    assert files == seeded


def test_resolve_secret_files_no_pool_is_a_safe_noop(tmp_path: Path) -> None:
    # Arrange — no var and no default directory: a host with no pool.
    # Act
    files = resolve_secret_files(environ={}, home=tmp_path)
    # Assert — empty, exactly the pre-fix behaviour (never a raise).
    assert files == []


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


def _envrc_error(action) -> EnvrcEvalError | None:
    try:
        action()
    except EnvrcEvalError as exc:
        return exc
    return None


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


def test_eval_envrc_error_does_not_expose_raw_stderr(tmp_path: Path) -> None:
    # Arrange
    marker = "RAW-STDERR-SECRET-MUST-NOT-APPEAR"
    envrc = tmp_path / ".envrc"
    envrc.write_text(f"echo {marker} >&2\nexit 1\n", encoding="utf-8")

    # Act
    error = _envrc_error(lambda: eval_envrc(envrc))

    # Assert
    assert error is not None and marker not in str(error)


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
    # Arrange — a supported provider secret; the .envrc references its var.
    secret = tmp_path / "secret.env"
    secret.write_text("export SCITEX_GENAI_GATEWAY_API_KEY=abc123\n", encoding="utf-8")
    secret.chmod(0o600)
    os.environ[_SECRETS_VAR] = str(secret)
    envrc = tmp_path / ".envrc"
    envrc.write_text(
        'export PUBLIC="$SCITEX_GENAI_GATEWAY_API_KEY"\n', encoding="utf-8"
    )
    # Act
    out = eval_envrc(envrc)
    # Assert — the .envrc reference resolved to the real secret value.
    assert out.get("PUBLIC") == "abc123"


def test_secrets_preamble_does_not_leak_source_secret(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — same setup as the resolve test.
    secret = tmp_path / "secret.env"
    secret.write_text("export SCITEX_GENAI_GATEWAY_API_KEY=abc123\n", encoding="utf-8")
    secret.chmod(0o600)
    os.environ[_SECRETS_VAR] = str(secret)
    envrc = tmp_path / ".envrc"
    envrc.write_text(
        'export PUBLIC="$SCITEX_GENAI_GATEWAY_API_KEY"\n', encoding="utf-8"
    )
    # Act
    out = eval_envrc(envrc)
    # Assert — the source secret var is NOT folded (cancels in the diff).
    assert "SCITEX_GENAI_GATEWAY_API_KEY" not in out


def test_secret_preamble_shell_code_is_rejected_without_execution(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange
    canary = tmp_path / "secret-file-executed"
    secret = tmp_path / "secret.env"
    secret.write_text(f"SECRET_TOK=$(touch {canary})\n", encoding="utf-8")
    secret.chmod(0o600)
    os.environ[_SECRETS_VAR] = str(secret)
    envrc = tmp_path / ".envrc"
    envrc.write_text('export PUBLIC="$SECRET_TOK"\n', encoding="utf-8")

    # Act
    error = _envrc_error(lambda: eval_envrc(envrc))

    # Assert
    assert error is not None and not canary.exists()


def test_secret_preamble_bash_env_is_filtered_before_bash_can_execute_it(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — BASH_ENV is sourced by non-interactive bash even with
    # --noprofile/--norc. The payload and pool are both legitimate owner-only
    # regular files, so only the positive name policy can stop this execution.
    canary = tmp_path / "bash-env-executed"
    payload = tmp_path / "payload.sh"
    payload.write_text(f"touch {canary}\n", encoding="utf-8")
    payload.chmod(0o600)
    secret = tmp_path / "secret.env"
    secret.write_text(f"BASH_ENV={payload}\n", encoding="utf-8")
    secret.chmod(0o600)
    os.environ[_SECRETS_VAR] = str(secret)
    envrc = tmp_path / ".envrc"
    envrc.write_text("export ORDINARY_VALUE=still-safe\n", encoding="utf-8")

    # Act
    out = eval_envrc(envrc)

    # Assert
    assert out.get("ORDINARY_VALUE") == "still-safe" and not canary.exists()


@pytest.mark.skipif(
    _SYSTEM_PYTHON is None,
    reason="a non-venv Python is required for the PYTHONUSERBASE canary",
)
def test_secret_preamble_pythonuserbase_cannot_execute_pth(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — a plain child Python automatically imports executable .pth
    # lines from PYTHONUSERBASE. Exercise the interpreter, not just the parser.
    canary = tmp_path / "python-userbase-executed"
    user_site = (
        tmp_path
        / "python-userbase/lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    user_site.mkdir(parents=True)
    (user_site / "attacker.pth").write_text(
        f"import pathlib; pathlib.Path({str(canary)!r}).touch()\n",
        encoding="utf-8",
    )
    secret = tmp_path / "python-hook.env"
    secret.write_text(f"PYTHONUSERBASE={user_site.parents[2]}\n", encoding="utf-8")
    secret.chmod(0o600)
    os.environ[_SECRETS_VAR] = str(secret)
    envrc = tmp_path / ".envrc"
    envrc.write_text(
        f"{shlex.quote(str(_SYSTEM_PYTHON))} -c pass\nexport SAFE_RESULT=ok\n",
        encoding="utf-8",
    )
    control_env = dict(os.environ)
    control_env["PYTHONUSERBASE"] = str(user_site.parents[2])
    subprocess.run([str(_SYSTEM_PYTHON), "-c", "pass"], check=True, env=control_env)
    control_executed = canary.exists()
    canary.unlink(missing_ok=True)

    # Act
    out = eval_envrc(envrc)

    # Assert
    assert (control_executed, canary.exists(), out.get("SAFE_RESULT")) == (
        True,
        False,
        "ok",
    )


@pytest.mark.parametrize(
    "hook_name", ["JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS"]
)
def test_secret_preamble_java_option_hooks_never_reach_runtime_launcher(
    tmp_path: Path, secrets_envrc: None, hook_name: str
) -> None:
    # Arrange — use a hermetic launcher so the test does not require a JRE.
    # It consumes each documented JVM option variable at the child boundary.
    canary = tmp_path / f"{hook_name}-executed"
    launcher = tmp_path / "java"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib\n"
        f"value = os.environ.get({hook_name!r})\n"
        "if value: pathlib.Path(value).touch()\n",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    secret = tmp_path / "java-hook.env"
    secret.write_text(f"{hook_name}={canary}\n", encoding="utf-8")
    secret.chmod(0o600)
    os.environ[_SECRETS_VAR] = str(secret)
    envrc = tmp_path / ".envrc"
    envrc.write_text(f"{shlex.quote(str(launcher))}\n", encoding="utf-8")
    control_env = dict(os.environ)
    control_env[hook_name] = str(canary)
    subprocess.run([str(launcher)], check=True, env=control_env)
    control_executed = canary.exists()
    canary.unlink(missing_ok=True)

    # Act
    eval_envrc(envrc)

    # Assert
    assert (control_executed, canary.exists()) == (True, False)


@pytest.mark.skipif(
    _SSH_KEYGEN is None or _SETSID is None,
    reason="OpenSSH and setsid are required for the execution canary",
)
def test_secret_preamble_ssh_askpass_cannot_execute_helper(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — ssh-keygen invokes SSH_ASKPASS for an encrypted key when no
    # terminal is available. Exercise the real OpenSSH helper hook.
    key = tmp_path / "encrypted-key"
    subprocess.run(
        [
            str(_SSH_KEYGEN),
            "-q",
            "-t",
            "ed25519",
            "-N",
            "test-passphrase",
            "-f",
            str(key),
        ],
        check=True,
    )
    canary = tmp_path / "ssh-askpass-executed"
    askpass = tmp_path / "askpass"
    askpass.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(canary))}\nprintf '%s\\n' test-passphrase\n",
        encoding="utf-8",
    )
    askpass.chmod(0o700)
    secret = tmp_path / "ssh-hook.env"
    secret.write_text(f"SSH_ASKPASS={askpass}\n", encoding="utf-8")
    secret.chmod(0o600)
    os.environ[_SECRETS_VAR] = str(secret)
    envrc = tmp_path / ".envrc"
    envrc.write_text(
        "export DISPLAY=:0\nexport SSH_ASKPASS_REQUIRE=force\n"
        f"{shlex.quote(str(_SETSID))} {shlex.quote(str(_SSH_KEYGEN))} -y -f "
        f"{shlex.quote(str(key))} >/dev/null\n",
        encoding="utf-8",
    )
    control_env = dict(os.environ)
    control_env.update(
        {
            "DISPLAY": ":0",
            "SSH_ASKPASS_REQUIRE": "force",
            "SSH_ASKPASS": str(askpass),
        }
    )
    subprocess.run(
        [str(_SETSID), str(_SSH_KEYGEN), "-y", "-f", str(key)],
        check=True,
        env=control_env,
        stdout=subprocess.DEVNULL,
    )
    control_executed = canary.exists()
    canary.unlink(missing_ok=True)

    # Act
    eval_envrc(envrc)

    # Assert
    assert (control_executed, canary.exists()) == (True, False)


@pytest.mark.skipif(_PERL is None, reason="Perl is required for the execution canary")
def test_secret_preamble_perl5db_cannot_execute_debugger_hook(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — PERL5DB is executable Perl loaded by `perl -d` before user code.
    canary = tmp_path / "perl5db-executed"
    hook = (
        f"BEGIN {{ open(my $fh, '>', {str(canary)!r}); close($fh); }} "
        "package DB; sub DB {}"
    )
    secret = tmp_path / "perl-hook.env"
    secret.write_text(f'PERL5DB="{hook}"\n', encoding="utf-8")
    secret.chmod(0o600)
    os.environ[_SECRETS_VAR] = str(secret)
    envrc = tmp_path / ".envrc"
    envrc.write_text(
        f"PERLDB_OPTS=NonStop=1 {shlex.quote(str(_PERL))} -d -e 0 >/dev/null 2>&1\n",
        encoding="utf-8",
    )
    control_env = dict(os.environ)
    control_env.update({"PERL5DB": hook, "PERLDB_OPTS": "NonStop=1"})
    subprocess.run(
        [str(_PERL), "-d", "-e", "0"],
        check=True,
        env=control_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    control_executed = canary.exists()
    canary.unlink(missing_ok=True)

    # Act
    eval_envrc(envrc)

    # Assert
    assert (control_executed, canary.exists()) == (True, False)


@pytest.mark.skipif(_CARGO is None, reason="Cargo is required for the execution canary")
def test_secret_preamble_rustc_wrapper_cannot_execute_wrapper(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — Cargo executes RUSTC_WRAPPER before invoking rustc.
    canary = tmp_path / "rustc-wrapper-executed"
    wrapper = tmp_path / "rustc-wrapper"
    wrapper.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(canary))}\nexec \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    secret = tmp_path / "rust-hook.env"
    secret.write_text(f"RUSTC_WRAPPER={wrapper}\n", encoding="utf-8")
    secret.chmod(0o600)
    os.environ[_SECRETS_VAR] = str(secret)
    project = tmp_path / "rust-project"
    (project / "src").mkdir(parents=True)
    (project / "Cargo.toml").write_text(
        '[package]\nname = "hook_canary"\nversion = "0.0.0"\nedition = "2021"\n',
        encoding="utf-8",
    )
    (project / "src/main.rs").write_text("fn main() {}\n", encoding="utf-8")
    attack_target = tmp_path / "attack-target"
    envrc = tmp_path / ".envrc"
    envrc.write_text(
        f"cd {shlex.quote(str(project))}\n"
        f"CARGO_TARGET_DIR={shlex.quote(str(attack_target))} "
        f"{shlex.quote(str(_CARGO))} check -q\n",
        encoding="utf-8",
    )
    control_env = dict(os.environ)
    control_env.update(
        {
            "RUSTC_WRAPPER": str(wrapper),
            "CARGO_TARGET_DIR": str(tmp_path / "control-target"),
        }
    )
    subprocess.run(
        [str(_CARGO), "check", "-q"], check=True, cwd=project, env=control_env
    )
    control_executed = canary.exists()
    canary.unlink(missing_ok=True)

    # Act
    eval_envrc(envrc)

    # Assert
    assert (control_executed, canary.exists()) == (True, False)


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
