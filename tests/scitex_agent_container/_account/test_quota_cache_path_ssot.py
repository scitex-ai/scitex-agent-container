"""Writer default / reader / apptainer-bind must resolve ONE host path (SSOT).

2026-07-20 INCIDENT: the boot picker's fail-loud gate ("quota cache is blind …
run `sac accounts refresh-quota-cache`") HARD-BLOCKED `sac-restart scitex-dev`,
and the documented fix did nothing — because the writer's default wrote the
LEGACY `~/.scitex/quota-cache.json` while the reader's first candidate is the
RUNTIME `~/.scitex/agent-container/runtime/quota-cache.json`. A plain
`refresh-quota-cache` populated a file the picker never read, so the picker
stayed blind. These tests pin the invariant that makes that hint work: the
populator's default path IS the path the reader reads first (and the container
bind resolves to the same place). Revert either fix → these go RED.

PA-307 / STX-TQ002 / STX-TQ007 — real tmp I/O (no mocks), AAA markers,
one assertion per test.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._account.quota_cache import (
    HOST_RUNTIME_CACHE_SUBPATH,
    default_host_cache_path,
    host_cache_candidates,
    write_quota_cache,
)
from scitex_agent_container.runtimes._apptainer_quota_cache import (
    QUOTA_CACHE_HOST_PATH_DEFAULT,
)


def test_writer_default_path_equals_reader_first_candidate(tmp_path: Path):
    # Arrange: a fixed home so both ends resolve deterministically.
    home = tmp_path

    # Act: the populator's default write path and the reader's first candidate.
    writer_default = default_host_cache_path(home)
    reader_first = host_cache_candidates(home)[0]

    # Assert: they are the SAME file, so `refresh-quota-cache` (no --cache-path)
    # populates exactly the file the boot picker reads first.
    assert writer_default == reader_first


def test_writer_default_write_lands_at_reader_first_candidate(tmp_path: Path):
    # Arrange: an account payload written via the populator's DEFAULT path.
    home = tmp_path
    accounts = {"acct": {"short": "acct", "h5": 5.0, "d7": 5.0, "ttl_h": 7.0}}

    # Act: write with no explicit cache_path (the `refresh-quota-cache` default).
    write_quota_cache(accounts, home=home)

    # Assert: the file now exists at the reader's first candidate path.
    assert host_cache_candidates(home)[0].is_file()


def test_writer_default_is_under_the_runtime_dir_not_the_scitex_root(
    tmp_path: Path,
):
    # Arrange
    home = tmp_path

    # Act: the resolved default write path, relative to home.
    rel = default_host_cache_path(home).relative_to(home)

    # Assert: it is sac's package-scoped runtime path, not the top-level
    # `.scitex/quota-cache.json` the reader never consults first.
    assert rel == HOST_RUNTIME_CACHE_SUBPATH


def test_apptainer_bind_default_is_the_runtime_path():
    # Arrange
    # Act
    # Assert: the in-container bind source is the runtime path, so agents read
    # the same freshly-written file the host picker does (not stale legacy).
    assert QUOTA_CACHE_HOST_PATH_DEFAULT.endswith(
        "/.scitex/agent-container/runtime/quota-cache.json"
    )
