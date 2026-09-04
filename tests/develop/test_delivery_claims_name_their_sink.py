#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A `stx-allow` reason that claims delivery must NAME the sink.

WHY THIS GATE EXISTS
====================
2026-08-19: three of the operator's Telegram messages were delivered to the
WRONG agent's session over several hours, and nobody noticed. The guard that
should have prevented it existed and FIRED CORRECTLY — `ensure_port_free_or_raise`
refused to bind a port another agent held, naming the port and the holder's
pid. The exception was then caught here:

    runtimes/_tui_bridge_seam.py
        except Exception as exc:  # stx-allow: fallback (reason: ... ; logged
                                  # for the operator)
            logging.getLogger(__name__).warning(...)

"logged for the operator" is a CHECKABLE CLAIM, and it was false: a full-text
search of the runtime tree found that warning in no log at all.

WHAT THIS GATE CHECKS, AND WHAT IT DELIBERATELY DOES NOT
========================================================
The repo already has TWO working layers here, and neither is the gap:
  1. a lint rule that flags fallbacks — it fires;
  2. an escape hatch that DEMANDS a written reason — it is obeyed, 1244 times.
The missing third question is whether the stated reason is TRUE. In general
that is not machine-checkable. But one subset is: a claim that something
REACHES SOMEWHERE can name where, and a named path can be opened.

So: if a `stx-allow` reason says the failure is logged / alerted / reported /
notified / surfaced / told, it must also name a sink — a path, a log file, a
channel, a card. Prose stays free; only the delivery promise is constrained.

THE PREDICATE IS "CLAIMS SOMETHING REACHES SOMEWHERE", not "contains the word
logged". cct's own near-miss on the same night read "silence becomes
impossible" — a delivery guarantee that no logging-verb filter would catch.
The vocabulary below is therefore deliberately wider than logging verbs, and
widening it further is a correct change, not a scope creep.

WHY A NAMED PATH AND NOT AN HONEST VERB
=======================================
"logged for the operator" was plausibly TRUE when written. The defect is
DRIFT: the claim outlived the sink it described, and nothing could notice
because "logged" has no referent to re-check. `runtime/logs/turn-bridge.log`
has one. That is the entire difference this gate buys.

THIS GATE DOES NOT DEPEND ON THE LOGGING MIGRATION
==================================================
src/ imports stdlib `logging` in 154 files and `scitex_logging` in 13. A
check built on scitex_logging would see 8% of the codebase today. This one
reads COMMENT TEXT, so it covers all 361 files with `stx-allow` regardless of
which library they use — and it must NOT later be "improved" into a
scitex_logging-based check, which would silently shrink its coverage twelve-fold.

IT DOES NOT TRY TO REDUCE 1244
==============================
Whether a swallow-if-you-explain hatch should exist at all is a larger and
separate argument. Mixing it in would sink both. This gate freezes the
falsifiable subset and stops.

SHRINKING IS TWO LINES; GROWING IS IMPOSSIBLE
=============================================
Name the sink in the comment, delete the entry here. A 70th unnamed claim
fails. The asymmetry is the whole point, and it is the reason to prefer a
shrink-only list: a list that can only shrink is an inventory; one that can
grow is a wish.

WHAT A PASS DOES **NOT** PROVE — READ THIS BEFORE TRUSTING A GREEN
==================================================================
This gate checks the FORM of the claim, not its TRUTH. A reason that names
`runtime/logs/turn-bridge.log` passes even if nothing has ever written one
byte there. So `_tui_bridge_seam.py` — the site that motivated this entire
gate — would pass the moment someone appends a plausible path, and would then
carry a GREEN CHECK beside a claim that is still false. That is worse than
the original in one specific way, and it is the reason this paragraph exists.

Raised by claude-code-telegrammer while reviewing the gate, and it is the
same shape one turn further in: "documented and justified" was not "does
anything enforce it", and "names a sink" is not "the sink receives".

Closure, cheapest first, none of it done here:
  1. assert the named path sits under a known log root — grep-level, catches
     inventions and typos. NOT DONE: the 69 frozen reasons use several roots
     and enumerating them honestly needs a survey, not a guess.
  2. assert something in the tree actually writes to that path.
  3. the only one that proves delivery: emit a line there in a test and read
     it back. Same shape as the two-ended probe agreed for the turn bridge —
     a claim about ARRIVAL can only be settled at the receiving end.

