"""Branch-coverage closure for ``_read_installed_plugins`` fallback chain.

Covers the optional-cred fallback paths in ``credentials.py`` lines
147-155: each ``if not isinstance(...)`` guard inside the plugin
loader. AAA structure, one assertion per test, real on-disk JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._account.credentials import (
    read_credentials_metadata,
)


def _write_plugins_file(home: Path, payload: dict) -> None:
    plugins_dir = home / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "installed_plugins.json").write_text(json.dumps(payload))


def test_plugins_top_level_non_dict_yields_empty_list(tmp_path: Path) -> None:
    # Arrange — ``plugins`` value is a list, not a dict (line 148 branch).
    _write_plugins_file(tmp_path, {"plugins": ["not-a-dict"]})
    # Act
    result = read_credentials_metadata(home=tmp_path)
    # Assert
    assert result["installed_plugins"] == []


def test_plugins_entries_value_non_list_is_skipped(tmp_path: Path) -> None:
    # Arrange — one plugin maps to a string instead of list (line 152 branch).
    _write_plugins_file(tmp_path, {"plugins": {"bad-plugin": "not-a-list"}})
    # Act
    result = read_credentials_metadata(home=tmp_path)
    # Assert
    assert result["installed_plugins"] == []


def test_plugins_entry_non_dict_member_is_skipped(tmp_path: Path) -> None:
    # Arrange — entry list contains a non-dict element (line 155 branch).
    _write_plugins_file(tmp_path, {"plugins": {"weird-plugin": ["not-a-dict-entry"]}})
    # Act
    result = read_credentials_metadata(home=tmp_path)
    # Assert
    assert result["installed_plugins"] == []


def test_plugins_mixed_valid_and_invalid_entries_keeps_only_valid_dict(
    tmp_path: Path,
) -> None:
    # Arrange — one valid dict mixed with junk types in the same list.
    _write_plugins_file(
        tmp_path,
        {
            "plugins": {
                "mixed": [
                    "string-junk",
                    42,
                    {"version": "1.0.0", "scope": "user"},
                ]
            }
        },
    )
    # Act
    result = read_credentials_metadata(home=tmp_path)
    # Assert
    assert len(result["installed_plugins"]) == 1


def test_plugins_top_level_string_value_returns_empty(tmp_path: Path) -> None:
    # Arrange — ``plugins`` is a JSON string scalar.
    _write_plugins_file(tmp_path, {"plugins": "definitely-not-a-dict"})
    # Act
    result = read_credentials_metadata(home=tmp_path)
    # Assert
    assert result["installed_plugins"] == []
