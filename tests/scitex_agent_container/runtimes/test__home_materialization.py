from __future__ import annotations

from dataclasses import asdict

import pytest

from scitex_agent_container._lifecycle._birth_certificate import compiled_spec_snapshot
from scitex_agent_container.config._to_home_spec import parse_to_home
from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._home_materialization import (
    HomeMaterializationError,
    resolve_home_imports,
)


def _config(tmp_path, layers):
    spec = tmp_path / "agent" / "spec.yaml"
    spec.parent.mkdir(exist_ok=True)
    spec.write_text("test")
    return AgentConfig(
        name="agent", config_path=str(spec), to_home=parse_to_home({"imports": layers})
    )


def _layer(layer_id, source, *, conflict="error", required=True, target=".", precedence=10):
    return {
        "id": layer_id,
        "source": source,
        "precedence": precedence,
        "apply": "pre-launch-on-start-and-restart",
        "destination": target,
        "mode": "managed-overlay-v1",
        "conflict": conflict,
        "stale": "preserve",
        "required": required,
    }


def test_resolves_absolute_provenance_digest_and_order(tmp_path):
    shared = tmp_path / "agent" / "shared"
    local = tmp_path / "agent" / "local"
    shared.mkdir(parents=True)
    local.mkdir()
    (shared / "a").write_text("one")
    (local / "b").write_text("two")
    config = _config(
        tmp_path,
        [_layer("shared", "shared"), _layer("local", "local", precedence=20)],
    )

    resolved = resolve_home_imports(config)

    assert [item.id for item in resolved] == ["shared", "local"]
    assert resolved[0].source == str(shared.resolve())
    assert resolved[0].content_digest.startswith("sha256:")
    assert config.resolved_to_home_imports == [asdict(item) for item in resolved]


def test_digest_changes_with_content(tmp_path):
    source = tmp_path / "agent" / "home"
    source.mkdir(parents=True)
    item = source / "state.md"
    item.write_text("one")
    config = _config(tmp_path, [_layer("agent", "home")])
    before = resolve_home_imports(config)[0].content_digest
    item.write_text("two")
    after = resolve_home_imports(config)[0].content_digest
    assert before != after


def test_digest_tracks_dereferenced_symlink_content(tmp_path):
    source = tmp_path / "agent" / "home"
    source.mkdir(parents=True)
    target = tmp_path / "content"
    target.write_text("one")
    (source / "linked").symlink_to(target)
    config = _config(tmp_path, [_layer("agent", "home")])
    before = resolve_home_imports(config)[0].content_digest
    target.write_text("two")
    after = resolve_home_imports(config)[0].content_digest
    assert before != after


def test_missing_required_fails_and_optional_is_enumerated(tmp_path):
    with pytest.raises(HomeMaterializationError, match="required.*missing"):
        resolve_home_imports(_config(tmp_path, [_layer("required", "missing")]))
    resolved = resolve_home_imports(
        _config(tmp_path, [_layer("optional", "missing", required=False)])
    )
    assert resolved[0].status == "missing"
    assert resolved[0].content_digest is None


def test_collision_requires_explicit_replace(tmp_path):
    for name, text in (("low", "one"), ("high", "two")):
        source = tmp_path / "agent" / name
        source.mkdir(parents=True)
        (source / "same").write_text(text)
    with pytest.raises(HomeMaterializationError, match="conflicts at same"):
        resolve_home_imports(
            _config(tmp_path, [_layer("low", "low"), _layer("high", "high", precedence=20)])
        )
    resolved = resolve_home_imports(
        _config(
            tmp_path,
            [_layer("low", "low"), _layer("high", "high", conflict="higher-precedence-wins", precedence=20)],
        )
    )
    assert [item.id for item in resolved] == ["low", "high"]


def test_compiled_incarnation_enumerates_resolved_source_and_digest(tmp_path):
    source = tmp_path / "agent" / "home"
    source.mkdir(parents=True)
    (source / "identity.md").write_text("agent")
    config = _config(tmp_path, [_layer("per-agent", "home")])
    resolve_home_imports(config)

    recorded = compiled_spec_snapshot(config)["resolved_to_home_imports"]

    assert recorded[0]["source"] == str(source.resolve())
    assert recorded[0]["content_digest"].startswith("sha256:")
    assert recorded[0]["apply"] == "pre-launch-on-start-and-restart"
