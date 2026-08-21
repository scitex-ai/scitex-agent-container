"""Fail the test that leaves a background thread running behind it.

WHY THIS EXISTS, and why it is a GATE rather than a report. On 2026-08-20
develop went red on py3.12 and py3.13 with two failures that looked unrelated
to each other and to their own files::

    test_ci_group.py::test_why_green_run_says_no_failures
      assert 'ERRO: birth ...\\nno failures' == 'no failures'
    test_guard_group.py::test_json_top_level_keys_are_stable
      JSONDecodeError: Expecting value: line 1 column 1 (char 0)

Neither test was at fault. Four test files enabled ``health.enabled`` in a
spec, so ``agent_start`` launched a REAL ``health_monitor`` on a
``daemon=True`` thread that nothing joined. It outlived its test and kept
looping for the rest of the xdist worker, and its ``logger.error`` landed in
whatever capture buffer happened to be open -- an unrelated command's
``CliRunner`` output, AHEAD of that command's own text.

Every guilty test PASSED. 169 passed while leaking. That is the whole reason
this must fail the LEAKER rather than merely record it: the damage is done to
a stranger three files later, nothing local points back, and serially the
victims are green -- so it reads as an xdist flake and a re-run "fixes" it.

WHAT IT CANNOT SEE, stated so nobody reads a green run as more than it is:
a thread that has already FINISHED by teardown is invisible here, and so is
one started and joined within a single test. This catches the leak, not every
misuse of threading.
"""

from __future__ import annotations

import threading
from typing import Iterator

import pytest

# Threads that are SUPPOSED to outlive a test. Matched on the thread NAME, not
# on the callable it runs.
#
# That distinction is load-bearing and I got it wrong once: the hooks pool's
# target is `ThreadPoolExecutor._worker`, which reads exactly like a leak and
# which I reported as a second, worse leak ("daemon=False, does not die with
# the process") before finding it was a deliberate module-level pool. A
# target-based allowlist would have to name a private stdlib function; a
# name-based one names OUR pool and nothing else, because
# `thread_name_prefix` is the thing the pool's author chose on purpose.
#
# Keep this list SHORT. Every entry is a promise that something cleans the
# thread up, and a wrong entry silently disables the gate for a whole class.
_ALLOWED_NAME_PREFIXES: tuple[str, ...] = (
    # scitex_agent_container.hooks:41 -- module-level ThreadPoolExecutor,
    # bounded at max_workers=4, created once per process and never grown.
    "scitex-hook",
    # asyncio's default executor, same shape, owned by the loop.
    "asyncio_",
    # pytest-xdist's own execnet machinery.
    "execnet",
)


def _live_threads() -> dict[int, threading.Thread]:
    return {t.ident: t for t in threading.enumerate() if t.ident is not None}


def _is_allowed(thread: threading.Thread) -> bool:
    return any(thread.name.startswith(p) for p in _ALLOWED_NAME_PREFIXES)


def leaked_threads(before: "set[int]") -> "list[threading.Thread]":
    """Live threads absent from ``before`` and not allowlisted.

    Split out of the fixture so the gate's own regression test can drive it
    against a thread it controls and then joins. A gate whose firing is never
    exercised is a thermometer in a sealed box: it reports green whether or
    not it is capable of reporting anything else.
    """
    return [
        t
        for ident, t in _live_threads().items()
        if ident not in before and t.is_alive() and not _is_allowed(t)
    ]


def _describe(thread: threading.Thread) -> str:
    target = getattr(thread, "_target", None)
    where = getattr(target, "__module__", None)
    what = getattr(target, "__name__", None) or "(no target -- subclassed Thread?)"
    origin = f"{where}.{what}" if where else what
    return f"  {thread.name}  daemon={thread.daemon}  target={origin}"


@pytest.fixture(autouse=True, scope="function")
def _assert_no_leaked_threads(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail the test that leaves a NEW live thread behind it."""
    before = set(_live_threads())

    yield

    leaked = leaked_threads(before)
    if not leaked:
        return

    raise AssertionError(
        "\n=============== TEST LEAKED A BACKGROUND THREAD ===============\n"
        f"`{request.node.nodeid}` finished with {len(leaked)} new thread(s)\n"
        "still running:\n\n"
        + "\n".join(_describe(t) for t in leaked)
        + "\n\nTHIS TEST WILL STILL PASS EVERYWHERE ELSE. The thread keeps\n"
        "looping for the rest of this xdist worker and writes into whatever\n"
        "capture buffer is open later -- so the failure surfaces in an\n"
        "UNRELATED test, in an unrelated file, and only under -n auto. That\n"
        "is why this fails HERE, at the leak, and not there at the symptom.\n\n"
        "MOST LIKELY CAUSE: a spec with `health.enabled: true` reaching\n"
        "`_start_supervision.py`, which calls `thread_factory(...)` --\n"
        "defaulting to the real `threading.Thread` -- and nobody joins it.\n\n"
        "THE FIX IS ALREADY IN THIS REPO, three times over. Pass a\n"
        "hand-written stand-in via `thread_factory=` that RECORDS start()\n"
        "instead of running it, so a test can still assert the monitor was\n"
        "launched (PA-306: a stand-in, never a mock):\n"
        "  tests/.../_lifecycle/test__start_supervision.py     (_RecordingThread)\n"
        "  tests/.../_lifecycle/test__instances_auto_grant.py  (_CapturingThread)\n"
        "  tests/.../cli_pkg/lifecycle/test__start_force_clears_session.py\n\n"
        "Leave `health.enabled: true` in the spec -- the point is to keep\n"
        "covering the health-enabled start path, withholding only the real\n"
        "thread.\n\n"
        "IF THE THREAD IS LEGITIMATELY LONG-LIVED (a process-wide pool), give\n"
        "it a `thread_name_prefix` and add that prefix to\n"
        "_ALLOWED_NAME_PREFIXES in this file -- with a comment naming what\n"
        "bounds it. Do NOT allowlist by target: the stdlib pool's target is\n"
        "`_worker`, which is indistinguishable from a genuine leak.\n"
        "===============================================================\n"
    )


__all__ = ["_assert_no_leaked_threads", "leaked_threads"]
