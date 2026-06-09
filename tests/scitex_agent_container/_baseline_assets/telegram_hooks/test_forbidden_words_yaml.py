"""Pin the disowning-phrase entries in the version-controlled
forbidden-words YAML configs (lead directive 2026-06-09).

The forbidden_words.sh hook (canonical schema + loader owned by
scitex-dev, materialised into agent + lead containers via the
dotfile baseline) reads YAML configs at:

* ``~/.scitex/dev/config/forbidden-words.yaml`` (global)
* ``<cwd>/.scitex/dev/config/forbidden-words.yaml`` (project-specific)

This repo ships TWO copies of the same data so the hook fires in
both contexts:

1. ``.scitex/dev/config/forbidden-words.yaml`` — picked up by the
   hook when an agent runs with ``cwd=/work`` (the project-local
   path the loader checks second).
2. ``src/scitex_agent_container/_baseline_assets/telegram_hooks/forbidden-words.yaml``
   — the canonical, version-controlled deployable. Operators copy
   or symlink THIS file into ``~/.scitex/dev/config/forbidden-words.yaml``
   on every host so the hook fires for cross-host sessions too.

These tests pin: (a) both YAMLs load cleanly, (b) both contain the
THREE lead-mandated disowning phrases (`既存の問題`, `関係ない`,
`無関係`), and (c) every entry carries the schema-required
``word``/``pattern`` + ``reason`` + ``use_instead`` keys so the
hook's nudge message is informative on a hit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PROJECT_YAML = _REPO_ROOT / ".scitex" / "dev" / "config" / "forbidden-words.yaml"
_BASELINE_YAML = (
    _REPO_ROOT
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "telegram_hooks"
    / "forbidden-words.yaml"
)

# Disowning phrases — three required entries. The "関係" form is a
# REGEX (operator 2026-06-09: covers both 関係ない and 関係無い).
# The other two are literal words.
_REQUIRED_DISOWNING_WORDS = ("既存の問題", "無関係")
_REQUIRED_DISOWNING_PATTERNS = ("関係(ない|無い)",)
# Operator 2026-06-09 (comprehensive): every katakana-jargon entry has
# an English equivalent ALSO banned. The katakana entries are literal
# words (Japanese has no word boundaries) and the English ones are
# regex patterns with ``(?i)\b...\b`` so case-insensitive word-bounded
# matches catch "fallback", "Fallback", "FALLBACK" alike.
_REQUIRED_KATAKANA_JARGON_WORDS = (
    "ナッジ",
    "なっじ",
    "ステイル",
    "フォールバック",
    "ロールバック",
    "ウェッジ",
)
_REQUIRED_ENGLISH_EQUIVALENT_PATTERNS = (
    r"(?i)\bnudge\b",
    r"(?i)\bstale\b",
    r"(?i)\bfallback\b",
    r"(?i)\brollback\b",
    r"(?i)\bwedge\b",
)


def _load(path: Path) -> dict:
    assert path.is_file(), f"forbidden-words config missing: {path}"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _entries(data: dict) -> list[dict]:
    return [e for e in (data.get("forbidden_word") or []) if isinstance(e, dict)]


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(_PROJECT_YAML, id="project-cwd"),
        pytest.param(_BASELINE_YAML, id="baseline-deployable"),
    ],
)
class TestForbiddenWordsYaml:
    def test_yaml_loads_cleanly(self, path: Path) -> None:
        # Arrange / Act
        data = _load(path)
        # Assert
        assert isinstance(data.get("forbidden_word"), list)

    def test_carries_all_required_disowning_words(self, path: Path) -> None:
        # Arrange — literal-word disowning phrases the lead mandated 2026-06-09.
        data = _load(path)
        words = {e.get("word") for e in _entries(data)}
        # Act
        missing = [w for w in _REQUIRED_DISOWNING_WORDS if w not in words]
        # Assert
        assert missing == []

    def test_carries_all_required_disowning_patterns(self, path: Path) -> None:
        # Arrange — regex disowning pattern (関係(ない|無い)) the lead mandated 2026-06-09.
        data = _load(path)
        patterns = {e.get("pattern") for e in _entries(data)}
        # Act
        missing = [p for p in _REQUIRED_DISOWNING_PATTERNS if p not in patterns]
        # Assert
        assert missing == []

    def test_carries_all_required_katakana_jargon_words(self, path: Path) -> None:
        # Arrange — katakana-jargon literal-word entries the operator
        # mandated 2026-06-09 (comprehensive update).
        data = _load(path)
        words = {e.get("word") for e in _entries(data)}
        # Act
        missing = [w for w in _REQUIRED_KATAKANA_JARGON_WORDS if w not in words]
        # Assert
        assert missing == []

    def test_carries_all_required_english_equivalent_patterns(self, path: Path) -> None:
        # Arrange — English equivalents of every katakana-jargon entry,
        # banned via case-insensitive word-bounded regex (operator 2026-06-09).
        data = _load(path)
        patterns = {e.get("pattern") for e in _entries(data)}
        # Act
        missing = [
            p for p in _REQUIRED_ENGLISH_EQUIVALENT_PATTERNS if p not in patterns
        ]
        # Assert
        assert missing == []

    def test_every_entry_has_required_schema_fields(self, path: Path) -> None:
        # Arrange — the hook's nudge message names ``reason`` +
        # ``use_instead`` for every blocked word; missing either field
        # would surface an empty/None in the operator's view.
        data = _load(path)
        # Act
        bad = [
            e
            for e in _entries(data)
            if not (e.get("word") or e.get("pattern"))
            or not e.get("reason")
            or not e.get("use_instead")
        ]
        # Assert
        assert bad == [], f"entries missing required keys: {bad!r}"


def _word_set(path: Path) -> set:
    return {e.get("word") for e in _entries(_load(path)) if e.get("word")}


def _pattern_set(path: Path) -> set:
    return {e.get("pattern") for e in _entries(_load(path)) if e.get("pattern")}


def test_project_and_baseline_carry_identical_disowning_word_set() -> None:
    """Drift guard: the project-local config + the canonical
    deployable must stay in sync on the disowning literal-word set
    so a project-local override never silently drops a phrase from
    the fleet-wide ban list.
    """
    # Arrange
    proj = _word_set(_PROJECT_YAML)
    baseline = _word_set(_BASELINE_YAML)
    required = set(_REQUIRED_DISOWNING_WORDS)
    # Act
    proj_has = required & proj
    baseline_has = required & baseline
    # Assert
    assert (proj_has, baseline_has) == (required, required)


def test_project_and_baseline_carry_identical_disowning_pattern_set() -> None:
    """Drift guard for the disowning regex pattern (operator 2026-06-09)."""
    # Arrange
    proj = _pattern_set(_PROJECT_YAML)
    baseline = _pattern_set(_BASELINE_YAML)
    required = set(_REQUIRED_DISOWNING_PATTERNS)
    # Act
    proj_has = required & proj
    baseline_has = required & baseline
    # Assert
    assert (proj_has, baseline_has) == (required, required)


def test_project_and_baseline_carry_identical_katakana_jargon_word_set() -> None:
    """Drift guard for the katakana-jargon literal-word entries
    (operator 2026-06-09 comprehensive update).
    """
    # Arrange
    proj = _word_set(_PROJECT_YAML)
    baseline = _word_set(_BASELINE_YAML)
    required = set(_REQUIRED_KATAKANA_JARGON_WORDS)
    # Act
    proj_has = required & proj
    baseline_has = required & baseline
    # Assert
    assert (proj_has, baseline_has) == (required, required)


def test_project_and_baseline_carry_identical_english_equivalent_pattern_set() -> None:
    """Drift guard for the English-equivalent regex patterns
    (operator 2026-06-09 comprehensive update).
    """
    # Arrange
    proj = _pattern_set(_PROJECT_YAML)
    baseline = _pattern_set(_BASELINE_YAML)
    required = set(_REQUIRED_ENGLISH_EQUIVALENT_PATTERNS)
    # Act
    proj_has = required & proj
    baseline_has = required & baseline
    # Assert
    assert (proj_has, baseline_has) == (required, required)
