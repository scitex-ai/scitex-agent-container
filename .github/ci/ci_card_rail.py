#!/usr/bin/env python3
"""The CI feedback rail: a push registers a card, CI's verdict is written
onto that card, and the update is delivered to the agent that pushed.

ADR-0024 is the design. The operator's words (2026-08-12) describe a
MECHANISM rather than a wish —

    プッシュに連動して、必ずフックでカードにその情報を書かないといけない
    …GitHub から信号が帰ってきたときに、フックでカードに書き込む、
    それがエージェントに通知が行く

— so there are exactly two entry points, sharing exactly one card:

    push     ← the git ``pre-push`` hook, in the pushing agent's container
    verdict  ← a job on the runner that sits on the control-plane host

This file orchestrates. :mod:`ci_rail_cards` owns the card contract and
the store guard; :mod:`ci_rail_listen` owns delivery. The split is by
failure domain, not by size: a store that is silently the wrong database
and a bus that silently reaches nobody are different bugs with different
evidence, and each module carries the measurements for its own.

DEPENDENCIES ARE DELIBERATELY THIN — standard library, plus a lazy
``scitex_cards`` import for the card write. No ``scitex_agent_container``
import: this runs under ``uv run --with scitex-cards`` on a runner with
no sac installed, and from a git hook inside an agent container. Every
sac fact is fetched over its loopback HTTP control plane instead.

FAILING LOUDLY IS THE FEATURE. On the ``verdict`` path every
unrecoverable condition — unreachable daemon, non-2xx, unresolvable
recipient, refused card write, wrong store — exits non-zero and turns the
step red, because a notification rail that fails quietly is the original
defect wearing a fix. The single deliberate non-failure is a delivered
count of zero: the event is already persisted and will replay, so that is
a warning naming the RECIPIENT as the fault rather than a red step
blaming the rail. The ``push`` path inverts the trade-off and never
blocks a push — a card-store hiccup must not cost an engineer their work,
and the verdict half creates the card when it finds none.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_rail_cards import (  # noqa: E402 — sibling module, path fixed up above
    BLOCKER_CLEARED,
    STATUS_FOR_CONCLUSION,
    VERDICT_ACTOR,
    card_id_for,
    card_title,
    cards,
    get_card,
    now_stamp,
    record_push,
    repo_basename,
    resolved_store_dsn,
    upsert_card,
)
from ci_rail_failure import summarize_failures  # noqa: E402 — sibling module
from ci_rail_message import (  # noqa: E402 — sibling module
    sibling_workflow_names,
    verdict_text,
)
from ci_rail_listen import (  # noqa: E402 — sibling module, path fixed up above
    TOKEN_DIR,
    fleet_agents,
    listen_base_url,
    listen_token,
    notify_agent,
    reachability_of,
)

# A verdict is only reported for conclusions that ARE a verdict. A
# cancelled run means a newer push superseded this one (the gate sets
# ``cancel-in-progress``), not a statement about the code; reporting it
# would train the fleet to ignore this rail.
TERMINAL_CONCLUSIONS = frozenset({"success", "failure"})

# The identity of the pushing agent, in precedence order.
# ``$SCITEX_TODO_AGENT_ID`` is the card package's own canonical variable
# and so leads — but it is NOT reliably present: measured unset in this
# container's shell, and present in the cards MCP server process as the
# literal, unexpanded string ``${SCITEX_TODO_AGENT_ID}``. The sac-side
# names are set by the runtime that launched the container and were
# measured correct, so they are working fallbacks rather than decoration.
# An owner-less card is REJECTED by the store — there is no silent
# fallback there — so resolving this is what makes the push half work.
AGENT_ID_ENV_VARS = (
    "SCITEX_TODO_AGENT_ID",
    "SAC_NAME",
    "SCITEX_AGENT_CONTAINER_AGENT",
    "CLAUDE_AGENT_ID",
)

__all__ = [
    "TERMINAL_CONCLUSIONS",
    "main",
    "pushing_agent",
    "record_verdict",
    "resolve_recipient",
    "verdict_text",
]


def pushing_agent() -> str | None:
    """First non-empty, non-template agent identity from the environment."""
    for var in AGENT_ID_ENV_VARS:
        value = (os.environ.get(var) or "").strip()
        # Reject an unexpanded shell template rather than filing cards
        # owned by an agent literally named "${SCITEX_TODO_AGENT_ID}".
        if value and not value.startswith("${"):
            return value
    return None


def _warn(msg: str) -> None:
    """A GitHub warning annotation that stays readable off-CI."""
    print(f"::warning::{msg}", flush=True)


def _die(msg: str, code: int = 1) -> int:
    """Fail loudly: an error annotation AND a non-zero exit — both.

    The annotation is what a human sees on the run page; the exit code is
    what turns the step red so the run page gets looked at at all.
    """
    print(f"::error::{msg}", file=sys.stderr, flush=True)
    print(f"::error::{msg}", flush=True)
    return code


def resolve_recipient(
    *, card: dict[str, Any] | None, repo: str, agents: list[dict[str, Any]]
) -> tuple[str | None, str]:
    """Who should hear this verdict? Returns ``(name, how_it_was_decided)``.

    Precedence, and the reason for each step:

    1. **The card's own agent**, set by ``pre-push`` to whoever actually
       pushed. A verdict belongs to the pusher, and no inference beats a
       record of the fact.
    2. **An agent spec whose ``project`` matches the repo, PREFERRING one
       that is reachable right now.** sac's ``_ci_owner.resolve_owner``
       makes the same match but takes the first hit in sorted filename
       order, which on this very repo deterministically selects
       ``scitex-agent-container-04`` — zero inbox subscribers since
       2026-08-10. Sorting reachable candidates first is the whole
       difference between a delivered verdict and a silent one, and is
       why this does not simply call that function.

    ``None`` means nobody could be resolved; the caller must treat that
    as an error, never as a quiet skip.
    """
    if card:
        named = card.get("agent") or card.get("assignee")
        if isinstance(named, str) and named.strip():
            return named.strip(), "card"

    base = repo_basename(repo)
    matches = [a for a in agents if str(a.get("project", "")).strip() == base]
    if not matches:
        return None, "unresolved"
    matches.sort(
        key=lambda a: (
            a.get("inbox_reachable") != "reachable",
            -int(a.get("inbox_subscribers") or 0),
            str(a.get("started_at") or ""),
        )
    )
    name = matches[0].get("name")
    return (str(name) if name else None), "spec"



def record_verdict(
    *,
    repo: str,
    branch: str,
    sha: str,
    conclusion: str,
    leg: str,
    run_url: str,
    token: str,
    run_id: str = "",
    workflow: str = "",
) -> int:
    """Write the verdict onto the card, then deliver it. Returns an exit code."""
    pkg = cards()
    card_id = card_id_for(repo, sha)
    # Print the store BEFORE writing. Two databases on this host answer
    # to the same store_uuid, so "which one did CI write to" is a real
    # question a reader will have, and the run log is the only place it
    # can be answered after the fact.
    print(f"card store: {resolved_store_dsn()}", flush=True)

    agents = fleet_agents(token)
    card = get_card(pkg, card_id)
    if card is None:
        _warn(
            f"no card for {card_id} — the pre-push hook did not run for "
            f"{sha[:8]}. Creating one now so the verdict is still recorded."
        )
    recipient, how = resolve_recipient(card=card, repo=repo, agents=agents)
    if not recipient:
        return _die(
            f"no recipient for {repo}@{sha[:8]}: the card names no agent and no "
            f"agent spec declares project={repo_basename(repo)!r}. A verdict "
            "with no addressee is exactly the silent-nobody failure this rail "
            "exists to remove, so this step fails instead of dropping it."
        )

    # RECORD FIRST, DELIVER SECOND. The card is the durable statement of
    # what CI decided; the notification is a courtesy copy. If delivery
    # fails, the verdict is still on the board — which is not true in the
    # other order.
    # Only a red verdict needs the log read; a green one has nothing to
    # name and the API call would be pure latency.
    detail = summarize_failures(repo, run_id) if conclusion == "failure" else ""
    body = verdict_text(
        repo=repo,
        branch=branch,
        sha=sha,
        conclusion=conclusion,
        leg=leg,
        run_url=run_url,
        detail=detail,
        card_id=card_id,
        # Derived from the checkout at verdict time, so a workflow added
        # later cannot silently drop out of the disclaimer.
        unobserved=sibling_workflow_names(workflow),
    )
    upsert_card(
        pkg,
        card_id,
        # A runner's `run:` step carries NO agent identity, and the store
        # refuses to invent a creator. Without this the verdict half
        # cannot create a card -- the path taken whenever the pre-push
        # hook did not run, i.e. every human push and every repo where
        # the hook is not installed. The rail would fail precisely where
        # it is meant to be the safety net.
        create_only={"created_by": VERDICT_ACTOR},
        title=card_title(repo, branch, sha, conclusion),
        status=STATUS_FOR_CONCLUSION[conclusion],
        # CLOSE THE LOOP. The push half parked this card as blocked on
        # `compute` (waiting for a machine). If the verdict did not clear
        # that, every pushed commit would leave behind a card blocked
        # forever on a gate that has already opened -- this rail's own
        # failure mode, reproduced one level up and at one card per push.
        blocker=BLOCKER_CLEARED,
        kind="task",
        repo=repo_basename(repo),
        project=repo_basename(repo),
        agent=recipient,
        assignee=recipient,
        note=(
            f"CI {conclusion} for {sha[:8]} ({leg or 'gate'}) at {now_stamp()} "
            f"— {run_url}"
        ),
        last_activity=now_stamp(),
    )
    pkg.comment_task(task_id=card_id, text=body, by="ci")
    # READ BACK THE FIELD, NOT THE CARD'S EXISTENCE. A presence check
    # passes straight through the failure this guards against.
    #
    # The shared board takes its mutex as an flock on a file INSIDE each
    # container, while the cards live in one shared postgres. Two agents
    # each lock their own copy, both succeed instantly, and both do
    # read-whole-store -> mutate -> write-whole-store. Last writer wins
    # and silently reverts the other -- measured on the live store, where
    # a `complete_task` that RETURNED done was later found blocked,
    # undone by writes to entirely unrelated cards. The store's own
    # shrink guard compares the set of IDS, so it sees a card VANISH but
    # never a card REGRESS, and the confirmed loss passed it cleanly.
    #
    # That failure is this rail's thesis turned against it: a verdict
    # written and silently reverted is INDISTINGUISHABLE from a verdict
    # never written -- the card exists, looks like ours, and says the
    # wrong thing. So verify the VALUE, rewrite once, and if the store
    # still disagrees, fail loudly rather than notify anyone about a
    # record that has already evaporated.
    expected = STATUS_FOR_CONCLUSION[conclusion]
    stored = (get_card(pkg, card_id) or {}).get("status")
    if stored != expected:
        _warn(
            f"card {card_id}: wrote status={expected!r} but the store reads "
            f"{stored!r} — a concurrent writer clobbered it. Rewriting once."
        )
        upsert_card(
            pkg,
            card_id,
            title=card_title(repo, branch, sha, conclusion),
            status=expected,
            blocker=BLOCKER_CLEARED,
            last_activity=now_stamp(),
        )
        stored = (get_card(pkg, card_id) or {}).get("status")
        if stored != expected:
            return _die(
                f"card {card_id}: status reads {stored!r} after writing "
                f"{expected!r} twice. The verdict is NOT durably recorded, so "
                "this fails rather than announcing a record that does not exist."
            )
    print(f"card {card_id}: recorded {conclusion} (status={expected}, read back)")

    reach, subs = reachability_of(agents, recipient)
    print(
        f"recipient {recipient} (via {how}); inbox_reachable={reach} subscribers={subs}",
        flush=True,
    )

    try:
        result = notify_agent(token, agent=recipient, body=body, card_id=card_id)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        return _die(f"POST /v1/notify -> HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return _die(f"POST /v1/notify to {listen_base_url()} failed: {exc}")

    delivered = int(result.get("delivered_subscriber_count") or 0)
    print(
        f"notified {recipient}: msg_id={result.get('msg_id')} "
        f"delivered_subscriber_count={delivered}",
        flush=True,
    )
    if delivered == 0:
        # NOT an error, and the distinction is the entire point. The event
        # is already persisted in ``channel_events``; a zero count means
        # the RECIPIENT is deaf right now, not that the RAIL is broken.
        # Name which, so nobody spends a night debugging the other one.
        _warn(
            f"{recipient} has no live inbox subscriber (inbox_reachable={reach}). "
            "The verdict is persisted and replays on its next connect, but it "
            "did NOT reach a live session. This is a RECIPIENT fault, not a rail "
            "fault: the card and the channel_events row both exist."
        )
    return 0


def _cmd_push(args: argparse.Namespace) -> int:
    """Never blocks a push: loud on stderr, exit 0 regardless.

    A push must not fail because a card store hiccupped, and it need not
    — the verdict half creates the card when it finds none, so the worst
    case here is a verdict routed by agent spec instead of recorded fact.
    """
    try:
        record_push(
            repo=args.repo,
            branch=args.branch,
            sha=args.sha,
            agent=args.agent.strip() or pushing_agent(),
            subject=args.subject or "",
        )
    except Exception as exc:  # noqa: BLE001 — see the docstring
        print(
            f"ci_card_rail: push NOT recorded on a card ({exc}); the push "
            "itself is unaffected and CI will create the card instead.",
            file=sys.stderr,
            flush=True,
        )
        return 0
    print(
        f"ci_card_rail: recorded push {args.sha[:8]} on "
        f"{card_id_for(args.repo, args.sha)}"
    )
    return 0


def _cmd_verdict(args: argparse.Namespace) -> int:
    conclusion = (args.conclusion or "").strip().lower()
    if conclusion not in TERMINAL_CONCLUSIONS:
        print(
            f"ci_card_rail: conclusion {conclusion!r} is not a verdict "
            "(cancelled/skipped means a superseded push) — nothing reported.",
            flush=True,
        )
        return 0
    token = listen_token()
    if not token:
        return _die(
            "no sac listen bearer token: set $SAC_LISTEN_BEARER, or provide "
            f"~/{TOKEN_DIR}/listen-<host>.token. Refusing to skip delivery "
            "silently."
        )
    try:
        return record_verdict(
            repo=args.repo,
            branch=args.branch,
            sha=args.sha,
            conclusion=conclusion,
            leg=args.leg or "",
            run_url=args.run_url or "",
            token=token,
            run_id=args.run_id or "",
            workflow=args.workflow or "",
        )
    except ImportError as exc:
        return _die(f"scitex_cards is not importable on this runner: {exc}")
    except Exception as exc:  # noqa: BLE001 — a rail that fails quietly is the bug
        return _die(f"verdict for {args.repo}@{args.sha[:8]} not recorded: {exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ci_card_rail", description="push -> card -> CI verdict -> agent"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    push = sub.add_parser("push", help="record a push on its card (git pre-push hook)")
    push.add_argument("--repo", required=True)
    push.add_argument("--branch", required=True)
    push.add_argument("--sha", required=True)
    push.add_argument("--agent", default="")
    push.add_argument("--subject", default="")
    push.set_defaults(func=_cmd_push)

    verdict = sub.add_parser(
        "verdict", help="write CI's verdict to the card and notify the agent"
    )
    verdict.add_argument("--repo", required=True)
    verdict.add_argument("--branch", required=True)
    verdict.add_argument("--sha", required=True)
    verdict.add_argument("--conclusion", required=True)
    verdict.add_argument("--leg", default="")
    verdict.add_argument("--run-url", dest="run_url", default="")
    verdict.add_argument("--run-id", dest="run_id", default="")
    # The workflow this rail OBSERVES; every other workflow in the
    # checkout becomes the "cannot see" list on a green verdict.
    verdict.add_argument("--workflow", default="")
    verdict.set_defaults(func=_cmd_verdict)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

# EOF
