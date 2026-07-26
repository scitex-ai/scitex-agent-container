"""Tests for ``sac agents explain`` — the effective-launch-plan renderer.

Covers the two safety-critical behaviours (secret redaction, the
workdir-backing check) plus the unknown-agent error path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scitex_agent_container.cli_pkg._explain import (
    _identity_lines,
    _pwd_is_backed,
    _redact,
    explain,
)


def test_redact_masks_a_secret_named_value() -> None:
    # Arrange — an env entry whose KEY looks like a secret.
    entry = "SAC_ANTHROPIC_API_KEY=sk-ant-oat01-supersecret"
    # Act
    out = _redact(entry)
    # Assert — the value never appears verbatim.
    assert "sk-ant-oat01-supersecret" not in out


def test_redact_reports_the_secret_length_not_value() -> None:
    # Arrange
    entry = "SAC_LISTEN_BEARER=abcdef"
    # Act
    out = _redact(entry)
    # Assert
    assert out == "SAC_LISTEN_BEARER=<redacted: 6 chars>"


def test_redact_leaves_non_secret_env_untouched() -> None:
    # Arrange — an ordinary, non-secret env entry.
    entry = "CLAUDE_AGENT_ID=proj-scitex-dev"
    # Act
    out = _redact(entry)
    # Assert
    assert out == "CLAUDE_AGENT_ID=proj-scitex-dev"


def test_pwd_is_backed_true_when_under_a_bind_target() -> None:
    # Arrange — workdir nested inside a bound directory.
    binds = [("/home/u", "/home/u", "rw")]
    # Act
    backed = _pwd_is_backed("/home/u/proj/x", binds)
    # Assert
    assert backed is True


def test_pwd_is_backed_false_when_no_bind_covers_it() -> None:
    # Arrange — workdir not under any bind target (no cwd in container).
    binds = [("/data", "/capsule", "ro")]
    # Act
    backed = _pwd_is_backed("/work", binds)
    # Assert
    assert backed is False


def test_explain_unknown_agent_raises_click_exception() -> None:
    # Arrange
    from click.testing import CliRunner

    runner = CliRunner()
    # Act
    result = runner.invoke(explain, ["definitely-no-such-agent-xyz"])
    # Assert — fail-loud with a hint, not a stack trace.
    assert "no agent named" in result.output


def test_explain_forwards_requested_profile(monkeypatch) -> None:
    # Arrange
    from click.testing import CliRunner

    from scitex_agent_container.cli_pkg import _explain as module

    seen: dict[str, object] = {}

    def fake_load(path: str, *, profile: str | None = None):
        seen.update(path=path, profile=profile)
        return object()

    monkeypatch.setattr(module, "_spec_path_for", lambda _name: Path("/tmp/spec.yaml"))
    monkeypatch.setattr(module, "load_config", fake_load)
    monkeypatch.setattr(module, "render_plan", lambda _config, spec_path=None: "plan")
    # Act
    result = CliRunner().invoke(explain, ["sales", "--profile", "codex"])
    # Assert
    assert (result.exit_code, seen.get("profile")) == (0, "codex")


def test_identity_lines_show_profile_harness_and_backend() -> None:
    # Arrange
    config = SimpleNamespace(
        name="sales",
        labels={},
        profile="codex",
        harness="claude-code",
        backend="codex",
        runtime="tui",
    )
    # Act
    lines = _identity_lines(
        config, spec_path=Path("/tmp/spec.yaml"), sif="image.sif", claude=object()
    )
    # Assert
    assert any(
        "profile: codex   harness: claude-code   backend: codex" in line
        for line in lines
    )
