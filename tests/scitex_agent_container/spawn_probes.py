"""Lean spawn payloads for the collector timing tests.

``multiprocessing``/spawn children re-import the module that defines the
target callable. When that module is the TEST module, every owned worker
pays for a ``pytest`` (+ plugins) import under coverage tracing before it
does a millisecond of work — on a loaded CI runner (96 xdist workers over
3 matrix legs, ``nice -n 10``) that import alone exceeds the 1-4 s
collection deadlines, and result-assertion tests go red with ``'unknown'``
while passing locally. The fetchers/observers here import only stdlib
plus the two product modules under test, so the child startup measures
collection machinery, not interpreter boot.

No ``pytest`` import in this module, by design — keep it that way.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

from scitex_agent_container._account.provider_usage_projection import project
from scitex_agent_container.cli_pkg._helpers._agent_observation import (
    observe_instance,
)


def known_fetch(target, deadline):
    """A pure metadata producer executing in a real owned child process."""
    now = datetime.now(timezone.utc)
    return project("opencode-go", {"quota": {"usage": {"rolling": {
        "percent": 10, "resetsAt": (now + timedelta(hours=5)).isoformat(),
    }}}}, now)


def slow_fetch(target, deadline):
    """Land just past the 0.75s collection deadline but inside the 5.0s
    grace window: fails without grace, passes with it.

    Lives HERE, not in the test module, on purpose: spawn children
    re-import the fetcher's module, and a test-module fetcher would
    bill every owned worker a full ``pytest`` (+ plugins) import
    before a millisecond of work — seconds under xdist load, enough
    to blow the grace drain itself. See the module docstring.
    """
    time.sleep(0.7)
    return known_fetch(target, deadline)


def mixed_fetch(target, deadline):
    if target.name == "OPENCODE_GO_API_KEY_hung":
        time.sleep(10)
    return known_fetch(target, deadline)


def refusing_fetch(target, deadline):
    return {"error": "http-403"}


def recording_fetch(target, deadline):
    """Make any network-producer invocation visible in a private fixture."""
    target.auth_path.write_text("provider-invoked")
    return known_fetch(target, deadline)


def blocked_fifo_writer(path, snapshot):
    """An actual blocking filesystem boundary, without a FIFO reader."""
    with path.open("w") as stream:
        stream.write(json.dumps(snapshot))


def parallel_fetch(target, deadline):
    """Require another worker to publish before returning, proving concurrency."""
    directory = target.auth_path
    (directory / target.name).write_text(str(os.getpid()))
    while len(list(directory.iterdir())) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    if len(list(directory.iterdir())) < 2:
        return {"error": "metadata-deadline"}
    return known_fetch(target, deadline)


def deadline_probe_fetch(target, deadline):
    """Record the fetch bound handed to the worker, then deliver.

    ``target.auth_path`` is a scratch directory (same shape as
    :func:`parallel_fetch`); the remaining fetch budget at fetch time
    lands in ``<name>.deadline`` for the collection test to assert.
    """
    directory = target.auth_path
    (directory / (target.name + ".deadline")).write_text(str(deadline - time.monotonic()))
    return known_fetch(target, deadline)


def immediate_observer(record, snapshot, detail, salt):
    return observe_instance(record, snapshot, detail, salt)


def delayed_observer(record, snapshot, detail, salt):
    if record["name"] == "slow":
        time.sleep(10)
    return observe_instance(record, snapshot, detail, salt)
