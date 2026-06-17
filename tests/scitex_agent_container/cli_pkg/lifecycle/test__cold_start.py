"""Tests for the `sac start` cold-start target parser (operator TODO 2026-06-17).

`sac start` accepts four convenience forms in addition to a plain agent name:

  1. ``<label>@<host>:/path/to/workdir/``   — explicit label, host, workdir
  2. ``<host>:/path/to/workdir/``           — label = basename(workdir)
  3. ``/path/to/workdir/``                  — host = caller's host
  4. ``.``                                  — workdir = $CWD, host = caller

A plain agent name (``proj-figrecipe``) is NOT a cold-start form — it resolves
through the existing registry flow, so the parser returns ``None`` for it.
Malformed forms fail loud (``ColdStartParseError``) — no silent fallback.

Conventions: one assert / AAA markers; no mocks (pure function, real inputs).
"""

from __future__ import annotations

import pytest
from scitex_agent_container.cli_pkg.lifecycle._cold_start import (
    ColdStartParseError,
    ColdStartTarget,
    parse_start_target,
)

CALLER = "ywata-note-win"


# --- plain agent name → not a cold-start form (existing flow) ----------------


def test_plain_agent_name_returns_none():
    # Arrange
    arg = "proj-figrecipe"
    # Act
    result = parse_start_target(arg, caller_host=CALLER)
    # Assert
    assert result is None


# --- form 1: <label>@<host>:/path --------------------------------------------


def test_label_host_path_form_parses_label():
    # Arrange
    arg = "fig@spartan:/home/me/proj/figrecipe"
    # Act
    t = parse_start_target(arg, caller_host=CALLER)
    # Assert
    assert t.label == "fig"


def test_label_host_path_form_parses_host():
    # Arrange
    arg = "fig@spartan:/home/me/proj/figrecipe"
    # Act
    t = parse_start_target(arg, caller_host=CALLER)
    # Assert
    assert t.host == "spartan"


def test_label_host_path_form_parses_workdir():
    # Arrange
    arg = "fig@spartan:/home/me/proj/figrecipe"
    # Act
    t = parse_start_target(arg, caller_host=CALLER)
    # Assert
    assert t.workdir == "/home/me/proj/figrecipe"


# --- form 2: <host>:/path → label = basename ---------------------------------


def test_host_path_form_derives_label_from_basename():
    # Arrange
    arg = "spartan:/home/me/proj/figrecipe"
    # Act
    t = parse_start_target(arg, caller_host=CALLER)
    # Assert
    assert t.label == "figrecipe"


def test_host_path_form_parses_host():
    # Arrange
    arg = "spartan:/home/me/proj/figrecipe/"
    # Act
    t = parse_start_target(arg, caller_host=CALLER)
    # Assert
    assert t.host == "spartan"


def test_host_path_form_strips_trailing_slash_for_basename():
    # Arrange
    arg = "spartan:/home/me/proj/figrecipe/"
    # Act
    t = parse_start_target(arg, caller_host=CALLER)
    # Assert
    assert t.label == "figrecipe"


# --- form 3: /path → caller host ---------------------------------------------


def test_absolute_path_form_uses_caller_host():
    # Arrange
    arg = "/home/me/proj/figrecipe"
    # Act
    t = parse_start_target(arg, caller_host=CALLER)
    # Assert
    assert t.host == CALLER


def test_absolute_path_form_derives_label_from_basename():
    # Arrange
    arg = "/home/me/proj/figrecipe"
    # Act
    t = parse_start_target(arg, caller_host=CALLER)
    # Assert
    assert t.label == "figrecipe"


# --- form 4: . → cwd ---------------------------------------------------------


def test_dot_form_uses_cwd_as_workdir(tmp_path):
    # Arrange
    d = tmp_path / "myproj"
    d.mkdir()
    # Act
    t = parse_start_target(".", caller_host=CALLER, cwd=str(d))
    # Assert
    assert t.workdir == str(d)


def test_dot_form_derives_label_from_cwd_basename(tmp_path):
    # Arrange
    d = tmp_path / "myproj"
    d.mkdir()
    # Act
    t = parse_start_target(".", caller_host=CALLER, cwd=str(d))
    # Assert
    assert t.label == "myproj"


# --- fail-loud (no silent fallback) ------------------------------------------


def test_at_without_host_colon_fails_loud():
    # Arrange
    arg = "fig@justlabel"
    # Act
    # Assert
    with pytest.raises(ColdStartParseError):
        parse_start_target(arg, caller_host=CALLER)


def test_empty_host_in_host_path_fails_loud():
    # Arrange
    arg = ":/home/me/proj"
    # Act
    # Assert
    with pytest.raises(ColdStartParseError):
        parse_start_target(arg, caller_host=CALLER)


def test_host_colon_with_empty_path_fails_loud():
    # Arrange
    arg = "spartan:"
    # Act
    # Assert
    with pytest.raises(ColdStartParseError):
        parse_start_target(arg, caller_host=CALLER)


def test_label_with_invalid_chars_fails_loud():
    # Arrange
    arg = "bad label@spartan:/home/me/proj"
    # Act
    # Assert
    with pytest.raises(ColdStartParseError):
        parse_start_target(arg, caller_host=CALLER)


def test_returns_cold_start_target_type():
    # Arrange
    arg = "/home/me/proj/x"
    # Act
    t = parse_start_target(arg, caller_host=CALLER)
    # Assert
    assert isinstance(t, ColdStartTarget)
