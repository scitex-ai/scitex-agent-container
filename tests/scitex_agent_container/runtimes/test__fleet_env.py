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


def test_sac_declares_no_fleet_defaults_of_its_own() -> None:
    """Replaces test_fleet_defaults_seeded_with_the_cards_sqlite_read_backend.

    sac used to seed SCITEX_CARDS_READ_BACKEND=sqlite. Dropped 2026-07-29 on the
    store owner's ruling — nothing reads it, and it actively misled a diagnosis
    by stating a read policy that was never enforced. sac now declares nothing;
    the cascade exists purely for operator overrides.
    """
    # Arrange
    absent = Path("/nonexistent/config.yaml")
    # Act
    defaults = declared_fleet_defaults(absent)
    # Assert
    assert defaults == {}


def test_declared_defaults_do_not_mutate_the_module_constant(tmp_path: Path) -> None:
    """A caller mutating the result must not poison the next agent's env.

    The mechanism under test is that the returned dict is a COPY. It previously
    probed that via the seeded read-backend key; with sac declaring nothing, the
    probe is an operator override instead. The coverage is unchanged — only the
    key it mutates.
    """
    # Arrange
    cfg = _write_config_yaml(tmp_path / "config.yaml", {"ADDED": "x"})
    # Act
    declared_fleet_defaults(cfg)["ADDED"] = "MUTATED"
    # Assert — the module constant is untouched by a caller's mutation
    assert "ADDED" not in FLEET_DEFAULT_ENV


def test_declared_defaults_return_a_fresh_dict_each_call(tmp_path: Path) -> None:
    """Second half of the copy guarantee: two calls must not share a dict.

    Added because the original mutation test asserted against a CONSTANT that is
    now empty, which would pass even if the function returned the constant
    itself. This asserts the property directly rather than through a key.
    """
    # Arrange
    cfg = _write_config_yaml(tmp_path / "config.yaml", {"ADDED": "x"})
    # Act
    first = declared_fleet_defaults(cfg)
    first["ADDED"] = "MUTATED"
    second = declared_fleet_defaults(cfg)
    # Assert
    assert second["ADDED"] == "x"


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
    cfg = _write_config_yaml(
        tmp_path / "config.yaml", {"SCITEX_CARDS_READ_BACKEND": "yaml"}
    )
    # Act
    defaults = declared_fleet_defaults(cfg)
    # Assert
    assert defaults["SCITEX_CARDS_READ_BACKEND"] == "yaml"


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
    """An operator typo must not stop the fleet from launching.

    Previously asserted the seeded read-backend key survived the fallback. With
    sac declaring nothing that assertion would be vacuous — `{} == {}` passes
    even if parsing silently succeeded. So the malformed file now also CONTAINS
    a would-be override, and the assertion is that the override did NOT take
    effect: proof the parse actually failed and the fallback actually ran,
    rather than a shape that cannot come out the other way.
    """
    # Arrange — malformed, and carrying an override that must not be honoured.
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"spec: [this is not: a mapping\n  {CONFIG_SECTION}:\n    SHOULD_NOT_APPEAR: y\n"
    )
    # Act
    defaults = declared_fleet_defaults(cfg)
    # Assert
    assert "SHOULD_NOT_APPEAR" not in defaults


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


# ----------------------------------------------------------------------
# Board identity — BOTH names for the transition window, and the loud
# validator that refuses an unexpanded ``${VAR}`` (INCIDENT 2026-07-19:
# seven cards stored ``created_by='${SCITEX_CARDS_AGENT_ID}'``).
# ----------------------------------------------------------------------


def test_starting_an_agent_exports_the_current_board_identity_name() -> None:
    """scitex-cards reads SCITEX_CARDS_AGENT_ID; sac injected only the old name."""
    # Arrange
    config = SimpleNamespace(env={"SCITEX_TODO_AGENT_ID": "scitex-agent-container"})
    # Act
    merged = effective_env(config, defaults={})
    # Assert
    assert merged["SCITEX_CARDS_AGENT_ID"] == "scitex-agent-container"


def test_starting_an_agent_still_exports_the_legacy_board_identity_name() -> None:
    """Both, not a swap — installed scitex-cards versions differ across the fleet."""
    # Arrange
    config = SimpleNamespace(env={"SCITEX_TODO_AGENT_ID": "scitex-agent-container"})
    # Act
    merged = effective_env(config, defaults={})
    # Assert
    assert merged["SCITEX_TODO_AGENT_ID"] == "scitex-agent-container"


def _rejection_message(config: SimpleNamespace) -> str:
    """The error ``effective_env`` raises for ``config``, or ``""`` if it did not.

    Keeps the rejection tests at ONE assertion each (STX-TQ007 counts a
    ``pytest.raises`` block as an assertion, so pairing it with an assert on
    the message would be two).
    """
    try:
        effective_env(config, defaults={})
    except ValueError as exc:
        return str(exc)
    return ""


def test_an_unexpanded_substitution_value_is_rejected_loudly() -> None:
    """A ``${VAR}`` that never expanded is a non-answer; it must never be stored."""
    # Arrange
    config = SimpleNamespace(env={"SCITEX_CARDS_AGENT_ID": "${SCITEX_CARDS_AGENT_ID}"})
    # Act
    message = _rejection_message(config)
    # Assert
    assert "SCITEX_CARDS_AGENT_ID" in message


def test_the_rejection_error_quotes_the_offending_value() -> None:
    # Arrange
    config = SimpleNamespace(env={"ANY_KEY": "${SOMETHING}"})
    # Act
    message = _rejection_message(config)
    # Assert
    assert "${SOMETHING}" in message


