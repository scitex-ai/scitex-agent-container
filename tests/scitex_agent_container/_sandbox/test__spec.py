"""``spec.sandbox`` parsing: explicit keys, refusals that keep the seal tight."""

from __future__ import annotations

import pytest

from scitex_agent_container._sandbox._spec import (
    parse_allow_entry,
    parse_sandbox,
    parse_size_mb,
)


def _block(**over):
    block = {
        "egress": {"allow": ["opencode.ai:443", "pypi.org:443"]},
        "env": {"allow": ["SCITEX_STORE_DSN"], "model_key": "MODEL_KEY"},
        "run": {"root": "/scratch/someone/sac-runs", "size": "4G", "after": "remove"},
    }
    block.update(over)
    return {"sandbox": block}


def test_absent_block_is_not_sandboxed():
    # Arrange
    # Act
    sb = parse_sandbox({})
    # Assert
    assert sb is None


def test_full_block_parses_allowlist():
    # Arrange
    # Act
    sb = parse_sandbox(_block())
    # Assert
    assert sb.egress_allow == (("opencode.ai", 443), ("pypi.org", 443))


def test_size_is_megabytes():
    # Arrange
    # Act
    sb = parse_sandbox(_block())
    # Assert
    assert sb.run_size_mb == 4096


def test_unknown_key_is_refused():
    # Arrange
    spec = _block(network={"open": True})
    # Act
    # Assert
    with pytest.raises(ValueError, match="unknown keys"):
        parse_sandbox(spec)


def test_missing_key_is_refused():
    # Arrange
    spec = _block(env={"allow": []})
    # Act
    # Assert
    with pytest.raises(ValueError, match="missing required keys"):
        parse_sandbox(spec)


def test_empty_allowlist_is_refused():
    # Arrange
    spec = _block(egress={"allow": []})
    # Act
    # Assert
    with pytest.raises(ValueError, match="NON-EMPTY"):
        parse_sandbox(spec)


@pytest.mark.parametrize("root", ["/tmp/runs", "/var/tmp/x", "/dev/shm/x"])
def test_tmp_run_root_is_refused(root):
    # Arrange
    spec = _block(run={"root": root, "size": "1G", "after": "remove"})
    # Act
    # Assert
    with pytest.raises(ValueError, match="never tmp"):
        parse_sandbox(spec)


def test_unknown_after_mode_is_refused():
    # Arrange
    spec = _block(run={"root": "/scratch/x", "size": "1G", "after": "keep"})
    # Act
    # Assert
    with pytest.raises(ValueError, match="after"):
        parse_sandbox(spec)


@pytest.mark.parametrize(
    "entry",
    [
        "https://pypi.org:443",
        "pypi.org",
        "*.github.com:443",
        "10.0.0.1:443",
        "pypi.org:0",
    ],
)
def test_bad_allow_entries_are_refused(entry):
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError):
        parse_allow_entry(entry)


def test_allow_entry_is_lowercased():
    # Arrange
    # Act
    got = parse_allow_entry("PyPI.org:443")
    # Assert
    assert got == ("pypi.org", 443)


@pytest.mark.parametrize("size", ["4K", "", "G", "-1G"])
def test_bad_sizes_are_refused(size):
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError):
        parse_size_mb(size)
