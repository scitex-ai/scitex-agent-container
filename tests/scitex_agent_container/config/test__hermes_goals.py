"""Authored Hermes budgets and judge intent survive the real configuration boundary."""

from dataclasses import replace

import pytest
import yaml

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._engine_types import parse_engine_entry
from scitex_agent_container.config._hermes_goals import (
    HermesGoalSpec,
    parse_selected_hermes_goals,
    parse_selected_hermes_max_turns,
    validate_hermes_goal_engine,
)
from scitex_agent_container.config._schema_compat import canonical_surface_errors
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc


def _spec(entry=None):
    return {
        "harness": "hermes",
        "comms": {"channels": []},
        "available_harnesses": {"hermes": {} if entry is None else entry},
    }


def test_explicit_turn_controls_retain_integer_and_judge_intent():
    # Arrange
    spec = _spec(
        {"max_turns": 99999, "goals": {"max_turns": 99999, "judge_engine": "muse"}}
    )
    # Act
    observed = parse_selected_hermes_max_turns(spec), parse_selected_hermes_goals(spec)
    # Assert
    assert observed == (99999, HermesGoalSpec(99999, "muse"))


def test_omitted_turn_controls_preserve_existing_defaults():
    # Arrange
    spec = _spec()
    # Act
    observed = parse_selected_hermes_max_turns(spec), parse_selected_hermes_goals(spec)
    # Assert
    assert observed == (None, HermesGoalSpec())


@pytest.mark.parametrize("field", ["max_turns", "goals"])
@pytest.mark.parametrize("value", [None, True, False, 1.5, "99999", 0, -1])
def test_explicit_turn_limit_rejects_invalid_scalar(field, value):
    # Arrange
    entry = {field: {"max_turns": value} if field == "goals" else value}
    # Act / Assert
    with pytest.raises(ValueError, match="positive integer"):
        parse_selected_hermes_goals(_spec(entry))


@pytest.mark.parametrize(
    "goals",
    [None, [], {"model": "invented"}, {"judge_engine": ""}, {"judge_engine": True}],
)
def test_goal_policy_rejects_invalid_shape_or_unknown_field(goals):
    # Arrange
    spec = _spec({"goals": goals})
    # Act / Assert
    with pytest.raises(ValueError):
        parse_selected_hermes_goals(spec)


def test_canonical_validator_accepts_explicit_hermes_turn_policy():
    # Arrange
    entry = {
        "session": {"mode": "continue", "max_age_minutes": None},
        "max_turns": 99999,
        "goals": {"max_turns": 99999, "judge_engine": "muse"},
    }
    # Act
    errors = canonical_surface_errors({"spec": _spec(entry)})
    # Assert
    assert errors == []


def test_canonical_validator_surfaces_invalid_goal_limit():
    # Arrange
    entry = {
        "session": {"mode": "continue", "max_age_minutes": None},
        "goals": {"max_turns": True},
    }
    # Act
    errors = canonical_surface_errors({"spec": _spec(entry)})
    # Assert
    assert errors == [
        "spec.available_harnesses.hermes.goals.max_turns must be a positive integer"
    ]


def _config():
    engine = parse_engine_entry("muse", {"model": "meta/muse-spark-1.3-contributor"})
    return AgentConfig(
        name="lead",
        harness="hermes",
        engines={"muse": engine},
        engine_key="muse",
        model=engine.model,
        hermes_goals=HermesGoalSpec(99999, "muse"),
    )


def test_declared_same_engine_judge_preserves_meta_model_identity():
    # Arrange
    config = _config()
    # Act
    outcome = validate_hermes_goal_engine(config)
    # Assert
    assert outcome is None


@pytest.mark.parametrize(
    "judge,model,message",
    [
        ("unknown", "meta/muse-spark-1.3-contributor", "not a declared engine"),
        ("other", "meta/muse-spark-1.3-contributor", "selected agent engine"),
        ("muse", "different-model", "same model"),
    ],
)
def test_judge_route_refuses_unknown_engine_or_changed_model(judge, model, message):
    # Arrange
    config = _config()
    config.engines["other"] = parse_engine_entry("other", {"model": "different-model"})
    config.hermes_goals = replace(config.hermes_goals, judge_engine=judge)
    config.model = model
    # Act / Assert
    with pytest.raises(ValueError, match=message):
        validate_hermes_goal_engine(config)


@pytest.mark.parametrize("judge", ["muse", "unknown", "other"])
def test_authored_goal_intent_crosses_validated_yaml_loader(
    tmp_path, env_save_restore, judge
):
    # Arrange
    from scitex_agent_container.config import load_config

    env_save_restore.set("SAC_ENGINES_FILE", str(tmp_path / "no-fleet-engines.yaml"))
    raw = explicit_doc(
        {
            "runtime": "tui",
            "harness": "hermes",
            "engine": "muse",
            "comms": {"channels": []},
            "available_harnesses": {
                "hermes": {
                    "session": {"mode": "continue", "max_age_minutes": None},
                    "max_turns": 99999,
                    "goals": {"max_turns": 99999, "judge_engine": judge},
                }
            },
            "available_engines": {
                key: {
                    "model": model,
                    "provider": {
                        "base_url": "http://synthetic.invalid/v1",
                        "auth_token_env": "SYNTHETIC_KEY",
                    },
                }
                for key, model in (
                    ("muse", "muse-spark-1.3-contributor"),
                    ("other", "other-model"),
                )
            },
        }
    )
    for legacy in ("engines", "claude", "watchdog", "container"):
        raw["spec"].pop(legacy, None)
    path = tmp_path / "lead" / "spec.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(raw))
    # Act / Assert
    if judge == "muse":
        config = load_config(path)
        assert (config.hermes_max_turns, config.hermes_goals, config.model) == (
            99999,
            HermesGoalSpec(99999, "muse"),
            "muse-spark-1.3-contributor",
        )
    else:
        with pytest.raises(ValueError, match="judge_engine"):
            load_config(path)
