#!/usr/bin/env python3
"""Deploy-freshness alarm. Cron-side half of ``sac freshness``.

Runs hourly, off any Claude session, and does two jobs:

1. **Refreshes the cache** that every ``sac`` invocation reads. The CLI
   warning cannot do its own network lookup (it would put a multi-second
   PyPI round trip in front of every command), so something has to write
   that file. This is that something.
2. **Alarms** on the notify rail when the fleet is positively STALE, so
   the failure reaches the operator even if nobody happens to type
   ``sac`` today.

Shape deliberately mirrors ``quota-alarm.py`` (the established prior art):
shell the ``sac`` JSON surface, keep the logic in the package, debounce
per condition, notify via ``scitex-notification``. No forked logic lives
here — this file decides *when to speak*, never *what is true*.

THE BOOTSTRAP, AND WHY IT CANNOT FAIL QUIETLY
---------------------------------------------
This alarm shells ``sac freshness refresh``, a command that only exists
in the version that introduces it. On a host whose sac predates it, that
call fails — and a naive script would treat the failure as "nothing to
report" and go silently to sleep. That is the *exact* bug class this
whole subsystem exists to abolish: the alarm for staleness, defeated by
staleness.

So a missing ``freshness`` subcommand on an sac that IS installed is
itself treated as positive evidence of STALE (that sac is, by
construction, older than this feature) and raises the alarm. A missing
``sac`` binary entirely is UNKNOWN, and stays silent. The distinction is
the whole tri-state doctrine in one branch.

NOT AN AUTO-REMEDY
------------------
This never upgrades and never restarts anything. An automatic remedy on
a signal this coarse is how you take a fleet down at 3am. It surfaces;
a human or the owning agent acts.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

# One alarm per condition per 6h. The conditions here PERSIST until a
# human acts (a ghost tag stays a ghost), so a short debounce would mean
# an hourly repeat of the same unactioned line — which trains the
# operator to ignore it, and then the next real one is ignored too.
DEBOUNCE_S = 6 * 60 * 60

STATE_PATH = os.path.expanduser(
    "~/.scitex/agent-container/runtime/deploy-freshness-alarm-state.json"
)
NOTIFY_BIN = "/home/ywatanabe/.venv/bin/scitex-notification"
SAC_BIN = os.environ.get("SAC_BIN", "sac")

# Generous: this host sits at load ~60 and the refresh does a PyPI round
# trip plus two subprocesses. A tight timeout here would manufacture a
# false UNKNOWN out of a machine that was merely busy.
REFRESH_TIMEOUT_S = 180


def load_state() -> dict:
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_PATH)


def notify(message: str) -> None:
    """Warning-level notification on the established rail.

    Warning, not a phone call: the operator ruled that a warning is the
    right severity here and can be tightened later. Waking someone at 3am
    for a ghost tag would get the alarm muted, and a muted alarm is worse
    than none because everyone still believes it is watching.
    """
    subprocess.run(
        [
            NOTIFY_BIN,
            "send-notification",
            "--backend",
            "audio",
            "--level",
            "warning",
            message,
        ],
        check=False,
    )


def refresh() -> tuple[str, list[dict]]:
    """Run ``sac freshness refresh --json``.

    Returns ``(state, stale_findings)`` where state is
    ``fresh`` / ``stale`` / ``unknown``. See the module docstring for why
    a missing ``freshness`` subcommand is STALE while a missing ``sac``
    is UNKNOWN.
    """
    try:
        proc = subprocess.run(
            [SAC_BIN, "freshness", "refresh", "--json"],
            capture_output=True,
            text=True,
            timeout=REFRESH_TIMEOUT_S,
        )
    except FileNotFoundError:
        # No sac at all. We cannot conclude anything about a fleet we
        # cannot see. UNKNOWN -> silent.
        return "unknown", []
    except (OSError, subprocess.SubprocessError):
        return "unknown", []

    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        # sac ran but produced no JSON. The overwhelmingly likely cause
        # is a sac too old to HAVE this subcommand (click exits 2 with a
        # usage error on stderr) -- which is itself proof this host is
        # behind. Say so; do not shrug.
        if proc.returncode == 2:
            return "stale", [
                {
                    "check": "sac-too-old",
                    "summary": (
                        "the installed sac has no `freshness` command — it "
                        "predates the deploy-freshness alarm entirely"
                    ),
                    "remedy": "pip install -U scitex-agent-container",
                }
            ]
        return "unknown", []

    state = payload.get("state", "unknown")
    stale = [f for f in payload.get("findings", []) if f.get("state") == "stale"]
    return state, stale


def main() -> None:
    state, stale = refresh()

    # UNKNOWN and FRESH both say nothing. Only positive evidence speaks.
    if state != "stale" or not stale:
        return

    now = time.time()
    saved = load_state()
    spoke = False

    for finding in stale:
        check = finding.get("check", "?")
        last = saved.get(check, {})
        if now - last.get("alarmed_at", 0) < DEBOUNCE_S:
            continue

        message = f"Deploy freshness: {finding.get('summary', check)}."
        remedy = finding.get("remedy")
        if remedy:
            message += f" Fix: {remedy}"
        notify(message)
        saved[check] = {"alarmed_at": now, "summary": finding.get("summary", "")}
        spoke = True

    if spoke:
        save_state(saved)


if __name__ == "__main__":
    main()
