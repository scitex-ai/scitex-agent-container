"""Tests for apptainer overlay provisioning (:mod:`_apptainer_overlay`).

Regression cover for the 2026-07-13 stillborn-agent bug: `sac agents create
<name> --template python_developer --start` wrote a valid spec, then the START
died in apptainer's container_creation phase with

    FATAL: while loading overlay images: failed to open overlay image
    <...>/containers/overlays/<name>/: ... no such file or directory

because NOTHING in sac ever created the per-agent overlay directory. The fleet's
overlays existed only as an incidental side-effect of ``deploy_to_home_overlay``,
whose resolver reads only the SPACE-SEPARATED ``--overlay <path>`` raw_arg — and
the dir-template emits the ``=``-JOINED ``--overlay=<path>`` spelling that
apptainer accepts equally. The resolver saw nothing, the side-effect never fired,
the agent was stillborn.

The pinned behaviour: a brand-new agent's overlay is auto-provisioned (root +
``upper/`` + ``work/``) before ``apptainer exec``, for BOTH spellings; an
unprovisionable overlay fails LOUD with the exact path and the exact fix, never
a raw apptainer FATAL.

PA-306 no-mocks: real ``AgentConfig`` / ``ApptainerSpec`` against real
``tmp_path`` directories. STX-TQ002 AAA markers, STX-TQ007 one assert per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ApptainerSpec
from scitex_agent_container.runtimes._apptainer_overlay import (
    OVERLAY_DIR_MODE,
    OverlayProvisionError,
    ensure_overlay_dirs,
    is_image_overlay,
    overlay_flags,
    raw_arg_value,
    resolve_overlay_declaration,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _cfg(
    tmp_path: Path,
    *,
    raw_args: list[str] | None = None,
    overlay: str = "",
    overlay_size: str = "",
) -> AgentConfig:
    """Real AgentConfig whose apptainer block declares an overlay however
    the caller likes — modeled field, or either raw_args spelling."""
    cfg = AgentConfig(name="brand-new", runtime="tui", workdir=str(tmp_path))
    cfg.apptainer = ApptainerSpec(
        relaxed=True,
        raw_args=list(raw_args or []),
        overlay=overlay,
        overlay_size=overlay_size,
    )
    return cfg


def _template_raw_args(overlay_dir: Path) -> list[str]:
    """The EXACT raw_args shape ``_template_python_developer`` emits — the
    ``=``-joined spelling that used to resolve to "no overlay declared"."""
    return [
        "--userns",
        "--containall",
        "--home=/home/agent",
        f"--overlay={overlay_dir}/",
    ]


# ---------------------------------------------------------------------------
# raw_arg_value — both spellings apptainer accepts
# ---------------------------------------------------------------------------


def test_raw_arg_value_reads_space_separated_spelling() -> None:
    # Arrange — the hand-authored fleet spec shape.
    raw = ["--containall", "--overlay", "/ov", "--userns"]
    # Act
    value = raw_arg_value(raw, "--overlay")
    # Assert
    assert value == "/ov"


def test_raw_arg_value_reads_equals_joined_spelling() -> None:
    # Arrange — the dir-template shape sac used to be BLIND to (the bug).
    raw = ["--containall", "--overlay=/ov", "--userns"]
    # Act
    value = raw_arg_value(raw, "--overlay")
    # Assert
    assert value == "/ov"


def test_raw_arg_value_returns_empty_when_flag_absent() -> None:
    # Arrange
    raw = ["--containall", "--userns"]
    # Act
    value = raw_arg_value(raw, "--overlay")
    # Assert
    assert value == ""


def test_raw_arg_value_returns_empty_for_dangling_flag() -> None:
    # Arrange — trailing flag with no value must not IndexError.
    raw = ["--containall", "--overlay"]
    # Act
    value = raw_arg_value(raw, "--overlay")
    # Assert
    assert value == ""


# ---------------------------------------------------------------------------
# resolve_overlay_declaration — what apptainer will actually receive
# ---------------------------------------------------------------------------


def test_resolve_declaration_finds_equals_joined_raw_arg(tmp_path: Path) -> None:
    # Arrange — the stillborn-agent spec shape.
    overlay = tmp_path / "overlays" / "brand-new"
    cfg = _cfg(tmp_path, raw_args=_template_raw_args(overlay))
    # Act
    resolved = resolve_overlay_declaration(cfg)
    # Assert — trailing slash normalised away by Path.
    assert resolved == overlay


def test_resolve_declaration_finds_space_separated_raw_arg(tmp_path: Path) -> None:
    # Arrange — the live fleet's hand-authored shape.
    overlay = tmp_path / "overlays" / "scholar"
    cfg = _cfg(tmp_path, raw_args=["--overlay", str(overlay)])
    # Act
    resolved = resolve_overlay_declaration(cfg)
    # Assert
    assert resolved == overlay


def test_resolve_declaration_prefers_modeled_field(tmp_path: Path) -> None:
    # Arrange — the inline `full` template uses spec.apptainer.overlay.
    overlay = tmp_path / "modeled"
    cfg = _cfg(tmp_path, overlay=str(overlay), raw_args=["--overlay", "/ignored"])
    # Act
    resolved = resolve_overlay_declaration(cfg)
    # Assert
    assert resolved == overlay


def test_resolve_declaration_anchors_relative_path_to_workdir(tmp_path: Path) -> None:
    # Arrange
    cfg = _cfg(tmp_path, overlay="rel-ov")
    # Act
    resolved = resolve_overlay_declaration(cfg)
    # Assert
    assert resolved == tmp_path / "rel-ov"


def test_resolve_declaration_none_when_no_overlay_declared(tmp_path: Path) -> None:
    # Arrange
    cfg = _cfg(tmp_path, raw_args=["--containall"])
    # Act
    resolved = resolve_overlay_declaration(cfg)
    # Assert
    assert resolved is None


# ---------------------------------------------------------------------------
# is_image_overlay — never mkdir over a loopback image
# ---------------------------------------------------------------------------


def test_is_image_overlay_true_for_img_suffix(tmp_path: Path) -> None:
    # Arrange
    candidate = tmp_path / "ov.img"
    # Act
    verdict = is_image_overlay(candidate)
    # Assert
    assert verdict is True


def test_is_image_overlay_true_when_overlay_size_declared(tmp_path: Path) -> None:
    # Arrange — overlay_size IS the "sized loopback image" contract.
    candidate = tmp_path / "sized"
    # Act
    verdict = is_image_overlay(candidate, overlay_size="5G")
    # Assert
    assert verdict is True


def test_is_image_overlay_true_for_existing_file(tmp_path: Path) -> None:
    # Arrange — an image already created by `apptainer overlay create`.
    image = tmp_path / "already"
    image.write_bytes(b"")
    # Act
    verdict = is_image_overlay(image)
    # Assert
    assert verdict is True


def test_is_image_overlay_false_for_plain_directory_path(tmp_path: Path) -> None:
    # Arrange
    candidate = tmp_path / "overlays" / "brand-new"
    # Act
    verdict = is_image_overlay(candidate)
    # Assert
    assert verdict is False


# ---------------------------------------------------------------------------
# ensure_overlay_dirs — THE regression: brand-new agent gets an overlay
# ---------------------------------------------------------------------------


def test_ensure_creates_overlay_root_for_equals_joined_spec(tmp_path: Path) -> None:
    # Arrange — the EXACT spec shape that FATAL'd apptainer.
    overlay = tmp_path / "overlays" / "brand-new"
    cfg = _cfg(tmp_path, raw_args=_template_raw_args(overlay))
    # Act
    ensure_overlay_dirs(cfg)
    # Assert — the root apptainer lstat()s now exists.
    assert overlay.is_dir()


def test_ensure_creates_upper_dir(tmp_path: Path) -> None:
    # Arrange
    overlay = tmp_path / "overlays" / "brand-new"
    cfg = _cfg(tmp_path, raw_args=_template_raw_args(overlay))
    # Act
    ensure_overlay_dirs(cfg)
    # Assert — same layout every live fleet overlay carries.
    assert (overlay / "upper").is_dir()


def test_ensure_creates_work_dir(tmp_path: Path) -> None:
    # Arrange
    overlay = tmp_path / "overlays" / "brand-new"
    cfg = _cfg(tmp_path, raw_args=_template_raw_args(overlay))
    # Act
    ensure_overlay_dirs(cfg)
    # Assert
    assert (overlay / "work").is_dir()


def test_ensure_provisions_dirs_with_fleet_permissions(tmp_path: Path) -> None:
    # Arrange — live overlays (scitex-scholar, figrecipe, …) are all 0755.
    overlay = tmp_path / "overlays" / "brand-new"
    cfg = _cfg(tmp_path, raw_args=_template_raw_args(overlay))
    # Act
    ensure_overlay_dirs(cfg)
    # Assert
    assert (overlay / "upper").stat().st_mode & 0o777 == OVERLAY_DIR_MODE


def test_ensure_also_provisions_space_separated_spec(tmp_path: Path) -> None:
    # Arrange — the fleet's other spelling must be provisioned too.
    overlay = tmp_path / "overlays" / "scholar"
    cfg = _cfg(tmp_path, raw_args=["--overlay", str(overlay)])
    # Act
    ensure_overlay_dirs(cfg)
    # Assert
    assert (overlay / "work").is_dir()


def test_ensure_also_provisions_modeled_overlay_field(tmp_path: Path) -> None:
    # Arrange — the inline `full` template's spec.apptainer.overlay.
    overlay = tmp_path / "overlays" / "modeled"
    cfg = _cfg(tmp_path, overlay=f"{overlay}/")
    # Act
    ensure_overlay_dirs(cfg)
    # Assert
    assert (overlay / "upper").is_dir()


def test_ensure_returns_the_overlay_root(tmp_path: Path) -> None:
    # Arrange
    overlay = tmp_path / "overlays" / "brand-new"
    cfg = _cfg(tmp_path, raw_args=_template_raw_args(overlay))
    # Act
    resolved = ensure_overlay_dirs(cfg)
    # Assert
    assert resolved == overlay


def test_ensure_is_idempotent_across_restarts(tmp_path: Path) -> None:
    # Arrange — an agent restarts; its overlay carries accumulated state.
    overlay = tmp_path / "overlays" / "brand-new"
    cfg = _cfg(tmp_path, raw_args=_template_raw_args(overlay))
    ensure_overlay_dirs(cfg)
    (overlay / "upper" / "keep-me").write_text("state\n", encoding="utf-8")
    # Act — second start must not disturb the existing overlay.
    ensure_overlay_dirs(cfg)
    # Assert
    assert (overlay / "upper" / "keep-me").read_text(encoding="utf-8") == "state\n"


def test_ensure_leaves_existing_overlay_permissions_untouched(tmp_path: Path) -> None:
    # Arrange — operator (or the agent) tightened its own overlay.
    overlay = tmp_path / "overlays" / "brand-new"
    cfg = _cfg(tmp_path, raw_args=_template_raw_args(overlay))
    ensure_overlay_dirs(cfg)
    (overlay / "upper").chmod(0o700)
    # Act
    ensure_overlay_dirs(cfg)
    # Assert — no chmod of a dir that already exists.
    assert (overlay / "upper").stat().st_mode & 0o777 == 0o700


def test_ensure_noop_without_any_overlay_declaration(tmp_path: Path) -> None:
    # Arrange — a spec with no overlay at all.
    cfg = _cfg(tmp_path, raw_args=["--containall"])
    # Act
    resolved = ensure_overlay_dirs(cfg)
    # Assert
    assert resolved is None


def test_ensure_never_mkdirs_a_sized_image_overlay(tmp_path: Path) -> None:
    # Arrange — overlay_size means `apptainer overlay create` owns this path;
    # a directory here would shadow the image and break that path outright.
    image = tmp_path / "ov.img"
    cfg = _cfg(tmp_path, overlay=str(image), overlay_size="100M")
    # Act
    ensure_overlay_dirs(cfg)
    # Assert
    assert not image.exists()


def test_ensure_fails_loud_naming_the_path_when_unprovisionable(
    tmp_path: Path,
) -> None:
    # Arrange — overlay parent is a FILE, so mkdir cannot succeed.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir\n", encoding="utf-8")
    overlay = blocker / "brand-new"
    cfg = _cfg(tmp_path, raw_args=_template_raw_args(overlay))
    # Act
    # Assert — the actionable hint names the exact path, not a raw FATAL.
    with pytest.raises(OverlayProvisionError, match=str(overlay)):
        ensure_overlay_dirs(cfg)


def test_ensure_failure_hint_carries_the_mkdir_command(tmp_path: Path) -> None:
    # Arrange
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir\n", encoding="utf-8")
    cfg = _cfg(tmp_path, raw_args=_template_raw_args(blocker / "brand-new"))
    # Act — capture the message; a pytest.raises here would be a 2nd assertion.
    message = ""
    try:
        ensure_overlay_dirs(cfg)
    except OverlayProvisionError as exc:
        message = str(exc)
    # Assert — constitution: always give an actionable hint.
    assert "mkdir -p" in message


# ---------------------------------------------------------------------------
# overlay_flags — the curated --overlay flag for the MODELED field
# ---------------------------------------------------------------------------


def test_overlay_flags_emits_flag_for_provisioned_directory(tmp_path: Path) -> None:
    # Arrange
    overlay = tmp_path / "overlays" / "modeled"
    cfg = _cfg(tmp_path, overlay=str(overlay))
    ensure_overlay_dirs(cfg)
    # Act
    flags = overlay_flags(cfg)
    # Assert
    assert flags == ["--overlay", str(overlay)]


def test_overlay_flags_empty_for_raw_args_only_overlay(tmp_path: Path) -> None:
    # Arrange — raw_args overlays pass through verbatim; no curated flag, so
    # the launch never carries a DUPLICATE --overlay.
    overlay = tmp_path / "overlays" / "brand-new"
    cfg = _cfg(tmp_path, raw_args=_template_raw_args(overlay))
    # Act
    flags = overlay_flags(cfg)
    # Assert
    assert flags == []


def test_overlay_flags_fails_loud_for_missing_image_overlay(tmp_path: Path) -> None:
    # Arrange — image overlay, no overlay_size → nothing can create it.
    cfg = _cfg(tmp_path, overlay=str(tmp_path / "ov.img"))
    # Act
    # Assert
    with pytest.raises(FileNotFoundError, match="overlay_size"):
        overlay_flags(cfg)

# EOF
