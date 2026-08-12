"""Build REAL hook executables for the never-stop-when-task-remains tests.

PA-306 no-mocks: these are not patched callables. Each helper writes a real
executable to disk that a real :func:`subprocess.run` spawns, prints to real
stdout/stderr, and exits with a real code — the same interface the
scitex-cards executable presents. ``$SAC_MAY_STOP_CMD`` (a documented
production knob, not a test-only hatch) points sac at the script.

A fixture is a CLAIM ABOUT SOMEONE ELSE'S OUTPUT, so the two below are not
invented — both were captured from real runs on this host:

* :data:`CLICK_USAGE_ERROR` is what ``scitex-cards`` really prints (and the
  code it really exits with) for a verb it does not have.
* :func:`runnable_verdict` mirrors a real ``may-stop --agent ...`` answer.

An earlier revision deliberately shipped NO fixture of the detector's
payload, to avoid coupling sac to scitex-cards' schema. That purity is what
let the live defect through: with only hook-protocol JSON in the suite,
nothing ever exercised the shape the detector ACTUALLY emits, and the
never-stop gate was tested exclusively against output it never receives. We
now assert the minimum needed to tell an ANSWER from a FAILURE TO ANSWER —
the ``runnable`` flag — and nothing more.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: Exactly what a host whose scitex-cards predates ``may-stop`` emits: the
#: usage text on STDERR, an EMPTY stdout, and exit 2 — the same code the
#: protocol uses for "work remains". Captured from a real run.
CLICK_USAGE_ERROR = (
    "Usage: scitex-cards [OPTIONS] [COMMAND] [ARGS]...\n"
    "Try 'scitex-cards --help' for help.\n"
    "\n"
    "Error: No such command 'may-stop'."
)


def write_detector(
    tmp_path: Path,
    *,
    returncode: int,
    stdout: str = "",
    stderr: str = "",
    name: str = "fake-hook-exe",
) -> Path:
    """Write an executable emitting exactly these streams + exit code."""
    script = tmp_path / name
    script.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null 2>&1 || true\n"  # drain the hook payload like the real one
        f"cat <<'SAC_EOF_OUT'\n{stdout}\nSAC_EOF_OUT\n"
        f"cat >&2 <<'SAC_EOF_ERR'\n{stderr}\nSAC_EOF_ERR\n"
        f"exit {returncode}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def stale_cards_detector(tmp_path: Path, *, name="stale-cards") -> Path:
    """A detector from BEFORE ``may-stop`` existed — the fleet's steady state.

    Exits 2 (which the protocol reads as "work remains") while stdout is
    empty and stderr holds click's usage error.
    """
    return write_detector(
        tmp_path, returncode=2, stdout="", stderr=CLICK_USAGE_ERROR, name=name
    )


def runnable_verdict(
    *, agent: str = "a", items: "list[dict] | None" = None, idle_seconds: int = 1084
) -> str:
    """One line of JSON shaped like a real ``may-stop`` "work remains" answer.

    Note it carries NO ``decision`` key — the detector answers in its own
    verdict schema, not in Claude Code's hook protocol.
    """
    if items is None:
        items = [
            {
                "card_id": "card-1",
                "reason": "in_progress card",
                "next_action": "work it, update it, or close it",
            }
        ]
    return json.dumps(
        {
            "agent": agent,
            "runnable": True,
            "items": items,
            "idle_seconds": idle_seconds,
        }
    )


def isolate_runtime(env_save_restore, tmp_path: Path) -> Path:
    """Point the loop-guard state tree at ``tmp_path``.

    ``runtime_base_dir()`` reads its env var at CALL time, so this really
    redirects the state — there is no import-time constant to defeat it.
    """
    root = tmp_path / "sac-runtime"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(root))
    return root


def clear_identity(env_save_restore) -> None:
    """Remove every identity var so a test controls resolution completely."""
    from scitex_agent_container._never_stop_when_task_remains._identity import (
        IDENTITY_ENV_VARS,
    )

    for key in IDENTITY_ENV_VARS:
        env_save_restore.delete(key)


def detector_env(env_save_restore, script: "Path | str") -> None:
    """Point sac at ``script`` as the registered hook executable."""
    env_save_restore.set("SAC_MAY_STOP_CMD", str(script))


def missing_detector(env_save_restore, tmp_path: Path) -> None:
    """Point sac at a path that genuinely does not exist."""
    env_save_restore.set(
        "SAC_MAY_STOP_CMD", str(tmp_path / "no-such-binary" / os.sep.join(["gone"]))
    )


# ---------------------------------------------------------------------------
# The awaiting-operator READ command (`scitex-cards list-tasks ... --json`).
#
# Same no-mocks discipline as above: a real executable on disk, spawned by a
# real subprocess, printing real bytes. Nothing here patches a callable.
# ---------------------------------------------------------------------------

#: Distinct from ``_TTL_OFF`` below: tests that want to prove the CACHE works
#: set a real TTL. Everything else disables it so each act reads afresh.
AWAITING_TTL_ENV = "SAC_AWAITING_CARDS_TTL_S"
AWAITING_CMD_ENV = "SAC_AWAITING_CARDS_CMD"
AWAITING_STORE_ENV = "SAC_AWAITING_STORE_CMD"

#: The shape `scitex-cards resolve-store --json` really answers with, captured
#: from the live host 2026-08-12. The store identity is NOT the env var: this
#: fleet has four stores, and a reader that trusts `$SCITEX_CARDS_DB` rather
#: than the resolver is how one ends up quoting an abandoned one.
LIVE_STORE_JSON = json.dumps(
    {
        "resolved": "postgresql://scitex_cards@127.0.0.1:55432/scitex_cards",
        "db_env": "postgresql://scitex_cards@127.0.0.1:55432/scitex_cards",
        "user_store": "/home/agent/.scitex/cards/cards.db",
        "backend": "postgresql",
        "store_uuid": "1d55dd6e-3d2a-4c24-a429-a78835ab988f",
    }
)

#: The SAME store, once it also reports the engine half of its identity.
#: ``store_uuid`` lives in the rows and is therefore copied by a fork;
#: ``system_identifier`` comes from the engine (Postgres
#: ``pg_control_system()``), which a file copy cannot carry with it. On
#: 2026-08-11 two endpoints shared the uuid below while their
#: system_identifiers differed — 7671108644284358700 vs 7672112238472680366 —
#: and that difference was the only thing that distinguished them.
PAIRED_STORE_JSON = json.dumps(
    {
        "resolved": "postgresql://scitex_cards@127.0.0.1:55432/scitex_cards",
        "backend": "postgresql",
        "store_uuid": "1d55dd6e-3d2a-4c24-a429-a78835ab988f",
        "system_identifier": "7671108644284358700",
    }
)

#: The ABANDONED sidecar, measured the same night: 365 rows, 149 unseen, a
#: zero-byte WAL, and no write since the previous morning while readers kept
#: attaching. Opened constantly, written never.
DEAD_SIDECAR_JSON = json.dumps(
    {
        "resolved": "/home/agent/.scitex/cards/runtime/todo.db",
        "backend": "sqlite",
        "store_uuid": "deadbeef-0000-0000-0000-000000000000",
    }
)


def store_identity_cmd(
    env_save_restore,
    tmp_path: Path,
    payload: str = LIVE_STORE_JSON,
    *,
    returncode: int = 0,
    name: str = "fake-resolve-store",
) -> Path:
    """Install a real store-identity command answering with ``payload``."""
    script = write_detector(
        tmp_path, returncode=returncode, stdout=payload, name=name
    )
    env_save_restore.set(AWAITING_STORE_ENV, str(script))
    return script

#: What a refused read really prints. Captured from the live board on
#: 2026-08-12, while the operator's own report said the database was
#: "refusing the read intermittently tonight" — the same run answered rc 0
#: with 21 rows minutes later.
REFUSED_READ_STDERR = (
    "Traceback (most recent call last):\n"
    '  File "scitex_cards/_db_export.py", line 54, in _record\n'
    "    raise ExportRefused(\n"
    "scitex_cards._db_export.ExportRefused: notifications row 'n_5e9e1ec3555f' "
    "has no record_json payload — this DB predates schema v3's payload columns "
    "and cannot be back-filled. Exporting stripped records is worse than "
    "exporting none."
)


def operator_card(
    card_id: str, *, blocked_days_ago: int, agent: str = "agent-x"
) -> dict:
    """One row shaped like a real ``list-tasks --json`` card blocked on a human.

    Carries the three fields the summary is defined on (``status``,
    ``blocker``, a stamp) plus the ``title`` a real row would have, so a test
    row is not thinner than the thing it stands for.
    """
    stamp = datetime.now(timezone.utc) - timedelta(days=blocked_days_ago)
    return {
        "id": card_id,
        "title": f"[q] {card_id} needs an operator decision",
        "status": "blocked",
        "blocker": "operator-decision",
        "assignee": agent,
        "scope": f"agent:{agent}",
        "blocked_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "created_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_activity": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def awaiting_cards(
    env_save_restore,
    tmp_path: Path,
    cards: "list[dict] | None" = None,
    *,
    returncode: int = 0,
    stdout: "str | None" = None,
    stderr: str = "",
    name: str = "fake-list-tasks",
) -> Path:
    """Install a real read command answering with ``cards``, and point sac at it.

    ``stdout`` overrides the rendered JSON outright (for the unreadable-output
    cases); ``returncode`` non-zero is how the "database refuses the read"
    case is reproduced — a real process, a real failing exit code.
    """
    body = json.dumps(cards if cards is not None else []) if stdout is None else stdout
    script = write_detector(
        tmp_path, returncode=returncode, stdout=body, stderr=stderr, name=name
    )
    env_save_restore.set(AWAITING_CMD_ENV, str(script))
    env_save_restore.set(AWAITING_TTL_ENV, "0")
    # Keep the store probe offline too — without it the REAL
    # `scitex-cards resolve-store` is spawned against the live fleet store.
    if not os.environ.get(AWAITING_STORE_ENV):
        store_identity_cmd(env_save_restore, tmp_path)  # read afresh unless a test says else
    return script


#: The env var ``scitex-cards list-tasks`` silently ANDs into its filter.
SCOPE_ENV = "SCITEX_TODO_SCOPE"


def scope_sensitive_board(
    env_save_restore,
    tmp_path: Path,
    cards: "list[dict]",
    *,
    name: str = "scope-sensitive-list-tasks",
) -> Path:
    """A reader that honours ``$SCITEX_TODO_SCOPE`` exactly as the real one does.

    NOT an invented behaviour — MEASURED on the live board, 2026-08-12::

        baseline                          : 21
        with SCITEX_TODO_SCOPE=other      : 0
        same + explicit --scope ''        : 21
        no env + explicit --scope ''      : 21

    That zero is the whole reason this fixture exists. A new alarm that
    silently reports nothing is WORSE than no alarm, because it converts
    "nobody looked" into "we checked and it was clear" — the exact failure
    family this feature was built to remove, reappearing inside the feature. A
    test that only ever ran with the variable unset would have passed and
    shipped it.

    The rule reproduced here: an explicit ``--scope ''`` opts out; no
    ``--scope`` at all inherits the ambient value; any other explicit scope
    filters. The last two return the empty board.
    """
    body = json.dumps(cards)
    payload = tmp_path / f"{name}.json"
    payload.write_text(body)
    script = tmp_path / name
    script.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null 2>&1 || true\n"
        "__given=no\n__val=\n__prev=\n"
        'for __a in "$@"; do\n'
        '  if [ "$__prev" = "--scope" ]; then __given=yes; __val="$__a"; fi\n'
        '  __prev="$__a"\n'
        "done\n"
        # No --scope at all: the ambient variable applies, as it really does.
        'if [ "$__given" = no ] && [ -n "$SCITEX_TODO_SCOPE" ]; then\n'
        "  echo '[]'\n  exit 0\nfi\n"
        # An explicit NON-empty scope filters; only '' means "ignore it".
        'if [ "$__given" = yes ] && [ -n "$__val" ]; then\n'
        "  echo '[]'\n  exit 0\nfi\n"
        f"cat {shlex.quote(str(payload))}\n"
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    env_save_restore.set(AWAITING_CMD_ENV, str(script))
    env_save_restore.set(AWAITING_TTL_ENV, "0")
    # Keep the store probe offline too — without it the REAL
    # `scitex-cards resolve-store` is spawned against the live fleet store.
    if not os.environ.get(AWAITING_STORE_ENV):
        store_identity_cmd(env_save_restore, tmp_path)
    return script


def no_awaiting_cards(env_save_restore, tmp_path: Path) -> Path:
    """The default board state for tests: nothing is waiting on a human.

    Every test that drives the hook needs this, because without it the real
    ``scitex-cards`` on PATH would be spawned against the LIVE fleet board —
    slow, non-deterministic, and a test suite that fails when a database it
    does not own is down.
    """
    return awaiting_cards(env_save_restore, tmp_path, [], name="no-awaiting-cards")


def unreadable_board(env_save_restore, tmp_path: Path) -> Path:
    """The degraded case: the read command fails the way a refused read does.

    Captured shape: ``scitex-cards list-tasks --json`` against a store it will
    not export exits non-zero with a traceback on stderr and NOTHING on
    stdout (measured on the live board, 2026-08-12:
    ``ExportRefused: notifications row ... has no record_json payload``).
    """
    return awaiting_cards(
        env_save_restore,
        tmp_path,
        returncode=1,
        stdout="",
        stderr=REFUSED_READ_STDERR,
        name="refusing-board",
    )