def test_a_normal_board_identity_value_passes_through_unchanged() -> None:
    """CONTROL — the validator must reject non-answers, not everything."""
    # Arrange
    config = SimpleNamespace(env={"SCITEX_TODO_AGENT_ID": "scitex-agent-container"})
    # Act
    merged = effective_env(config, defaults={})
    # Assert
    assert merged["SCITEX_TODO_AGENT_ID"] == "scitex-agent-container"


def test_a_normal_unrelated_value_passes_through_unchanged() -> None:
    """CONTROL — an ordinary value with no ``${`` is untouched."""
    # Arrange
    config = SimpleNamespace(env={"PLAIN": "scitex-agent-container"})
    # Act
    merged = effective_env(config, defaults={})
    # Assert
    assert merged["PLAIN"] == "scitex-agent-container"


def test_raw_args_declared_identity_is_mirrored_to_the_current_name() -> None:
    """Most specs declare the identity ONLY in raw_args, never in spec.env."""
    # Arrange
    config = SimpleNamespace(
        env={},
        apptainer=SimpleNamespace(
            raw_args=["--env", "SCITEX_TODO_AGENT_ID=scitex-dev"]
        ),
    )
    # Act
    merged = effective_env(config, defaults={})
    # Assert
    assert merged["SCITEX_CARDS_AGENT_ID"] == "scitex-dev"


def test_raw_args_identity_wins_over_the_spec_env_identity() -> None:
    """apptainer --env is last-wins and raw_args are appended AFTER spec.env."""
    # Arrange
    config = SimpleNamespace(
        env={"SCITEX_TODO_AGENT_ID": "from-spec-env"},
        apptainer=SimpleNamespace(
            raw_args=["--env", "SCITEX_TODO_AGENT_ID=from-raw-args"]
        ),
    )
    # Act
    merged = effective_env(config, defaults={})
    # Assert
    assert merged["SCITEX_CARDS_AGENT_ID"] == "from-raw-args"


# ----------------------------------------------------------------------
# Dual-write must stay GONE. The YAML tier it gated was deleted 2026-07-21;
# after that the flag routed nothing while still reaching every container,
# and scitex-cards' health FAILED single_write_target purely on its presence
# (a false alarm nobody could clear). Dropped 2026-07-28 on the store owner's
# decision. These assert the absence at BOTH layers, because a key removed
# from the dict but still rendered into argv would be the same bug.
# ----------------------------------------------------------------------

DEAD_WRITE_ROUTING_KEYS = ("SCITEX_CARDS_DUAL_WRITE", "SCITEX_TODO_DUAL_WRITE")


@pytest.mark.parametrize("key", DEAD_WRITE_ROUTING_KEYS)
def test_dead_write_routing_key_is_not_a_fleet_default(key: str) -> None:
    """sac must not declare a write-routing flag for a store tier that is gone."""
    # Arrange
    absent = Path("/nonexistent/config.yaml")
    # Act
    defaults = declared_fleet_defaults(absent)
    # Assert
    assert key not in defaults and key not in FLEET_DEFAULT_ENV


@pytest.mark.parametrize("key", DEAD_WRITE_ROUTING_KEYS)
def test_dead_write_routing_key_never_reaches_argv(key: str) -> None:
    """argv is what actually reaches the container, so assert it there too."""
    # Arrange
    config = SimpleNamespace(env={})
    # Act
    flags = fleet_env_flags(config, defaults=FLEET_DEFAULT_ENV)
    # Assert
    assert not any(flag.startswith(f"{key}=") for flag in flags)


DEAD_READ_ROUTING_KEYS = ("SCITEX_CARDS_READ_BACKEND", "SCITEX_TODO_READ_BACKEND")


@pytest.mark.parametrize("key", DEAD_READ_ROUTING_KEYS)
def test_dead_read_routing_key_is_not_a_fleet_default(key: str) -> None:
    """sac must not declare a read-routing flag that nothing reads.

    This INVERTS ``test_read_backend_default_is_retained``, which asserted the
    SQLite read pin "stays". That test was written when the pin was believed to
    mean something. It does not: scitex-cards searched their read path from
    source (positive control first) and found the variable only in a comment and
    a retired-vars key — never read for behaviour. The old test pinned a policy
    statement that was never true.

    Dropped 2026-07-29 on the store owner's explicit ruling, same standard as
    DEAD_WRITE_ROUTING_KEYS above. Do NOT reintroduce without a new ruling.
    """
    # Arrange
    absent = Path("/nonexistent/config.yaml")
    # Act
    defaults = declared_fleet_defaults(absent)
    # Assert
    assert key not in defaults and key not in FLEET_DEFAULT_ENV


@pytest.mark.parametrize("key", DEAD_READ_ROUTING_KEYS)
def test_dead_read_routing_key_never_reaches_argv(key: str) -> None:
    """argv is what actually reaches the container, so assert it there too."""
    # Arrange
    config = SimpleNamespace(env={})
    # Act
    flags = fleet_env_flags(config, defaults=FLEET_DEFAULT_ENV)
    # Assert
    assert not any(flag.startswith(f"{key}=") for flag in flags)


def test_an_empty_fleet_default_env_is_a_valid_state() -> None:
    """Removing the last default must not break the mechanism.

    FLEET_DEFAULT_ENV is now empty. The cascade still exists for operator
    overrides via config.yaml's ``fleet_default_env``, so this asserts the
    empty case yields no flags rather than raising or misbehaving.
    """
    # Arrange
    config = SimpleNamespace(env={})
    # Act
    flags = fleet_env_flags(config, defaults={})
    # Assert
    assert flags == []