SCOPE, STATED SO THE SUMMARY CANNOT DRIFT
=========================================
1244 `stx-allow` escape hatches exist. 73 claim delivery; 69 of those name no
sink and are frozen here. THE OTHER 1175 ARE NOT CLEAN — they are merely
making claims that are not falsifiable, and this gate says nothing whatever
about them. "The fallback problem is fixed" is the sentence this will collapse
into if the scope is not written down, and it would be false.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

#: Claims that SOMETHING REACHES SOMEWHERE. Wider than logging verbs on
#: purpose — see the module docstring.
_CLAIMS_DELIVERY = re.compile(
    r"\b(logged|logs|logging|alerted|alarm|reported|notified|notify|surfaced|"
    r"raised as a card|the operator will see|reaches the operator|told)\b",
    re.I,
)

#: A NAMED sink: a path, a log file, a channel, a card, a stream.
_NAMES_A_SINK = re.compile(
    r"(runtime/logs/|\.log\b|~/\.scitex/|\$HOME/|/var/log/|telegram|"
    r"scitex-cards|a2a|journald|journalctl|stderr|stdout|card\b)",
    re.I,
)

_ESCAPE_HATCH = re.compile(r"stx-allow:")

#: MEASURED 2026-08-19. Every `stx-allow` line whose reason claims delivery
#: and names no sink. 73 lines claimed delivery; 4 already named a sink.
FROZEN_UNNAMED_CLAIMS = frozenset(
    {
        "scitex_agent_container/_account/claude_usage.py:381",
        "scitex_agent_container/_account/claude_usage.py:540",
        "scitex_agent_container/_account/interactive_login.py:262",
        "scitex_agent_container/_account/openai_usage.py:333",
        "scitex_agent_container/_account/openai_usage.py:407",
        "scitex_agent_container/_account/refresh_alarm.py:75",
        "scitex_agent_container/_agentstate/_journal.py:226",
        "scitex_agent_container/_authheal/_pass.py:283",
        "scitex_agent_container/_authheal/_pass.py:431",
        # _birth_certificate.py LEFT THIS SET 2026-08-20 — the reason now
        # names its sink (journald via sac-listen.service for a brokered
        # start, the caller's stderr for a direct one) and carries the
        # journalctl invocation that checks it. Measured before writing:
        # sac-listen.service is StandardOutput=journal, and per-agent logs
        # live under runtime/logs/<agent>/ — asserted rather than assumed,
        # because a NAMED sink that is wrong is worse than an unnamed one.
        # _github_ci_poll_loop.py:160 LEFT THIS LIST 2026-08-20. Its sink is now
        # named in the reason itself (journald via sac-listen.service). It came
        # off the list the hard way: this change shifted the claim from line 160
        # to 170, which rotted the entry AND made the same claim reappear as a
        # NEW unnamed one — two failures from one insertion, and green locally
        # because the frozen list is keyed on a line number that only moves when
        # the file is edited. That is the guard working, not misfiring: an entry
        # pinned by line is a claim about a LOCATION, and the location changed.
        "scitex_agent_container/_lifecycle/_in_sif_http_client.py:141",
        # _instances.py LEFT THIS SET 2026-08-28. Its claim was frozen at
        # :263 as an unnamed one, and it was twice RE-PINNED (:265, then :267
        # by the a2a-ports migration, whose added comment moved it again) on
        # the reasoning that a bare `except: pass` has no sink to name.
        # That reasoning was right about the code and wrong about what to do:
        # the review then proved the swallow was hiding a live TypeError (a
        # stale `db_path=` kwarg) on EVERY spec-driven start. The honest fix
        # was never to re-pin the debt — it was to give the site a sink. Both
        # handlers now log (warning for a name collision, error for anything
        # else) and NAME where it lands, so the claim is checkable and the
        # entry is gone rather than moved. Verified on the merged file: the
        # a2a-ports comment still shifts the lines, and it no longer matters,
        # because a claim that names its sink is not this gate's business at
        # any line number.
        "scitex_agent_container/_lifecycle/_listen_client_resolve.py:178",
        "scitex_agent_container/_lifecycle/_orphan_mcp_cleanup.py:227",
        "scitex_agent_container/_lifecycle/_prune_runtime.py:80",
        "scitex_agent_container/_lifecycle/_relocate_transcript.py:172",
        "scitex_agent_container/_lifecycle/_restart_client.py:142",
        "scitex_agent_container/_lifecycle/_sdk_heartbeat_loop.py:188",
        "scitex_agent_container/_lifecycle/_sdk_heartbeat_loop.py:289",
        "scitex_agent_container/_lifecycle/_sdk_heartbeat_loop.py:328",
        "scitex_agent_container/_lifecycle/_tui_bridge_supervisor.py:206",
        "scitex_agent_container/_lifecycle/_tui_bridge_supervisor.py:230",
        "scitex_agent_container/_lifecycle/_tui_heartbeat_loop.py:230",
        "scitex_agent_container/_lifecycle/_tui_heartbeat_loop.py:346",
        "scitex_agent_container/_lifecycle/_tui_heartbeat_loop.py:375",
        "scitex_agent_container/_listen/_deploy_freshness.py:227",
        "scitex_agent_container/_listen/_liveness_tick.py:123",
        # _node_channel_forwarders.py LEFT THIS SET 2026-09-02 — the reason
        # now names where the tolerated non-JSON body goes (the a2a response
        # the sender receives) instead of claiming it is "surfaced".
        "scitex_agent_container/_maintenance/_install_integrity_pointers.py:193",
        "scitex_agent_container/_maintenance/_install_integrity_pointers.py:226",
        "scitex_agent_container/_maintenance/_venv_dist_assertion.py:120",
        "scitex_agent_container/_mcp/_channel_post_deliver.py:120",
        "scitex_agent_container/_mcp/_channel_post_deliver.py:99",
        # THESE FOUR MOVED, they did not change. The dispatch-ledger port to
        # PostgreSQL (2026-08-28) added prose above each of them, so the
        # coordinates shifted 107->115, 329->331, 46->62 and 59->79. The
        # `stx-allow` reasons themselves are byte-for-byte the ones frozen
        # before, and no new unnamed claim was introduced.
        #
        # RE-PINNED RATHER THAN CLOSED, deliberately. Closing one means
        # writing a path into the comment, and this file's own docstring says
        # a plausible path that nothing writes to is WORSE than the honest
        # unnamed claim, because it buys a green check. These four log through
        # `logging.getLogger(__name__)`; where that lands for a container
        # agent is a survey nobody has done, so naming it here would be a
        # guess wearing a receipt.
        "scitex_agent_container/_mcp/_channel_reaction_ack.py:115",
        "scitex_agent_container/_mcp/channel.py:331",
        "scitex_agent_container/_network/_peer_dispatch.py:62",
        "scitex_agent_container/_network/_peer_dispatch.py:79",
        "scitex_agent_container/_network/probe.py:457",
        "scitex_agent_container/_reconcile/_budget.py:164",
        "scitex_agent_container/_reconcile/_pass.py:370",
        "scitex_agent_container/_reconcile/_pass.py:428",
        "scitex_agent_container/_reconcile/_perform.py:96",
        "scitex_agent_container/_runners/_codex_turn_driver.py:169",
        "scitex_agent_container/_runners/_openai_turn_driver.py:240",
        "scitex_agent_container/_runners/_session_completion.py:183",
        "scitex_agent_container/_runners/_session_conversation.py:262",
        "scitex_agent_container/_runners/_session_hooks.py:225",
        "scitex_agent_container/_runners/_session_http.py:244",
        "scitex_agent_container/_runners/_tmux/claude_code.py:516",
        "scitex_agent_container/_state/_acl_broker_client.py:130",
        "scitex_agent_container/_state/snapshot/_io.py:411",
        "scitex_agent_container/a2a/executors/_base.py:97",
        "scitex_agent_container/cli_pkg/_account_refresh_push.py:99",
        "scitex_agent_container/cli_pkg/_agents_cct_audit.py:101",
        "scitex_agent_container/cli_pkg/_helpers/_agent_list_fleet_probe.py:157",
        "scitex_agent_container/cli_pkg/_helpers/_agent_list_fleet_probe.py:186",
        "scitex_agent_container/cli_pkg/_helpers/_agent_list_fleet_probe.py:272",
        "scitex_agent_container/cli_pkg/build_cmds.py:316",
        "scitex_agent_container/cli_pkg/hook_cmds.py:46",
        # _tui_outbound.py and _tui_turn_bridge.py LEFT THIS SET 2026-08-20 —
        # both reasons now name their sink. MEASURED before writing it, because
        # a NAMED sink that is wrong is worse than an unnamed one:
        # _logging/__init__.py documents scitex-logging fanning out to stderr
        # AND a rotating file under ~/.scitex/logging/runtime/, and that
        # directory holds live scitex-<date>.log files (plus .1/.2 rotations)
        # on both this container and its host. It is NOT
        # ~/.scitex/agent-container/runtime/logs/ — that one holds
        # shell-redirect logs (creds-watch.log, host_exec.log, build logs) and
        # is the directory I would have named had I guessed from the tree.
        "scitex_agent_container/runtimes/_apptainer_auth_bind.py:296",
        "scitex_agent_container/runtimes/_cct_rail_alarm.py:198",
        "scitex_agent_container/runtimes/_cct_rail_verdict.py:242",
        # _openai_sdk_common.py:179 LEFT THIS SET 2026-08-29, by DELETION
        # rather than by naming a sink. The claim was "surfaced by
        # vendor session open instead" on an `except OSError: pass` guarding
        # the mkdir of the session-db parent directory — an honest claim while
        # a session db was a FILE. sac's OpenAI runner now keeps conversation
        # state in PostgreSQL (`_state/openai_session_store.py`), so there is
        # no directory to create, no open to surface anything, and the whole
        # helper went with the file. An entry whose line no longer exists must
        # leave the list, or it becomes a blessed coordinate for whatever
        # drifts into position 179.
        "scitex_agent_container/runtimes/_tui_bridge_seam.py:40",
        "scitex_agent_container/runtimes/_tui_inject.py:92",
        "scitex_agent_container/runtimes/_tui_turn_bridge_lifecycle.py:196",
    }
)


