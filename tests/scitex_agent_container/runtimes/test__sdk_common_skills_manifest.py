"""Integration tests: ``metadata.labels.skills`` → system_prompt manifest.

After the per-agent ``metadata.labels.skills`` CSV reaches the runtime
via ``AgentConfig.labels``, ``build_sdk_options`` is expected to APPEND
the SOFT manifest block (from ``_skills_manifest.build_skills_manifest_block``)
to whatever ``system_prompt`` the caller supplied. The block lets the
agent know which skills are mounted at ``$HOME/.claude/skills/`` so it
can invoke them via the ``Skill`` tool — see the module-level doctrine
note in ``_skills_manifest.py``.

These tests cover three behaviours:

  1. Non-empty labels.skills CSV → manifest appears in system_prompt.
  2. Empty / absent labels.skills → system_prompt unchanged.
  3. Idempotent — calling ``build_sdk_options`` twice on the same
     config must NOT double-append the manifest block (the lifecycle
     code paths can construct the options more than once per agent
     start, and a doubled block leaks the same advice twice).

PA-306 fixtures: no ``monkeypatch``. Each test sets env / module
attributes via the same ``_Env`` helper used by ``test__sdk_common.py``
and reverses them on teardown.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scitex_agent_container.runtimes import _sdk_common
from scitex_agent_container.runtimes._sdk_common import build_sdk_options

_SAC_KEY = _sdk_common._SAC_API_KEY_ENV


# ---------------------------------------------------------------------------
# _Env fixture (copy of the shape used in test__sdk_common.py — both
# suites mutate the same module surfaces; sharing the helper avoids
# coupling but the shape stays identical so future refactors can
# extract it into a conftest).
# ---------------------------------------------------------------------------


class _Env:
    def __init__(self) -> None:
        self._env_snapshots: dict[str, str | None] = {}
        self._attr_snapshots: list[tuple[Any, str, Any]] = []

    def setenv(self, key: str, value: str) -> None:
        if key not in self._env_snapshots:
            self._env_snapshots[key] = os.environ.get(key)
        os.environ[key] = value

    def delenv(self, key: str) -> None:
        if key not in self._env_snapshots:
            self._env_snapshots[key] = os.environ.get(key)
        os.environ.pop(key, None)

    def setattr_module(self, obj: Any, name: str, value: Any) -> None:
        if not any(a is obj and n == name for a, n, _ in self._attr_snapshots):
            self._attr_snapshots.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self) -> None:
        for key, prev in self._env_snapshots.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        for obj, name, prev in self._attr_snapshots:
            setattr(obj, name, prev)


@pytest.fixture
def sdk_env():
    env = _Env()
    try:
        yield env
    finally:
        env.restore()


def _valid_creds_json() -> str:
    expires_at_ms = int((time.time() + 86_400) * 1_000)
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "tok",
                "refreshToken": "ref",
                "expiresAt": expires_at_ms,
                "scopes": ["user:inference"],
                "subscriptionType": "max",
            }
        }
    )


def _write_valid_cred(env: _Env, cfg_dir: Path) -> Path:
    cred = cfg_dir / ".credentials.json"
    env.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text(_valid_creds_json())
    return cred


def _write_skill(home: Path, name: str, description: str) -> None:
    """Drop a minimal SKILL.md under $HOME/.claude/skills/<name>/."""
    skill_dir = home / ".claude" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: |\n  {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def _swap_registry_returning(env: _Env, entry: Any) -> None:
    import scitex_agent_container._state.registry as reg_mod

    class _FakeRegistry:
        def get(self, _name):
            return entry

    env.setattr_module(reg_mod, "Registry", _FakeRegistry)


def _swap_load_config_with_labels(
    env: _Env, workdir: str, *, labels: dict[str, str]
) -> None:
    """Swap ``config.load_config`` to return a stub config whose
    ``labels`` carries the operator-curated CSV surfaces — including the
    ``skills`` key the manifest helper consults.
    """
    import scitex_agent_container.config as cfg_mod

    claude_ns = SimpleNamespace(provider=None, account="")
    config_ns = SimpleNamespace(
        expanded_workdir=workdir,
        claude=claude_ns,
        labels=labels,
    )
    env.setattr_module(cfg_mod, "load_config", lambda _path: config_ns)


# ---------------------------------------------------------------------------
# Behaviour 1 — non-empty labels.skills CSV → manifest appended.
# ---------------------------------------------------------------------------


class TestManifestAppendedFromLabels:
    @pytest.fixture
    def _opts_with_skills_labels(self, sdk_env: _Env, tmp_path):
        # Arrange: auth via cred file under a separate cfg dir so it
        # doesn't collide with the agent's $HOME.
        _write_valid_cred(sdk_env, tmp_path / "cfg")
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        # Arrange: container $HOME with one skill mounted.
        home = tmp_path / "home" / "agent"
        (home / ".claude").mkdir(parents=True)
        _write_skill(home, "scitex-writer", "[WHAT] Writes papers.")
        sdk_env.setenv("HOME", str(home))
        # Arrange: workspace + load_config returning labels.skills.
        ws = tmp_path / "ws"
        ws.mkdir()
        _swap_registry_returning(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config_with_labels(
            sdk_env, str(ws), labels={"skills": "scitex-writer"}
        )
        # Act
        opts = build_sdk_options("alpha", system_prompt="be brief")
        return opts

    def test_system_prompt_carries_manifest_heading(
        self, _opts_with_skills_labels
    ) -> None:
        # Arrange
        opts = _opts_with_skills_labels
        # Act
        prompt = opts.system_prompt or ""
        # Assert — the LOCKED heading is the idempotency anchor.
        assert "## Available skills" in prompt

    def test_system_prompt_lists_skill_by_name(self, _opts_with_skills_labels) -> None:
        # Arrange
        opts = _opts_with_skills_labels
        # Act
        prompt = opts.system_prompt or ""
        # Assert
        assert "scitex-writer" in prompt

    def test_system_prompt_preserves_original_text(
        self, _opts_with_skills_labels
    ) -> None:
        # Arrange — the manifest must AUGMENT the original prompt, not
        # replace it.
        opts = _opts_with_skills_labels
        # Act
        prompt = opts.system_prompt or ""
        # Assert
        assert "be brief" in prompt


# ---------------------------------------------------------------------------
# Behaviour 2 — absent / empty labels.skills → system_prompt unchanged.
# ---------------------------------------------------------------------------


class TestNoManifestWithoutLabels:
    @pytest.fixture
    def _opts_no_labels_skills(self, sdk_env: _Env, tmp_path):
        # Arrange — same setup, but the labels dict has no ``skills`` key.
        _write_valid_cred(sdk_env, tmp_path / "cfg")
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        home = tmp_path / "home" / "agent"
        (home / ".claude").mkdir(parents=True)
        sdk_env.setenv("HOME", str(home))
        ws = tmp_path / "ws"
        ws.mkdir()
        _swap_registry_returning(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config_with_labels(sdk_env, str(ws), labels={"role": "writer"})
        # Act
        opts = build_sdk_options("alpha", system_prompt="be brief")
        return opts

    def test_system_prompt_has_no_manifest_heading(
        self, _opts_no_labels_skills
    ) -> None:
        # Arrange
        opts = _opts_no_labels_skills
        # Act
        prompt = opts.system_prompt or ""
        # Assert
        assert "## Available skills" not in prompt

    def test_system_prompt_is_exactly_the_input(self, _opts_no_labels_skills) -> None:
        # Arrange — strict: no manifest, no augmentation, no whitespace
        # tacked on.
        opts = _opts_no_labels_skills
        # Act
        prompt = opts.system_prompt
        # Assert
        assert prompt == "be brief"

    @pytest.fixture
    def _opts_empty_csv(self, sdk_env: _Env, tmp_path):
        # Arrange — labels.skills present but empty/whitespace-only.
        _write_valid_cred(sdk_env, tmp_path / "cfg")
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        home = tmp_path / "home" / "agent"
        (home / ".claude").mkdir(parents=True)
        sdk_env.setenv("HOME", str(home))
        ws = tmp_path / "ws"
        ws.mkdir()
        _swap_registry_returning(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config_with_labels(sdk_env, str(ws), labels={"skills": "  "})
        # Act
        opts = build_sdk_options("alpha", system_prompt="be brief")
        return opts

    def test_empty_csv_leaves_prompt_unchanged(self, _opts_empty_csv) -> None:
        # Arrange
        opts = _opts_empty_csv
        # Act
        prompt = opts.system_prompt
        # Assert
        assert prompt == "be brief"


# ---------------------------------------------------------------------------
# Behaviour 3 — idempotency.
# ---------------------------------------------------------------------------


class TestIdempotent:
    @pytest.fixture
    def _opts_called_twice(self, sdk_env: _Env, tmp_path):
        # Arrange — same setup as the first scenario.
        _write_valid_cred(sdk_env, tmp_path / "cfg")
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        home = tmp_path / "home" / "agent"
        (home / ".claude").mkdir(parents=True)
        _write_skill(home, "scitex-writer", "[WHAT] Writes papers.")
        sdk_env.setenv("HOME", str(home))
        ws = tmp_path / "ws"
        ws.mkdir()
        _swap_registry_returning(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config_with_labels(
            sdk_env, str(ws), labels={"skills": "scitex-writer"}
        )
        # Act — first call produces an augmented system_prompt; feed it
        # back into build_sdk_options as the system_prompt of a second
        # call. The lifecycle path can re-enter the option-builder more
        # than once per agent start; the manifest block must NOT double-
        # append.
        first_opts = build_sdk_options("alpha", system_prompt="be brief")
        first_prompt = first_opts.system_prompt
        second_opts = build_sdk_options("alpha", system_prompt=first_prompt)
        return first_prompt, second_opts.system_prompt

    def test_heading_appears_exactly_once(self, _opts_called_twice) -> None:
        # Arrange
        _, second_prompt = _opts_called_twice
        # Act
        n_headings = (second_prompt or "").count("## Available skills")
        # Assert — exactly one heading; a doubled block leaks the same
        # advice twice and bloats context.
        assert n_headings == 1

    def test_second_call_returns_identical_prompt(self, _opts_called_twice) -> None:
        # Arrange — re-entering build_sdk_options with an already-
        # augmented prompt should be a no-op for the manifest block.
        first_prompt, second_prompt = _opts_called_twice
        # Act / Assert
        assert second_prompt == first_prompt
