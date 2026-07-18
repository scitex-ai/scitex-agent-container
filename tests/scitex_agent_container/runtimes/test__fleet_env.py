"""Fleet-default env layer — precedence, data-purity and the config.yaml layer.

Mirrors ``src/scitex_agent_container/runtimes/_fleet_env.py`` (PS-204 §2).

The load-bearing property is PRECEDENCE: a fleet default must reach an agent
that says nothing, and must LOSE to an agent that says something. Both
directions are asserted, and the override direction is additionally proven at
the argv level (``test_spec_env_overrides_fleet_default_in_argv``) because argv
is what actually reaches the container — a merge that is correct in a dict and
wrong in the rendered flags would still be a broken feature.

Real YAML files on ``tmp_path`` and a real ``AgentConfig`` via ``load_config``
— no mocks (PA-306).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scitex_agent_container.runtimes._fleet_env import (
    CONFIG_SECTION,
    FLEET_DEFAULT_ENV,
    declared_fleet_defaults,
    effective_env,
    fleet_env_flags,
    merge_fleet_env,
)


def _write_config_yaml(path: Path, mapping: dict) -> Path:
    """A real ``config.yaml`` carrying ``spec.fleet_default_env``."""
    import yaml

    path.write_text(yaml.safe_dump({"spec": {CONFIG_SECTION: mapping}}))
    return path


# ----------------------------------------------------------------------
# The data layer.
# ----------------------------------------------------------------------


def test_fleet_defaults_seeded_with_the_cards_dual_write_flag() -> None:
    # Arrange
    absent = Path("/nonexistent/config.yaml")
    # Act
    defaults = declared_fleet_defaults(absent)
    # Assert
    assert defaults["SCITEX_CARDS_DUAL_WRITE"] == "1"


def test_fleet_defaults_seeded_with_the_cards_sqlite_read_backend() -> None:
    # Arrange
    absent = Path("/nonexistent/config.yaml")
    # Act
    defaults = declared_fleet_defaults(absent)
    # Assert
    assert defaults["SCITEX_CARDS_READ_BACKEND"] == "sqlite"


def test_declared_defaults_do_not_mutate_the_module_constant(tmp_path: Path) -> None:
    """A caller mutating the result must not poison the next agent's env."""
    # Arrange
    cfg = _write_config_yaml(tmp_path / "config.yaml", {"ADDED": "x"})
    # Act
    declared_fleet_defaults(cfg)["SCITEX_CARDS_DUAL_WRITE"] = "MUTATED"
    # Assert
    assert FLEET_DEFAULT_ENV["SCITEX_CARDS_DUAL_WRITE"] == "1"


# ----------------------------------------------------------------------
# Layer 2 — the operator's config.yaml.
# ----------------------------------------------------------------------


def test_config_yaml_can_add_a_new_fleet_default(tmp_path: Path) -> None:
    # Arrange
    cfg = _write_config_yaml(tmp_path / "config.yaml", {"OPERATOR_KEY": "yes"})
    # Act
    defaults = declared_fleet_defaults(cfg)
    # Assert
    assert defaults["OPERATOR_KEY"] == "yes"


def test_config_yaml_overrides_a_sac_declared_default(tmp_path: Path) -> None:
    # Arrange
    cfg = _write_config_yaml(tmp_path / "config.yaml", {"SCITEX_CARDS_DUAL_WRITE": "0"})
    # Act
    defaults = declared_fleet_defaults(cfg)
    # Assert
    assert defaults["SCITEX_CARDS_DUAL_WRITE"] == "0"


def test_config_yaml_values_are_coerced_to_strings(tmp_path: Path) -> None:
    """A YAML ``true`` must render as a well-formed --env value, not a repr."""
    # Arrange
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"spec:\n  {CONFIG_SECTION}:\n    FLAG: true\n    COUNT: 3\n")
    # Act
    defaults = declared_fleet_defaults(cfg)
    # Assert
    assert (defaults["FLAG"], defaults["COUNT"]) == ("True", "3")


def test_malformed_config_yaml_degrades_to_sac_defaults(tmp_path: Path) -> None:
    """An operator typo must not stop the fleet from launching."""
    # Arrange
    cfg = tmp_path / "config.yaml"
    cfg.write_text("spec: [this is not: a mapping\n")
    # Act
    defaults = declared_fleet_defaults(cfg)
    # Assert
    assert defaults["SCITEX_CARDS_DUAL_WRITE"] == "1"


