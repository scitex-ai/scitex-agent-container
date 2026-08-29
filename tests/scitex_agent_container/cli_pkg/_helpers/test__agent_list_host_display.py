"""Tests for the Host + Started DISPLAY columns of ``sac agents list``.

Covers two operator display fixes (2026-07-13):

* Host column — the literal ``"local"`` sentinel is resolved to the machine's
  canonical hostname for DISPLAY (``host_display``), while the raw ``host``
  field keeps ``"local"`` for backward-compat consumers (``_is_ghost_row``).
* Started column — the raw ISO-8601 UTC stamp is rendered in the pinned
  display timezone (``YYYY-MM-DD HH:MM (JST)``); the ``--json`` path keeps the
  raw ISO untouched.

No mocks: a hand-rolled ``_FakeRegistry`` (same shape as the real one), real
``spec.yaml`` files under ``tmp_path`` exercised by the real
``load_config`` / ``validate_config``, and save/restore swaps of real module
attributes — mirroring the sibling ``test__agent_list`` conventions.
"""

from __future__ import annotations

from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import scitex_agent_container.cli_pkg._helpers._agent_list as _al
from scitex_agent_container.cli_pkg._helpers._agent_list_probe import LocalProbe
from scitex_agent_container.cli_pkg._helpers._agent_list import (
    get_agent_list_data,
    print_agent_list,
    print_agent_list_json,
)
from scitex_agent_container.cli_pkg._helpers._agent_list_host import (
    _host_display_for,
    _resolve_display_host,
)

import pytest


@pytest.fixture(autouse=True)
def _instances_store(pg_schema: str):
    """A throwaway ``instances`` store for every test in this file.

    ``instances`` moved to the shared PostgreSQL store on 2026-08-28 and the
    verbs driven here read ``list_active_instances`` on every path, so the
    dependency belongs to the VERB rather than to any one case. Autouse
    rather than per-signature for that reason, and for one more: it keeps a
    NEW test in this file from silently resolving whatever store the process
    happens to point at.
    """
    yield

_STARTED_ISO = "2026-07-12T21:36:30Z"


# ---------------------------------------------------------------------------
# Real-fake registry + save/restore swap seams (no mocks).
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """Hand-rolled stand-in for ``Registry`` — same shape as the real one."""

    def __init__(self, entries: list[dict]) -> None:
        self._entries = entries

    def list_all(self) -> list[dict]:
        return list(self._entries)


@contextmanager
def _swap_probe(impl: Callable[[Any], bool | None]) -> Iterator[None]:
    """Swap ``_probe_local`` on BOTH the module + parent-package re-export."""
    import scitex_agent_container.cli_pkg._helpers as _pkg

    saved_al = _al.probe_local_detail
    saved_pkg = getattr(_pkg, "probe_local_detail", None)
    _al.probe_local_detail = impl  # type: ignore[assignment]
    _pkg.probe_local_detail = impl  # type: ignore[assignment]
    try:
        yield
    finally:
        _al.probe_local_detail = saved_al  # type: ignore[assignment]
        if saved_pkg is None:
            delattr(_pkg, "probe_local_detail")
        else:
            _pkg.probe_local_detail = saved_pkg  # type: ignore[assignment]


def _running(value: bool | None, runtime: str = "TestRuntime"):
    """A probe callable answering ``value`` — the shape the pool consumes."""

    def impl(cfg):
        return LocalProbe(running=value, runtime=runtime, error=None)

    return impl


@contextmanager
def _swap_discover(impl: Callable[[], list[tuple[str, Path]]]) -> Iterator[None]:
    """Swap ``_al._discover_defined_agents`` for a real callable."""
    saved = _al._discover_defined_agents
    _al._discover_defined_agents = impl  # type: ignore[assignment]
    try:
        yield
    finally:
        _al._discover_defined_agents = saved  # type: ignore[assignment]


@contextmanager
def _swap_display_host(value: str) -> Iterator[None]:
    """Swap ``_al._resolve_display_host`` to a fixed hostname (real callable).

    ``get_agent_list_data`` calls it as a bare module global, so rebinding
    ``_al._resolve_display_host`` makes the resolved hostname deterministic
    without touching the machine's real ``socket.gethostname()``.
    """
    saved = _al._resolve_display_host
    _al._resolve_display_host = lambda: value  # type: ignore[assignment]
    try:
        yield
    finally:
        _al._resolve_display_host = saved  # type: ignore[assignment]


