"""Tests for :mod:`scitex_agent_container._listen._inline_spec_preflight`.

Real I/O against tmp_path; bind sources are real files/dirs on disk
or deliberately-missing paths. AAA + one assert per test (PA-307).
The preflight is a pure function over the filesystem — no mocks.

Pinning the wire-shape contract here so clew + future SAC-from-SAC
clients can branch on it without grepping prose. Per the clew review
on #287, the failure-body keys are:

  * ``kind`` (top-level branch tag) = ``"bind_unresolvable"``
  * ``details.binds[]`` (was ``unresolvable``) — array form so multi-
    capsule callers see EVERY miss in one round-trip
  * ``details.binds[].source`` (was ``bind``) — raw spec entry
  * ``details.binds[].container_path`` (was ``container_dest``)
  * ``details.binds[].host_normalized`` (was always-emitted
    ``host_resolved``) — present ONLY when ``~``/``$VAR`` expansion
    changed the source
  * ``details.translation_hint`` (was ``remediation_hint``)
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._listen._inline_spec_preflight import (
    BindCheck,
    PreflightResult,
    preflight_bind_sources,
    preflight_failure_response_body,
)

# ---------------------------------------------------------------------------
# Spec builders
# ---------------------------------------------------------------------------


def _spec_with_binds(binds: list) -> dict:
    return {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"apptainer": {"binds": binds}},
    }


# ---------------------------------------------------------------------------
# preflight_bind_sources — happy path
# ---------------------------------------------------------------------------


def test_preflight_ok_when_no_binds() -> None:
    # Arrange — spec carries no apptainer.binds at all.
    spec = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"apptainer": {}},
    }
    # Act
    result = preflight_bind_sources(spec)
    # Assert
    assert result.ok is True


def test_preflight_ok_when_all_binds_exist(tmp_path: Path) -> None:
    # Arrange — two real dirs on disk, both referenced as binds.
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    spec = _spec_with_binds([f"{tmp_path}/a:/inside_a:ro", f"{tmp_path}/b:/inside_b"])
    # Act
    result = preflight_bind_sources(spec)
    # Assert
    assert result.ok is True


def test_preflight_records_every_bind_in_input_order(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    spec = _spec_with_binds([f"{tmp_path}/x:/x", f"{tmp_path}/y:/y"])
    # Act
    result = preflight_bind_sources(spec)
    # Assert
    assert tuple(c.bind for c in result.checks) == (
        f"{tmp_path}/x:/x",
        f"{tmp_path}/y:/y",
    )


def test_preflight_records_container_dest(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / "src").mkdir()
    spec = _spec_with_binds([f"{tmp_path}/src:/capsule:ro"])
    # Act
    result = preflight_bind_sources(spec)
    # Assert
    assert result.checks[0].container_dest == "/capsule"


def test_preflight_expands_tilde_in_host_src(tmp_path: Path, env_save_restore) -> None:
    # Arrange — ~ should resolve to $HOME, set to a real tmp dir.
    env_save_restore.set("HOME", str(tmp_path))
    (tmp_path / "data").mkdir()
    spec = _spec_with_binds(["~/data:/data:ro"])
    # Act
    result = preflight_bind_sources(spec)
    # Assert
    assert result.ok is True


def test_preflight_expands_envvar_in_host_src(tmp_path: Path, env_save_restore) -> None:
    # Arrange — $MYDIR is set to a real tmp path.
    env_save_restore.set("MYDIR", str(tmp_path))
    spec = _spec_with_binds(["$MYDIR:/inside"])
    # Act
    result = preflight_bind_sources(spec)
    # Assert
    assert result.ok is True


# ---------------------------------------------------------------------------
# preflight_bind_sources — failure detection
# ---------------------------------------------------------------------------


def test_preflight_fails_when_one_bind_missing(tmp_path: Path) -> None:
    # Arrange — a real dir + a deliberately-missing path.
    (tmp_path / "real").mkdir()
    spec = _spec_with_binds([f"{tmp_path}/real:/r", f"{tmp_path}/nope/missing:/n:ro"])
    # Act
    result = preflight_bind_sources(spec)
    # Assert
    assert result.ok is False


def test_preflight_unresolvable_list_only_includes_missing(
    tmp_path: Path,
) -> None:
    # Arrange
    (tmp_path / "ok").mkdir()
    spec = _spec_with_binds([f"{tmp_path}/ok:/x", f"{tmp_path}/missing:/y"])
    # Act
    result = preflight_bind_sources(spec)
    missing_binds = [c.bind for c in result.unresolvable]
    # Assert
    assert missing_binds == [f"{tmp_path}/missing:/y"]


def test_preflight_records_exists_on_host_per_entry(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / "real").mkdir()
    spec = _spec_with_binds([f"{tmp_path}/real:/r", f"{tmp_path}/gone:/g"])
    # Act
    result = preflight_bind_sources(spec)
    states = {c.host_resolved: c.exists_on_host for c in result.checks}
    # Assert
    assert states == {f"{tmp_path}/real": True, f"{tmp_path}/gone": False}


def test_preflight_records_resolved_host_path(tmp_path: Path, env_save_restore) -> None:
    # Arrange — verify the resolved path is the tilde-expanded form.
    env_save_restore.set("HOME", str(tmp_path))
    (tmp_path / "d").mkdir()
    spec = _spec_with_binds(["~/d:/d"])
    # Act
    result = preflight_bind_sources(spec)
    # Assert
    assert result.checks[0].host_resolved == f"{tmp_path}/d"


# ---------------------------------------------------------------------------
# preflight_bind_sources — defensive parsing
# ---------------------------------------------------------------------------


def test_preflight_handles_dict_bind_form(tmp_path: Path) -> None:
    # Arrange — apptainer parser accepts {src, dst, mode}; preflight too.
    (tmp_path / "ok").mkdir()
    spec = _spec_with_binds(
        [{"src": str(tmp_path / "ok"), "dst": "/inside", "mode": "ro"}]
    )
    # Act
    result = preflight_bind_sources(spec)
    # Assert
    assert result.ok is True


def test_preflight_flags_malformed_bind_string(tmp_path: Path) -> None:
    # Arrange — missing colon means the entry isn't a valid bind.
    spec = _spec_with_binds(["this-is-not-a-bind"])
    # Act
    result = preflight_bind_sources(spec)
    # Assert
    assert result.ok is False


def test_preflight_flags_malformed_dict_bind(tmp_path: Path) -> None:
    # Arrange — dict missing 'src'.
    spec = _spec_with_binds([{"dst": "/x"}])
    # Act
    result = preflight_bind_sources(spec)
    # Assert
    assert result.ok is False


def test_preflight_collapses_to_ok_when_spec_shape_unexpected() -> None:
    # Arrange — no spec.apptainer.binds → nothing to check → ok=True.
    weird_spec = {"unexpected": "shape"}
    # Act
    result = preflight_bind_sources(weird_spec)
    # Assert
    assert result.ok is True


def test_preflight_returns_dataclass_results(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / "ok").mkdir()
    spec = _spec_with_binds([f"{tmp_path}/ok:/o"])
    # Act
    result = preflight_bind_sources(spec)
    # Assert
    assert isinstance(result, PreflightResult)


def test_preflight_per_bind_results_are_dataclass(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / "ok").mkdir()
    spec = _spec_with_binds([f"{tmp_path}/ok:/o"])
    # Act
    result = preflight_bind_sources(spec)
    # Assert
    assert isinstance(result.checks[0], BindCheck)


# ---------------------------------------------------------------------------
# preflight_failure_response_body — stable wire shape
# ---------------------------------------------------------------------------


def test_failure_body_uses_kind_bind_unresolvable(tmp_path: Path) -> None:
    # Arrange
    spec = _spec_with_binds([f"{tmp_path}/missing:/x"])
    result = preflight_bind_sources(spec)
    # Act
    body = preflight_failure_response_body(result)
    # Assert
    assert body["kind"] == "bind_unresolvable"


def test_failure_body_has_error_field(tmp_path: Path) -> None:
    # Arrange
    spec = _spec_with_binds([f"{tmp_path}/missing:/x"])
    result = preflight_bind_sources(spec)
    # Act
    body = preflight_failure_response_body(result)
    # Assert
    assert "error" in body


def test_failure_body_lists_unresolvable_binds_only(tmp_path: Path) -> None:
    # Arrange — one OK, one missing; only the missing one should appear
    # under the renamed ``details.binds`` array (was ``unresolvable``).
    (tmp_path / "ok").mkdir()
    spec = _spec_with_binds([f"{tmp_path}/ok:/o", f"{tmp_path}/missing:/m"])
    result = preflight_bind_sources(spec)
    # Act
    body = preflight_failure_response_body(result)
    listed_sources = [e["source"] for e in body["details"]["binds"]]
    # Assert
    assert listed_sources == [f"{tmp_path}/missing:/m"]


def test_failure_body_carries_translation_hint(tmp_path: Path) -> None:
    # Arrange — hint renamed from ``remediation_hint`` to
    # ``translation_hint`` per clew review (names what the caller
    # actually needs to DO — translate the path).
    spec = _spec_with_binds([f"{tmp_path}/missing:/x"])
    result = preflight_bind_sources(spec)
    # Act
    body = preflight_failure_response_body(result)
    # Assert
    assert body["details"]["translation_hint"] != ""


def test_failure_body_entry_has_container_path(tmp_path: Path) -> None:
    # Arrange — container destination is echoed under the renamed
    # ``container_path`` key (was ``container_dest``).
    spec = _spec_with_binds([f"{tmp_path}/missing:/capsule"])
    result = preflight_bind_sources(spec)
    # Act
    body = preflight_failure_response_body(result)
    # Assert
    assert body["details"]["binds"][0]["container_path"] == "/capsule"


def test_failure_body_each_entry_has_exists_on_host_false(
    tmp_path: Path,
) -> None:
    # Arrange — all listed entries must be the missing ones.
    spec = _spec_with_binds([f"{tmp_path}/x:/x", f"{tmp_path}/y:/y"])
    result = preflight_bind_sources(spec)
    # Act
    body = preflight_failure_response_body(result)
    flags = [e["exists_on_host"] for e in body["details"]["binds"]]
    # Assert
    assert flags == [False, False]


def test_failure_body_omits_host_normalized_when_no_expansion_happened(
    tmp_path: Path,
) -> None:
    # Arrange — bind source is a plain absolute path, no ~ / $VAR to
    # expand. ``host_normalized`` should be OMITTED (saves wire bytes
    # + makes "no normalisation" cleanly distinguishable from
    # "normalisation produced the same path").
    spec = _spec_with_binds([f"{tmp_path}/missing:/x"])
    result = preflight_bind_sources(spec)
    # Act
    body = preflight_failure_response_body(result)
    # Assert
    assert "host_normalized" not in body["details"]["binds"][0]


def test_failure_body_includes_host_normalized_when_expansion_changed_path(
    tmp_path: Path,
    env_save_restore,
) -> None:
    # Arrange — ``~/missing-dir`` expands to a different path on the
    # host; the entry MUST include ``host_normalized`` so the operator
    # sees what was actually stat()ed.
    env_save_restore.set("HOME", str(tmp_path))
    spec = _spec_with_binds(["~/missing-dir:/x"])
    result = preflight_bind_sources(spec)
    # Act
    body = preflight_failure_response_body(result)
    # Assert
    assert body["details"]["binds"][0]["host_normalized"] == f"{tmp_path}/missing-dir"
