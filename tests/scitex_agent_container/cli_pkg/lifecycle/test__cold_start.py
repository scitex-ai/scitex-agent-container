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
    resolve_cold_start_targets,
)
from scitex_agent_container.cli_pkg.lifecycle._common import _iter_agent_yamls

CALLER = "scitex-laptop-01"  # retired spelling was ywata-note-win


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


# --- an agents-root is an EXISTING bulk target, never a cold-start -----------
#
# REGRESSION. `sac agents start <registry>` used to inject _iter_agent_yamls as
# the bulk-dir detector; that helper matched only <name>/<name>.yaml, so a real
# registry of <name>/spec.yaml agents read as EMPTY and fell through to
# cold-start. Measured 2026-08-27 before the fix: plans=1, action='would-create',
# label = the directory's own basename, and targets=[] -- a phantom agent
# materialised into the registry while none of the real agents started.


def _registry(tmp_path, *names, layout="spec"):
    """Build a registry-shaped dir.

    ``layout="spec"`` -> <name>/spec.yaml, what every registry writer emits.
    ``layout="self"`` -> <name>/<name>.yaml, what `sac fleet materialize` emits.
    """
    reg = tmp_path / "agents"
    reg.mkdir()
    for n in names:
        (reg / n).mkdir()
        fname = "spec.yaml" if layout == "spec" else f"{n}.yaml"
        (reg / n / fname).write_text("x")
    return reg


def _resolve(reg, base):
    """Call the resolver the way `sac agents start` actually calls it.

    _start.py injects this exact detector. Using the resolver's DEFAULT here
    instead would exercise a path production never takes -- and would pass
    whether or not the bug is present.
    """
    return resolve_cold_start_targets(
        [str(reg)],
        caller_host=CALLER,
        dry_run=True,
        base_dir=base,
        dir_has_agents=lambda p: bool(_iter_agent_yamls(p)),
    )


def test_a_spec_yaml_registry_dir_produces_no_cold_start_plan(tmp_path):
    # Arrange
    reg = _registry(tmp_path, "alpha", "beta")
    base = tmp_path / "base"
    base.mkdir()
    # Act
    _targets, plans = _resolve(reg, base)
    # Assert
    assert plans == []


def test_a_spec_yaml_registry_dir_is_passed_through_as_a_target(tmp_path):
    # Arrange
    reg = _registry(tmp_path, "alpha", "beta")
    base = tmp_path / "base"
    base.mkdir()
    # Act
    targets, _plans = _resolve(reg, base)
    # Assert -- the real agents must survive as a target, not be replaced by a label
    assert targets == [str(reg)]


def test_no_phantom_agent_dir_is_created_inside_the_registry(tmp_path):
    # Arrange
    reg = _registry(tmp_path, "alpha")
    base = tmp_path / "base"
    base.mkdir()
    # Act
    _resolve(reg, base)
    # Assert -- nothing named after the directory itself appeared
    assert not (reg / reg.name).exists()


def test_a_self_named_registry_dir_is_also_not_cold_started(tmp_path):
    # Arrange -- `sac fleet materialize` still writes <name>/<name>.yaml. The
    # first attempt at this fix DELETED the detector injection, which made this
    # layout cold-start instead: the same bug pointed the other way. Eight
    # tests in test__start.py caught it; this one states the invariant here.
    reg = _registry(tmp_path, "alpha", "beta", layout="self")
    base = tmp_path / "base"
    base.mkdir()
    # Act
    targets, plans = _resolve(reg, base)
    # Assert
    assert (plans, targets) == ([], [str(reg)])