@contextmanager
def _env_set(key: str, value: str) -> Iterator[None]:
    """Set an env var for the block, restore prior value on exit."""
    saved = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


def _no_discover() -> list[tuple[str, Path]]:
    return []


def _write_valid_spec(dir_: Path) -> Path:
    """Write a minimal real v3 spec.yaml the real loader/validator accept."""
    dir_.mkdir(parents=True, exist_ok=True)
    spec = dir_ / "spec.yaml"
    spec.write_text(
        explicitize_yaml("\n".join(
            [
                "apiVersion: scitex-agent-container/v3",
                "kind: Agent",
                "metadata: {}",
                "spec:",
                "  runtime: apptainer",
                "  host: ${HOSTNAME}",
                "  workdir: /home/agent/work",
                "  apptainer:",
                "    image: /x.sif",
                "    binds: []",
                "  claude:",
                "    model: sonnet",
                "  health:",
                "    enabled: true",
                "    interval: 60",
                "  restart:",
                "    policy: on-failure",
                "    max_retries: 3",
            ]
        )
        + "\n")
    )
    return spec


# ---------------------------------------------------------------------------
# _host_display_for — pure sentinel → hostname mapping.
# ---------------------------------------------------------------------------


def test_host_display_for_local_resolves_to_hostname():
    # Arrange
    resolved = "ywata-note-win"
    # Act
    out = _host_display_for("local", resolved)
    # Assert
    assert out == "ywata-note-win"


def test_host_display_for_localhost_resolves_to_hostname():
    # Arrange
    resolved = "ywata-note-win"
    # Act
    out = _host_display_for("localhost", resolved)
    # Assert
    assert out == "ywata-note-win"


def test_host_display_for_empty_resolves_to_hostname():
    # Arrange
    resolved = "ywata-note-win"
    # Act
    out = _host_display_for("", resolved)
    # Assert
    assert out == "ywata-note-win"


def test_host_display_for_none_resolves_to_hostname():
    # Arrange
    resolved = "ywata-note-win"
    # Act
    out = _host_display_for(None, resolved)
    # Assert
    assert out == "ywata-note-win"


def test_host_display_for_concrete_host_passes_through():
    # Arrange — a real host label is forward-safe: it must NOT be overwritten.
    resolved = "ywata-note-win"
    # Act
    out = _host_display_for("spartan-bm159", resolved)
    # Assert
    assert out == "spartan-bm159"


# ---------------------------------------------------------------------------
# _resolve_display_host — tolerant, never raises, returns a real string.
# ---------------------------------------------------------------------------


def test_resolve_display_host_returns_nonempty_string():
    # Arrange
    # (no setup — reads the real machine identity via the tolerant resolver)
    # Act
    out = _resolve_display_host()
    # Assert
    assert isinstance(out, str) and out


# ---------------------------------------------------------------------------
# get_agent_list_data — host_display added, raw host kept.
# ---------------------------------------------------------------------------


def test_registered_row_carries_resolved_host_display(tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "x")
    registry = _FakeRegistry([{"name": "x", "config": str(spec), "started_at": "ts"}])
    # Act
    with _swap_discover(_no_discover), _swap_probe(_running(True)), _swap_display_host(
        "ywata-note-win"
    ):
        out = get_agent_list_data(registry)
    # Assert
    assert out[0]["host_display"] == "ywata-note-win"


def test_registered_row_keeps_raw_host_local_for_backward_compat(tmp_path):
    # Arrange — the raw ``host`` sentinel must stay "local" so _is_ghost_row and
    # any script keying on it keep working.
    spec = _write_valid_spec(tmp_path / "x")
    registry = _FakeRegistry([{"name": "x", "config": str(spec), "started_at": "ts"}])
    # Act
    with _swap_discover(_no_discover), _swap_probe(_running(True)), _swap_display_host(
        "ywata-note-win"
    ):
        out = get_agent_list_data(registry)
    # Assert
    assert out[0]["host"] == "local"