def test_non_mapping_section_is_ignored(tmp_path: Path) -> None:
    # Arrange
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"spec:\n  {CONFIG_SECTION}:\n    - not\n    - a mapping\n")
    # Act
    defaults = declared_fleet_defaults(cfg)
    # Assert
    assert defaults == dict(FLEET_DEFAULT_ENV)


# ----------------------------------------------------------------------
# THE precedence rule: spec.env wins.
# ----------------------------------------------------------------------


def test_fleet_default_reaches_an_agent_that_declares_nothing() -> None:
    # Arrange
    defaults = {"FLEET_ONLY": "from-fleet"}
    # Act
    merged = merge_fleet_env({}, defaults=defaults)
    # Assert
    assert merged["FLEET_ONLY"] == "from-fleet"


def test_spec_env_overrides_a_fleet_default_of_the_same_name() -> None:
    """THE precedence rule — per-agent beats fleet default."""
    # Arrange
    defaults = {"SHARED_KEY": "from-fleet"}
    # Act
    merged = merge_fleet_env({"SHARED_KEY": "from-spec"}, defaults=defaults)
    # Assert
    assert merged["SHARED_KEY"] == "from-spec"


def test_spec_env_can_neutralise_a_fleet_default_with_an_empty_value() -> None:
    """The documented per-agent opt-out: same key, empty value."""
    # Arrange
    defaults = {"SHARED_KEY": "from-fleet"}
    # Act
    merged = merge_fleet_env({"SHARED_KEY": ""}, defaults=defaults)
    # Assert
    assert merged["SHARED_KEY"] == ""


def test_disjoint_spec_and_fleet_keys_both_survive() -> None:
    # Arrange
    defaults = {"FLEET_KEY": "f"}
    # Act
    merged = merge_fleet_env({"SPEC_KEY": "s"}, defaults=defaults)
    # Assert
    assert (merged["FLEET_KEY"], merged["SPEC_KEY"]) == ("f", "s")


def test_a_same_key_collision_does_not_raise() -> None:
    """Unlike the to_home cascade, a default exists in order to be overridden."""
    # Arrange
    defaults = {"SHARED_KEY": "from-fleet"}
    # Act
    merged = merge_fleet_env({"SHARED_KEY": "other"}, defaults=defaults)
    # Assert
    assert merged["SHARED_KEY"] == "other"


def test_merge_does_not_mutate_the_supplied_defaults() -> None:
    # Arrange
    defaults = {"SHARED_KEY": "from-fleet"}
    # Act
    merge_fleet_env({"SHARED_KEY": "from-spec"}, defaults=defaults)
    # Assert
    assert defaults["SHARED_KEY"] == "from-fleet"


def test_merge_is_idempotent() -> None:
    # Arrange
    defaults = {"A": "1"}
    once = merge_fleet_env({"B": "2"}, defaults=defaults)
    # Act
    twice = merge_fleet_env(once, defaults=defaults)
    # Assert
    assert twice == once


def test_none_spec_env_yields_the_fleet_defaults() -> None:
    """``spec.env`` is optional; a spec without one still gets the defaults."""
    # Arrange
    defaults = {"FLEET_ONLY": "v"}
    # Act
    merged = merge_fleet_env(None, defaults=defaults)
    # Assert
    assert merged == {"FLEET_ONLY": "v"}


# ----------------------------------------------------------------------
# The build_run_argv entry-points.
# ----------------------------------------------------------------------


def test_effective_env_reads_config_env() -> None:
    # Arrange
    config = SimpleNamespace(env={"SHARED_KEY": "from-spec"})
    # Act
    merged = effective_env(config, defaults={"SHARED_KEY": "from-fleet"})
    # Assert
    assert merged["SHARED_KEY"] == "from-spec"


def test_effective_env_tolerates_a_config_without_env() -> None:
    # Arrange
    config = SimpleNamespace()
    # Act
    merged = effective_env(config, defaults={"FLEET_ONLY": "v"})
    # Assert
    assert merged == {"FLEET_ONLY": "v"}


def test_fleet_env_flags_render_apptainer_env_pairs() -> None:
    # Arrange
    config = SimpleNamespace(env={})
    # Act
    flags = fleet_env_flags(config, defaults={"K": "V"})
    # Assert
    assert flags == ["--env", "K=V"]


@pytest.mark.parametrize("value", ["with space", "a=b", ""])
def test_flag_value_is_rendered_verbatim(value: str) -> None:
    """--env values are passed as a single argv element, never re-quoted."""
    # Arrange
    config = SimpleNamespace(env={"K": value})
    # Act
    flags = fleet_env_flags(config, defaults={})
    # Assert
    assert flags[1] == f"K={value}"
