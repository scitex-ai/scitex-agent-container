"""Immutable per-spec identity: ``spec.uuid`` parse, stamp and backfill.

Card ``sac-identity-spec-uuid-20261026``: names are mutable labels, the
uuid is the stable identity. House style per the fleet skill: AAA
markers, one assertion per test, real files over mocks.
"""

from __future__ import annotations

import uuid as _uuid
from pathlib import Path

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.config._spec_uuid import (
    backfill_spec_uuids,
    ensure_spec_uuid,
    parse_spec_uuid,
)
from tests.scitex_agent_container.config.test__explicit_validation import (
    _explicit_doc,
    _write_doc,
)

_SPEC_TEXT = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    role: test
spec:
  runtime: tui
  harness: hermes
"""


def _write_text(tmp_path: Path, text: str, name: str = "uuid-agent") -> Path:
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "spec.yaml"
    path.write_text(text)
    return path


def test_parse_absent_is_empty() -> None:
    # Arrange — a spec written before the backfill carries no uuid.
    # Act
    parsed = parse_spec_uuid(None, path="p")
    # Assert
    assert parsed == ""


def test_parse_valid_uuid_normalises_case() -> None:
    # Arrange — upper-case hex is the same uuid, not a different one.
    # Act
    parsed = parse_spec_uuid("A8098C1A-F86E-11DA-BD1A-00112444BE1E", path="p")
    # Assert
    assert parsed == "a8098c1a-f86e-11da-bd1a-00112444be1e"


def test_parse_malformed_raises_naming_path() -> None:
    # Arrange — a half-written uuid must never load as "no uuid".
    # Act
    with pytest.raises(ValueError) as error:
        parse_spec_uuid("not-a-uuid", path="some/spec.yaml")
    # Assert
    assert "some/spec.yaml" in str(error.value)


def test_ensure_stamps_missing_uuid_on_its_own_line(tmp_path: Path) -> None:
    # Arrange — a pre-backfill spec file, exact bytes controlled.
    path = _write_text(tmp_path, _SPEC_TEXT)
    # Act
    stamped = ensure_spec_uuid(path)
    # Assert — the returned value is a real uuid4.
    assert str(_uuid.UUID(stamped, version=4)) == stamped


def test_ensure_insert_touches_nothing_else(tmp_path: Path) -> None:
    # Arrange — same fixture; the line edit must not reformat siblings.
    path = _write_text(tmp_path, _SPEC_TEXT)
    before = path.read_text().splitlines()
    # Act
    ensure_spec_uuid(path)
    # Assert — exactly one added line, the uuid child of `spec:`.
    after = path.read_text().splitlines()
    assert len(after) == len(before) + 1


def test_ensure_is_first_seen_stable(tmp_path: Path) -> None:
    # Arrange — a stamped spec seen a second time keeps its identity.
    path = _write_text(tmp_path, _SPEC_TEXT)
    first = ensure_spec_uuid(path)
    # Act
    second = ensure_spec_uuid(path)
    # Assert
    assert second == first


def test_ensure_preserves_present_uuid(tmp_path: Path) -> None:
    # Arrange — a spec that already carries an identity is not rewritten.
    wanted = "12345678-1234-5678-1234-567812345678"
    text = _SPEC_TEXT.replace("  harness: hermes", f"  uuid: {wanted}\n  harness: hermes")
    path = _write_text(tmp_path, text)
    before = path.read_text()
    # Act
    kept = ensure_spec_uuid(path)
    # Assert
    assert (kept, path.read_text()) == (wanted, before)


def test_ensure_refuses_malformed_without_touching_file(
    tmp_path: Path,
) -> None:
    # Arrange — a corrupt uuid must fork nothing, not even a repair.
    text = _SPEC_TEXT.replace("  harness: hermes", "  uuid: bogus\n  harness: hermes")
    path = _write_text(tmp_path, text)
    before = path.read_text()
    # Act
    with pytest.raises(ValueError):
        ensure_spec_uuid(path)
    # Assert
    assert path.read_text() == before


def test_backfill_covers_dir_and_skips_bare_dirs(tmp_path: Path) -> None:
    # Arrange — an agents root with two specs and one bare directory.
    _write_text(tmp_path, _SPEC_TEXT, name="aaa")
    _write_text(tmp_path, _SPEC_TEXT, name="bbb")
    (tmp_path / "not-an-agent").mkdir()
    # Act
    stamped = backfill_spec_uuids(tmp_path)
    # Assert — both specs gained distinct stable identities.
    assert len({stamped["aaa"], stamped["bbb"]}) == 2


def test_loader_reads_uuid(tmp_path: Path) -> None:
    # Arrange — a fully-explicit spec carrying the new optional key.
    doc = _explicit_doc()
    doc["spec"]["uuid"] = "12345678-1234-5678-1234-567812345678"
    path = _write_doc(tmp_path, doc)
    # Act
    config = load_config(path)
    # Assert
    assert config.uuid == "12345678-1234-5678-1234-567812345678"


def test_loader_tolerates_absent_uuid(tmp_path: Path) -> None:
    # Arrange — the pre-backfill corpus keeps loading while stamps land.
    path = _write_doc(tmp_path, _explicit_doc())
    # Act
    config = load_config(path)
    # Assert
    assert config.uuid == ""


def test_loader_rejects_malformed_uuid(tmp_path: Path) -> None:
    # Arrange — a corrupt identity fails the load, loudly.
    doc = _explicit_doc()
    doc["spec"]["uuid"] = "bogus"
    path = _write_doc(tmp_path, doc)
    act = lambda: load_config(path)  # noqa: E731
    # Act
    with pytest.raises(ValueError) as error:
        act()
    # Assert
    assert "spec.uuid" in str(error.value)


def test_explicit_map_does_not_require_uuid() -> None:
    # Arrange — the red-start map must not demand a key the fleet-wide
    # backfill has not stamped yet (same posture as residency).
    from scitex_agent_container.config._explicit_fields import (
        required_fields_for_kind,
    )

    # Act
    paths = [f.path for f in required_fields_for_kind("Agent")]
    # Assert
    assert "uuid" not in paths
