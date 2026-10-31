"""Tests for the 2026-10-31 cold-fetch fuse (card sac-gui-cold-fetch-timeout-20261026).

Root cause (verified 2026-10-26 from compute-03 hub logs + listener timing):
the shell renders in ~0.6s and the listener answers in ~1s, but the cold-shell
fetch in warmColdShell() had no timeout, so a hung first fetch froze the page
on "Reading the fleet..." forever — the retry counter only advances on
settled responses. The fix threads an AbortController fuse per poll attempt,
tied to the 60s server ceiling (_remote.DEFAULT_TIMEOUT_SECONDS = 60.0);
a hung fetch now settles through the existing .catch into bounded retry,
then settleUnavailable().

JS behaviour is pinned by source assertions (no browser needed), per the
idiom the 2026-10-22 module established. Each test has AAA markers and a
single assertion.
"""

from __future__ import annotations

import re
from pathlib import Path

LEAF = Path(__file__).resolve().parents[3] / "src/scitex_agent_container/_django"
FLEET_JS = (LEAF / "static/scitex_agent_container/js/fleet-details.js").read_text(
    encoding="utf-8"
)

# The server ceiling this fuse is tied to
# (src/.../_django/_remote.py DEFAULT_TIMEOUT_SECONDS).
SERVER_CEILING_MS = 60_000


def test_cold_fetch_fuse_covers_the_server_ceiling():
    # Arrange — the fuse constant and the server ceiling it must cover.
    # Act
    match = re.search(r"var COLD_FETCH_TIMEOUT_MS\s*=\s*(\d+)\s*;", FLEET_JS)
    # Assert — the fuse waits out the full server budget (never faster).
    assert match is not None and int(match.group(1)) >= SERVER_CEILING_MS


def test_cold_fetch_attempt_carries_abort_signal():
    # Arrange — static JS only, no browser needed.
    # Act
    js = FLEET_JS
    # Assert — every poll attempt builds a controller, arms the fuse against
    # it, and threads its signal into the fetch.
    assert (
        "new AbortController()" in js
        and "controller.abort()" in js
        and "signal: controller.signal" in js
    )


def test_cold_fetch_fuse_is_cleared_on_every_settle():
    # Arrange — static JS only, no browser needed.
    # Act
    js = FLEET_JS
    # Assert — both settle paths (.then data + .catch failure, which is where
    # an abort lands) disarm the fuse, so a late abort can never fire into
    # the next attempt.
    assert js.count("clearTimeout(fuse)") >= 2


def test_cold_fetch_fuse_keeps_the_bounded_poll_contract():
    # Arrange — static JS only, no browser needed.
    # Act
    js = FLEET_JS
    # Assert — the pre-existing bound is untouched: bounded polls, in-place
    # settle, no new reload path.
    assert (
        "COLD_POLL_MAX" in js
        and "settleUnavailable" in js
        and js.count("window.location.reload") <= 2
    )
