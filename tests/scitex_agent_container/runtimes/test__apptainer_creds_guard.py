"""Tests for the credentials-placeholder delivery guard.

:func:`_apptainer_creds_guard.assert_credentials_placeholder_delivers`
converts the cryptic apptainer FATAL (a credentials file-bind landing on
a host path that the container ``$HOME`` won't resolve to) into a precise,
actionable error BEFORE launch. Covered layouts (handoff item 2):

  * relaxed ``--home`` + ``.img`` / no overlay → no usable upper-home →
    the placeholder at the workspace-home is shadowed by the ``--home``
    tmpfs → RAISE.
  * relaxed ``--home`` + DIRECTORY overlay (usable upper-home) → the
    upper-home bind carries the placeholder past the shadow → no raise.
  * non-relaxed (no raw_args ``--home``) → the workspace-home bind backs
    the container HOME → no raise.
  * placeholder creation failed (``None``) while a bind is emitted → RAISE.
  * no credentials bind (empty ``bind_flags``) → no-op.

PA-306 no-mocks: real ``AgentConfig`` + ``ApptainerSpec`` / ``ClaudeSpec``
instances against ``tmp_path`` real directories. The guard is a pure
function of its inputs, so the bind/placeholder args are computed with the
real production helpers (``credentials_file_bind`` /
``ensure_credentials_bind_target``) rather than hand-built. STX-TQ002 AAA
markers; STX-TQ007 one observable fact per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig, ClaudeSpec
from scitex_agent_container.config._types import ApptainerSpec
from scitex_agent_container.runtimes._apptainer_auth import (
    credentials_file_bind,
    ensure_credentials_bind_target,
)
from scitex_agent_container.runtimes._apptainer_creds_guard import (
    CredentialPlaceholderUndeliverableError,
    assert_credentials_placeholder_delivers,
)
from scitex_agent_container.runtimes._to_home_overlay import (
    resolve_overlay_upper_home,
)

# ---------------------------------------------------------------------------
# Builders — real configs + a real on-disk credentials file
# ---------------------------------------------------------------------------


def _creds_file(tmp_path: Path) -> Path:
    """A real, unexpired OAuth credentials file the binder will accept."""
    creds = tmp_path / "acct" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(
        '{"claudeAiOauth": {"accessToken": "tok", "expiresAt": 9999999999000}}',
        encoding="utf-8",
    )
    return creds


def _relaxed_home_cfg(
    tmp_path: Path, creds: Path, *, overlay: Path | None
) -> AgentConfig:
    """Relaxed spec whose raw_args declare ``--home`` (+ optional --overlay).

    ``overlay=None`` → no overlay (the --home tmpfs shadow has no upper-home
    to escape to). ``overlay`` ending in ``.img`` → loopback (also no
    upper-home). A directory ``overlay`` → a usable upper-home.
    """
    raw_args = ["--containall", "--home", "/home/agent"]
    if overlay is not None:
        raw_args += ["--overlay", str(overlay)]
    cfg = AgentConfig(
        name="relaxed-agent",
        runtime="apptainer",
        workdir=str(tmp_path),
        claude=ClaudeSpec(model="opus", credentials_file=str(creds)),
    )
    cfg.apptainer = ApptainerSpec(relaxed=True, raw_args=raw_args)
    return cfg


def _hardened_cfg(tmp_path: Path, creds: Path) -> AgentConfig:
    """Non-relaxed spec: no raw_args ``--home`` → workspace-home bind backs
    the container HOME (no tmpfs shadow)."""
    cfg = AgentConfig(
        name="hardened-agent",
        runtime="apptainer",
        workdir=str(tmp_path),
        claude=ClaudeSpec(model="opus", credentials_file=str(creds)),
    )
    cfg.apptainer = ApptainerSpec(relaxed=False, raw_args=[])
    return cfg


def _resolved_inputs(
    cfg: AgentConfig, home_host: Path, upper: Path | None
) -> tuple[list[str], Path | None]:
    """Run the real binder + placeholder helpers → ``(bind_flags, placeholder)``.

    Mirrors exactly what ``build_run_argv`` computes before calling the
    guard, so the guard is exercised on production-shaped inputs.
    """
    bind_flags = credentials_file_bind(cfg)
    placeholder = ensure_credentials_bind_target(
        cfg, home_host=home_host, overlay_upper_home=upper, bind_flags=bind_flags
    )
    return bind_flags, placeholder


# ---------------------------------------------------------------------------
# RAISE — relaxed --home with no usable overlay upper-home
# ---------------------------------------------------------------------------


def test_raises_for_relaxed_home_without_overlay(tmp_path: Path) -> None:
    # Arrange — relaxed --home, NO overlay: the workspace-home placeholder is
    # shadowed by the --home tmpfs and there is no upper-home to escape to.
    creds = _creds_file(tmp_path)
    cfg = _relaxed_home_cfg(tmp_path, creds, overlay=None)
    home_host = tmp_path / "state" / "home"
    home_host.mkdir(parents=True)
    bind_flags, placeholder = _resolved_inputs(cfg, home_host, None)
    # Act
    call = lambda: assert_credentials_placeholder_delivers(
        cfg, bind_flags=bind_flags, placeholder=placeholder, overlay_upper_home=None
    )
    # Assert
    pytest.raises(CredentialPlaceholderUndeliverableError, call)


def test_raises_for_relaxed_home_with_img_overlay(tmp_path: Path) -> None:
    # Arrange — relaxed --home + an .img loopback overlay (cannot host an
    # upper/), so resolve_overlay_upper_home returns None → same shadow trap.
    creds = _creds_file(tmp_path)
    img = tmp_path / "overlay.img"
    img.write_bytes(b"\x00")
    cfg = _relaxed_home_cfg(tmp_path, creds, overlay=img)
    home_host = tmp_path / "state" / "home"
    home_host.mkdir(parents=True)
    upper = resolve_overlay_upper_home(cfg)  # None for .img
    bind_flags, placeholder = _resolved_inputs(cfg, home_host, upper)
    # Act
    call = lambda: assert_credentials_placeholder_delivers(
        cfg, bind_flags=bind_flags, placeholder=placeholder, overlay_upper_home=upper
    )
    # Assert
    pytest.raises(CredentialPlaceholderUndeliverableError, call)


def test_error_message_names_the_bind_destination(tmp_path: Path) -> None:
    # Arrange — the diagnostic must name the in-container destination so the
    # operator can match it to apptainer's would-be FATAL line. The raise is
    # caught with try/except (not pytest.raises) so the single TQ007 assertion
    # is the message-content check this test is named for.
    creds = _creds_file(tmp_path)
    cfg = _relaxed_home_cfg(tmp_path, creds, overlay=None)
    home_host = tmp_path / "state" / "home"
    home_host.mkdir(parents=True)
    bind_flags, placeholder = _resolved_inputs(cfg, home_host, None)
    # Act
    message = ""
    try:
        assert_credentials_placeholder_delivers(
            cfg, bind_flags=bind_flags, placeholder=placeholder, overlay_upper_home=None
        )
    except CredentialPlaceholderUndeliverableError as exc:
        message = str(exc)
    # Assert
    assert "/home/agent/.claude/.credentials.json" in message


# ---------------------------------------------------------------------------
# RAISE — placeholder creation failed (the swallowed-OSError path)
# ---------------------------------------------------------------------------


def test_raises_when_placeholder_is_none_despite_bind(tmp_path: Path) -> None:
    # Arrange — a bind WILL be emitted but ensure_credentials_bind_target
    # returned None (placeholder touch failed). The guard must not let the
    # launch proceed to a destination that does not exist.
    creds = _creds_file(tmp_path)
    cfg = _hardened_cfg(tmp_path, creds)
    bind_flags = credentials_file_bind(cfg)
    # Act
    call = lambda: assert_credentials_placeholder_delivers(
        cfg, bind_flags=bind_flags, placeholder=None, overlay_upper_home=None
    )
    # Assert
    pytest.raises(CredentialPlaceholderUndeliverableError, call)


# ---------------------------------------------------------------------------
# NO-OP — layouts that already deliver, and the no-credential case
# ---------------------------------------------------------------------------


def test_noop_for_relaxed_home_with_directory_overlay(tmp_path: Path) -> None:
    # Arrange — relaxed --home + a DIRECTORY overlay: the upper-home bind is
    # emitted OVER the --home tmpfs, carrying the placeholder past the shadow.
    creds = _creds_file(tmp_path)
    overlay = tmp_path / "ov"
    cfg = _relaxed_home_cfg(tmp_path, creds, overlay=overlay)
    home_host = tmp_path / "state" / "home"
    home_host.mkdir(parents=True)
    upper = resolve_overlay_upper_home(cfg)
    upper.mkdir(parents=True)  # deploy_to_home_overlay creates this pre-launch
    bind_flags, placeholder = _resolved_inputs(cfg, home_host, upper)
    # Act
    assert_credentials_placeholder_delivers(
        cfg, bind_flags=bind_flags, placeholder=placeholder, overlay_upper_home=upper
    )
    # Assert — placeholder landed inside the usable upper-home, no raise.
    assert placeholder == upper / ".claude" / ".credentials.json"


def test_noop_for_non_relaxed_workspace_home_delivery(tmp_path: Path) -> None:
    # Arrange — non-relaxed: no --home tmpfs, so the workspace-home bind backs
    # the container HOME and the placeholder there is delivered.
    creds = _creds_file(tmp_path)
    cfg = _hardened_cfg(tmp_path, creds)
    home_host = tmp_path / "state" / "home"
    home_host.mkdir(parents=True)
    bind_flags, placeholder = _resolved_inputs(cfg, home_host, None)
    # Act
    assert_credentials_placeholder_delivers(
        cfg, bind_flags=bind_flags, placeholder=placeholder, overlay_upper_home=None
    )
    # Assert — placeholder under the workspace-home, no raise.
    assert placeholder == home_host / ".claude" / ".credentials.json"


def test_noop_when_no_credentials_bind(tmp_path: Path) -> None:
    # Arrange — no credentials_file / account → no bind is emitted; the guard
    # must be a no-op even for a relaxed --home + no-overlay spec.
    cfg = AgentConfig(
        name="no-creds",
        runtime="apptainer",
        workdir=str(tmp_path),
        claude=ClaudeSpec(model="opus"),
    )
    cfg.apptainer = ApptainerSpec(
        relaxed=True, raw_args=["--containall", "--home", "/home/agent"]
    )
    # Act
    assert_credentials_placeholder_delivers(
        cfg, bind_flags=[], placeholder=None, overlay_upper_home=None
    )
    # Assert — empty bind_flags is a clean no-op (no exception raised).
    assert credentials_file_bind(cfg) == []
