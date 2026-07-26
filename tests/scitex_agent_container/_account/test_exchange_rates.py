"""Tests for cached ECB USD/JPY reference-rate resolution."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scitex_agent_container._account.exchange_rates import (
    ECB_DAILY_URL,
    resolve_usd_jpy_rate,
)

_NOW = datetime(2026, 7, 26, 12, tzinfo=timezone.utc)
_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Envelope>
  <Cube>
    <Cube time="2026-07-24">
      <Cube currency="USD" rate="1.1377"/>
      <Cube currency="JPY" rate="186.38"/>
    </Cube>
  </Cube>
</Envelope>
"""


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return _XML


def _opener(_request, timeout):
    assert timeout == 5
    return _Response()


def test_explicit_rate_wins_without_fetch(tmp_path: Path) -> None:
    # Arrange
    # Act
    result = resolve_usd_jpy_rate(home=tmp_path, override=160.0)
    # Assert
    assert result["rate"] == 160.0


def test_ecb_cross_rate_is_jpy_per_usd(tmp_path: Path) -> None:
    # Arrange
    # Act
    result = resolve_usd_jpy_rate(home=tmp_path, opener=_opener, now=_NOW)
    # Assert
    assert result["rate"] == round(186.38 / 1.1377, 8)


def test_ecb_rate_date_is_preserved(tmp_path: Path) -> None:
    # Arrange
    # Act
    result = resolve_usd_jpy_rate(home=tmp_path, opener=_opener, now=_NOW)
    # Assert
    assert result["rate_date"] == "2026-07-24"


def test_ecb_source_is_explicit(tmp_path: Path) -> None:
    # Arrange
    # Act
    result = resolve_usd_jpy_rate(home=tmp_path, opener=_opener, now=_NOW)
    # Assert
    assert result["source"] == ECB_DAILY_URL


def test_fresh_cache_avoids_network(tmp_path: Path) -> None:
    # Arrange
    cache = tmp_path / ".scitex" / "cache" / "usd_jpy_rate.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            {
                "rate": 161.5,
                "rate_date": "2026-07-25",
                "fetched_at": _NOW.isoformat(),
            }
        )
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("network should not be called")

    # Act
    result = resolve_usd_jpy_rate(
        home=tmp_path,
        opener=fail_if_called,
        now=_NOW,
    )
    # Assert
    assert result["from_cache"] is True