def _unnamed_delivery_claims() -> set[str]:
    """Every `stx-allow` line claiming delivery without naming a sink."""
    found: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if not _ESCAPE_HATCH.search(line):
                continue
            if not _CLAIMS_DELIVERY.search(line):
                continue
            if _NAMES_A_SINK.search(line):
                continue
            found.add(f"{path.relative_to(SRC)}:{lineno}")
    return found


def test_the_scanner_still_detects_the_incident_that_motivated_it():
    """POSITIVE CONTROL. Without this, an empty result reads as compliance.

    The regexes are the whole gate. If a refactor breaks one, every other
    test here passes vacuously and the gate reports a clean codebase. So
    assert on a line we KNOW is a delivery claim with no sink.
    """
    # Arrange
    known_bad = 'except Exception:  # stx-allow: fallback (reason: x; logged for the operator)'
    # Act
    claims = bool(_CLAIMS_DELIVERY.search(known_bad))
    names = bool(_NAMES_A_SINK.search(known_bad))
    # Assert
    assert claims and not names


def test_a_named_sink_satisfies_the_gate():
    """The other half of the control: a compliant line must PASS.

    A predicate that rejects everything would also make the frozen set
    unshrinkable, and the failure would look like diligence.
    """
    # Arrange
    good = 'except Exception:  # stx-allow: fallback (reason: x; logged to runtime/logs/turn-bridge.log)'
    # Act
    claims = bool(_CLAIMS_DELIVERY.search(good))
    names = bool(_NAMES_A_SINK.search(good))
    # Assert
    assert claims and names


