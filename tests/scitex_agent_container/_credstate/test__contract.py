"""The ADR-0022 §5.1 contract must REFUSE non-conforming tables, not warn.

These tests are the reason the contract is worth anything: §7.3 of the ADR
records that the rule existed and was enforced nowhere. A rule that is
only written down is a rule that tables get created without.
"""

from __future__ import annotations

import re

import pytest

from scitex_agent_container._credstate._contract import (
    SYNC_COLUMNS,
    SyncContractError,
    assert_sync_contract,
    parse_columns,
    strip_comments,
    sync_columns_sql,
    table_name_of,
)
from scitex_agent_container._credstate._schema import TABLES


def _table(body: str, *, name: str = "t") -> str:
    return f"CREATE TABLE IF NOT EXISTS {name} (\n{body}\n)"


def _full_body(*, omit: str | None = None, pk: str = "PRIMARY KEY (row_uuid)") -> str:
    lines = [col.ddl() + "," for col in SYNC_COLUMNS if col.name != omit]
    lines.append("payload TEXT NULL,")
    lines.append(pk)
    return "\n".join(lines)


@pytest.mark.parametrize("name,ddl,conflict_class", TABLES)
def test_every_shipped_table_satisfies_its_declared_conflict_class(
    name, ddl, conflict_class
):
    # Arrange — the shipped DDL is the primary subject of this whole module.
    subject = ddl
    # Act
    result = assert_sync_contract(subject, conflict_class=conflict_class)
    # Assert — returns None when conforming; raises otherwise.
    assert result is None


@pytest.mark.parametrize("column", [c.name for c in SYNC_COLUMNS])
def test_a_table_missing_a_sync_column_is_refused_and_the_column_named(column):
    # Arrange
    ddl = _table(_full_body(omit=column))
    # Act / Assert
    # Assert
    with pytest.raises(SyncContractError, match=re.escape(column)):
        assert_sync_contract(ddl, conflict_class="state")


def test_a_log_table_whose_primary_key_omits_origin_node_is_refused():
    # Arrange
    ddl = _table(_full_body(pk="PRIMARY KEY (row_uuid)"))
    # Act
    # Assert
    with pytest.raises(SyncContractError, match="origin_node"):
        assert_sync_contract(ddl, conflict_class="log")


def test_a_log_table_with_origin_node_in_a_composite_key_is_accepted():
    # Arrange
    ddl = _table(_full_body(pk="PRIMARY KEY (origin_node, row_uuid)"))
    # Act
    result = assert_sync_contract(ddl, conflict_class="log")
    # Assert
    assert result is None


def test_the_tombstone_column_declared_not_null_is_refused():
    # Arrange — NULL is what "not deleted" means; NOT NULL destroys that.
    body = _full_body().replace(
        "deleted_at TIMESTAMPTZ NULL", "deleted_at TIMESTAMPTZ NOT NULL"
    )
    # Act
    # Assert
    with pytest.raises(SyncContractError, match="tombstone"):
        assert_sync_contract(_table(body), conflict_class="state")


def test_a_nullable_row_uuid_is_refused():
    # Arrange
    body = _full_body().replace("row_uuid UUID NOT NULL", "row_uuid UUID NULL")
    # Act
    # Assert
    with pytest.raises(SyncContractError, match="row_uuid"):
        assert_sync_contract(_table(body), conflict_class="state")


def test_an_unknown_conflict_class_is_refused():
    # Arrange
    ddl = _table(_full_body())
    # Act
    # Assert
    with pytest.raises(SyncContractError, match="whatever"):
        assert_sync_contract(ddl, conflict_class="whatever")


def test_configuration_tables_are_exempt_because_git_is_their_sync():
    # Arrange — ADR-0022 §5.2 rule 1 removes configuration from sync.
    ddl = _table("name TEXT PRIMARY KEY,\nvalue TEXT NOT NULL")
    # Act
    result = assert_sync_contract(ddl, conflict_class="configuration")
    # Assert
    assert result is None


def test_unparseable_input_fails_closed_rather_than_silently_passing():
    # Arrange
    not_sql = "this is not sql at all"
    # Act
    # Assert
    with pytest.raises(SyncContractError):
        assert_sync_contract(not_sql, conflict_class="state")


def test_comment_parentheses_do_not_hide_the_generated_sync_columns():
    # Arrange — regression: the generated block's comment holds '(' and ')',
    # and parsing before stripping mis-read the table.
    ddl = _table(
        sync_columns_sql() + "\n    payload TEXT NULL,\n    PRIMARY KEY (row_uuid)"
    )
    # Act
    columns = parse_columns(ddl)
    # Assert
    assert {c.name for c in SYNC_COLUMNS} <= set(columns)


def test_the_generated_block_still_validates_end_to_end():
    # Arrange
    ddl = _table(
        sync_columns_sql() + "\n    payload TEXT NULL,\n    PRIMARY KEY (row_uuid)"
    )
    # Act
    result = assert_sync_contract(ddl, conflict_class="state")
    # Assert
    assert result is None


def test_strip_comments_drops_the_commented_text():
    # Arrange
    sql = "a INT, -- commentary\n b INT"
    # Act
    stripped = strip_comments(sql)
    # Assert
    assert "commentary" not in stripped


def test_strip_comments_keeps_the_code_after_the_comment_line():
    # Arrange
    sql = "a INT, -- commentary\n b INT"
    # Act
    stripped = strip_comments(sql)
    # Assert
    assert "b INT" in stripped


def test_the_table_name_is_recovered_from_the_statement():
    # Arrange
    ddl = _table(_full_body(), name="credential_thing")
    # Act
    name = table_name_of(ddl)
    # Assert
    assert name == "credential_thing"


def test_an_inline_check_constraint_does_not_truncate_the_column_list():
    # Arrange
    body = _full_body() + ",\nCONSTRAINT c CHECK (payload IN ('a', 'b'))"
    # Act
    columns = parse_columns(_table(body))
    # Assert
    assert "payload" in columns


def test_constraint_clauses_are_not_mistaken_for_columns():
    # Arrange
    body = _full_body() + ",\nCONSTRAINT c CHECK (payload IN ('a', 'b'))"
    # Act
    columns = parse_columns(_table(body))
    # Assert
    assert "CONSTRAINT" not in columns