def test_defined_row_carries_resolved_host_display(tmp_path):
    # Arrange — an on-disk (defined, not registered) agent.
    spec = _write_valid_spec(tmp_path / "ondisk")
    registry = _FakeRegistry([])

    def _discover() -> list[tuple[str, Path]]:
        return [("ondisk", spec)]

    # Act
    with _swap_discover(_discover), _swap_display_host("ywata-note-win"):
        out = get_agent_list_data(registry)
    # Assert
    row = next(r for r in out if r["name"] == "ondisk")
    assert row["host_display"] == "ywata-note-win"


def test_defined_row_keeps_raw_host_local(tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "ondisk")
    registry = _FakeRegistry([])

    def _discover() -> list[tuple[str, Path]]:
        return [("ondisk", spec)]

    # Act
    with _swap_discover(_discover), _swap_display_host("ywata-note-win"):
        out = get_agent_list_data(registry)
    # Assert
    row = next(r for r in out if r["name"] == "ondisk")
    assert row["host"] == "local"


def test_json_row_exposes_both_host_and_host_display(tmp_path, capsys):
    # Arrange — --json consumers get the raw sentinel AND the resolved name.
    spec = _write_valid_spec(tmp_path / "x")
    registry = _FakeRegistry([{"name": "x", "config": str(spec)}])
    # Act
    with _swap_discover(_no_discover), _swap_probe(_running(True)), _swap_display_host(
        "ywata-note-win"
    ):
        print_agent_list_json(registry)
    # Assert
    data = json.loads(capsys.readouterr().out)
    assert data[0]["host"] == "local" and data[0]["host_display"] == "ywata-note-win"


# ---------------------------------------------------------------------------
# started_at raw ISO is preserved in the data / --json path.
# ---------------------------------------------------------------------------


def test_json_started_at_keeps_raw_iso(tmp_path, capsys):
    # Arrange — only the human table converts; --json keeps the raw ISO stamp.
    spec = _write_valid_spec(tmp_path / "x")
    registry = _FakeRegistry(
        [{"name": "x", "config": str(spec), "started_at": _STARTED_ISO}]
    )
    # Act
    with _swap_discover(_no_discover), _swap_probe(_running(True)), _swap_display_host(
        "ywata-note-win"
    ):
        print_agent_list_json(registry)
    # Assert
    data = json.loads(capsys.readouterr().out)
    assert data[0]["started_at"] == _STARTED_ISO


# ---------------------------------------------------------------------------
# print_agent_list (human table) — Host + Started rendering.
# ---------------------------------------------------------------------------


def test_print_agent_list_renders_resolved_hostname_in_host_column(tmp_path, capsys):
    # Arrange — a wide terminal so no column is squeezed out.
    spec = _write_valid_spec(tmp_path / "x")
    registry = _FakeRegistry(
        [{"name": "x", "config": str(spec), "started_at": _STARTED_ISO}]
    )
    # Act
    with _env_set("COLUMNS", "240"), _swap_discover(_no_discover), _swap_probe(_running(True)), _swap_display_host("ywata-note-win"):
        print_agent_list(registry)
    # Assert — the resolved hostname shows, not the literal "local" sentinel.
    out = capsys.readouterr().out
    assert "ywata-note-win" in out


def test_print_agent_list_started_column_renders_pinned_jst(tmp_path, capsys):
    # Arrange — pin the display tz to Asia/Tokyo and give the table full width.
    spec = _write_valid_spec(tmp_path / "x")
    registry = _FakeRegistry(
        [{"name": "x", "config": str(spec), "started_at": _STARTED_ISO}]
    )
    # Act
    with _env_set("SAC_DISPLAY_TZ", "Asia/Tokyo"), _env_set(
        "COLUMNS", "240"
    ), _swap_discover(_no_discover), _swap_probe(_running(True)), _swap_display_host(
        "ywata-note-win"
    ):
        print_agent_list(registry)
    # Assert — 21:36 UTC renders as 06:36 JST the next day; raw ISO is gone.
    out = capsys.readouterr().out
    assert "2026-07-13 06:36 (JST)" in out and _STARTED_ISO not in out