def test_no_new_unnamed_delivery_claim():
    """A 70th unnamed claim fails. This is the operator-facing rule."""
    # Arrange
    frozen = FROZEN_UNNAMED_CLAIMS
    # Act
    current = _unnamed_delivery_claims()
    # Assert
    new = current - frozen
    assert not new, (
        "These `stx-allow` reasons claim the failure is logged/alerted/"
        "reported but name no sink. Name the path or channel in the comment "
        "(e.g. 'logged to runtime/logs/<name>.log') so the claim can be "
        "re-checked later:\n  " + "\n  ".join(sorted(new))
    )


def test_the_frozen_list_does_not_rot():
    """An entry that no longer matches must be REMOVED from the list.

    Line numbers move. A stale entry silently grants permission to whatever
    line drifts into that position, which is how an allowlist becomes a set
    of blessed coordinates rather than an inventory. Removing it is the same
    two-line change as fixing one.
    """
    # Arrange
    frozen = FROZEN_UNNAMED_CLAIMS
    # Act
    current = _unnamed_delivery_claims()
    # Assert
    stale = frozen - current
    assert not stale, (
        "These entries no longer match — the claim was fixed, or the line "
        "moved. Delete them from FROZEN_UNNAMED_CLAIMS:\n  "
        + "\n  ".join(sorted(stale))
    )


# EOF
