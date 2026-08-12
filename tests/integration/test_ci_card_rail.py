"""The CI feedback rail's invariants (ADR-0024).

Two kinds of test live here, and the second kind is the point.

**Logic tests** cover the decisions the rail makes with no store and no
network: which card id both halves derive, who receives a verdict, what
status a red gate produces.

**Shape tests** assert facts about the WORKFLOW itself, in the spirit of
``test_ci_python_matrix_shape.py``. They exist because every failure this
rail is built to prevent is a silent one, and the two ways to re-open
that hole are both single-line edits to YAML: unpin ``runs-on`` (the
verdict then executes on a machine where loopback is a different host's
daemon and a different postgres) or drop ``if: always()`` (the rail then
only congratulates and never warns). Neither would fail any test that
only exercised Python, and neither would look wrong in review.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_DIR = REPO_ROOT / ".github" / "ci"
GATE_WORKFLOW = (
    REPO_ROOT / ".github" / "workflows" / "pytest-matrix-on-ubuntu-py3-11-3-12-3-13.yml"
)

# The label that must remain on the verdict job. It is carried by exactly
# one runner in the org pool -- the one on the host that runs `sac listen`
# and the card store.
CONTROL_PLANE_LABEL = "sac-control-plane"


def _load(module_name: str):
    """Import a rail module by path.

    The rail deliberately is NOT an installed package: it must run under
    ``uv run --with scitex-cards`` on a runner that has no sac install,
    and from a git hook inside an agent container. So the tests import it
    the same way the runner does -- by file -- rather than pretending it
    is importable as ``scitex_agent_container.something``.
    """
    sys.path.insert(0, str(CI_DIR))
    spec = importlib.util.spec_from_file_location(
        module_name, CI_DIR / f"{module_name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rail():
    return _load("ci_card_rail")


@pytest.fixture(scope="module")
def rail_cards():
    return _load("ci_rail_cards")


@pytest.fixture(scope="module")
def gate_workflow() -> dict:
    return yaml.safe_load(GATE_WORKFLOW.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# the card id is the ONLY channel between the two halves
# ---------------------------------------------------------------------------
def test_card_id_is_derived_from_repo_and_sha_only(rail_cards) -> None:
    """Both halves must reach the same id from the same commit.

    There is no message passed from a developer's git hook to a runner
    job minutes later, so the id has to be a pure function of facts both
    sides independently hold. Anything else -- a branch name, a run id, a
    timestamp -- would make the verdict land on a card nobody watches.
    """
    long_sha = "0123456789abcdef0123456789abcdef01234567"
    from_push = rail_cards.card_id_for("scitex-ai/scitex-agent-container", long_sha)
    from_ci = rail_cards.card_id_for("scitex-agent-container", long_sha)
    assert from_push == from_ci == "ci-scitex-agent-container-0123456789ab"


def test_card_id_distinguishes_pushes_to_the_same_branch(rail_cards) -> None:
    a = rail_cards.card_id_for("r/x", "a" * 40)
    b = rail_cards.card_id_for("r/x", "b" * 40)
    assert a != b, "a verdict belongs to the commit it judged, not to the branch"


# ---------------------------------------------------------------------------
# a red gate must never produce a gate-less `blocked` card
# ---------------------------------------------------------------------------
def test_no_conclusion_maps_to_blocked(rail_cards) -> None:
    """`blocked` without a named gate is invisible work.

    Such a card nudges nobody and is excluded from the runnable count, so
    it sits unseen until somebody goes looking -- the exact state this
    fleet spent 2026-08-11 paying for. A red gate waits on nothing; it is
    a finished run with a bad result, so it is `failed`.
    """
    assert "blocked" not in set(rail_cards.STATUS_FOR_CONCLUSION.values())
    assert rail_cards.STATUS_FOR_CONCLUSION["failure"] == "failed"
    assert rail_cards.STATUS_FOR_CONCLUSION["success"] == "done"


def test_every_terminal_conclusion_has_a_status(rail, rail_cards) -> None:
    """The two tables cannot drift apart without this failing."""
    assert set(rail.TERMINAL_CONCLUSIONS) == set(rail_cards.STATUS_FOR_CONCLUSION)


def test_title_leads_with_the_verdict(rail_cards) -> None:
    title = rail_cards.card_title("scitex-ai/sac", "feat/x", "abcdef1234567890", "failure")
    assert title.startswith("[CI FAILURE]")
    assert "sac" in title and "feat/x" in title and "abcdef12" in title


# ---------------------------------------------------------------------------
# recipient resolution -- the difference between delivered and silent
# ---------------------------------------------------------------------------
def _agent(name: str, *, project: str, reachable: bool, started: str = "2026-08-10"):
    return {
        "name": name,
        "project": project,
        "inbox_reachable": "reachable" if reachable else "unreachable",
        "inbox_subscribers": 1 if reachable else 0,
        "started_at": started,
    }


def test_recipient_prefers_a_reachable_agent_over_a_deaf_one(rail) -> None:
    """The regression that motivated not reusing sac's resolve_owner.

    Two agent specs declare ``project: scitex-agent-container``. sac's
    ``_ci_owner.resolve_owner`` walks ``sorted(glob("*/spec.yaml"))`` and
    returns the first match, and "-" sorts before "/", so
    ``scitex-agent-container-04`` wins -- an agent measured with zero
    inbox subscribers since 2026-08-10. Delivering there raises no error
    anywhere; it simply reaches nobody.
    """
    agents = [
        _agent("scitex-agent-container-04", project="scitex-agent-container", reachable=False),
        _agent("scitex-agent-container", project="scitex-agent-container", reachable=True),
    ]
    who, how = rail.resolve_recipient(card=None, repo="x/scitex-agent-container", agents=agents)
    assert who == "scitex-agent-container"
    assert how == "spec"


def test_recorded_pusher_beats_any_inference(rail) -> None:
    """A record of who pushed outranks a guess from the repo name."""
    agents = [_agent("some-other-agent", project="sac", reachable=True)]
    who, how = rail.resolve_recipient(
        card={"agent": "the-actual-pusher"}, repo="x/sac", agents=agents
    )
    assert (who, how) == ("the-actual-pusher", "card")


def test_unresolvable_recipient_is_reported_not_guessed(rail) -> None:
    who, how = rail.resolve_recipient(card=None, repo="x/nobody-owns-this", agents=[])
    assert who is None and how == "unresolved"


def test_deaf_is_still_chosen_when_it_is_the_only_candidate(rail) -> None:
    """A deaf owner is better than no addressee -- the event is durable.

    ``publish_to_agent`` persists to ``channel_events`` BEFORE publishing,
    so a verdict addressed to a currently-unsubscribed agent is replayed
    on its next connect rather than lost. Refusing to address it would
    throw away a recoverable delivery.
    """
    agents = [_agent("only-one", project="sac", reachable=False)]
    who, _ = rail.resolve_recipient(card=None, repo="x/sac", agents=agents)
    assert who == "only-one"


# ---------------------------------------------------------------------------
# identity resolution
# ---------------------------------------------------------------------------
def test_unexpanded_template_is_not_an_identity(rail, monkeypatch) -> None:
    """``${SCITEX_TODO_AGENT_ID}`` arrives literally in some processes.

    Measured in the cards MCP server on this host. Accepting it would
    file cards owned by an agent whose name is a shell template.
    """
    monkeypatch.setenv("SCITEX_TODO_AGENT_ID", "${SCITEX_TODO_AGENT_ID}")
    monkeypatch.setenv("SAC_NAME", "real-agent")
    assert rail.pushing_agent() == "real-agent"


def test_no_identity_anywhere_returns_none(rail, monkeypatch) -> None:
    for var in rail.AGENT_ID_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    assert rail.pushing_agent() is None


# ---------------------------------------------------------------------------
# the message a woken human reads
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_verdict_text_carries_conclusion_run_and_card(rail, conclusion: str) -> None:
    text = rail.verdict_text(
        repo="scitex-ai/sac",
        branch="feat/x",
        sha="abcdef1234567890",
        conclusion=conclusion,
        leg="pytest-matrix",
        run_url="https://github.com/o/r/actions/runs/1",
    )
    assert conclusion.upper() in text
    assert "https://github.com/o/r/actions/runs/1" in text
    assert rail.card_id_for("scitex-ai/sac", "abcdef1234567890") in text


# ---------------------------------------------------------------------------
# WORKFLOW SHAPE -- the two single-line edits that would silently re-break it
# ---------------------------------------------------------------------------
def test_verdict_job_is_pinned_to_the_control_plane_host(gate_workflow) -> None:
    """ADR-0024 assumed "the self-hosted runners execute on this host".

    They do not. ``vars.CI_RUNS_ON`` is ``scitex-org-cpu``: four runners
    on four machines (scitex-01..04), and only scitex-04-org-cpu-01 sits
    on the box running ``sac listen`` and the card store. Unpinned, the
    verdict job would 75% of the time POST to a different machine's
    loopback and write to a different postgres -- delivering to nobody
    and recording nowhere, with nothing raising an error.
    """
    job = gate_workflow["jobs"]["verdict"]
    runs_on = job["runs-on"]
    assert isinstance(runs_on, list), "must be a literal label list, not vars.CI_RUNS_ON"
    assert CONTROL_PLANE_LABEL in runs_on
    assert "self-hosted" in runs_on


def test_verdict_job_reports_red(gate_workflow) -> None:
    """Without ``always()`` the job is skipped exactly when it matters.

    ``needs: [test]`` alone means a failed gate skips the verdict job, so
    the rail would deliver green verdicts and stay silent on red -- a
    congratulations service, not a feedback rail.
    """
    job = gate_workflow["jobs"]["verdict"]
    assert "always()" in str(job.get("if", ""))
    assert "test" in job["needs"]


def test_verdict_job_passes_the_card_store_explicitly(gate_workflow) -> None:
    """An unset DSN does not fail -- it silently picks a local file.

    A ``run:`` step gets a non-interactive shell that sources no profile,
    so the DSN must come from the workflow. Writing the verdict into a
    store no board reads is the same silent-nobody failure as delivering
    to a deaf agent.
    """
    step = next(
        s for s in gate_workflow["jobs"]["verdict"]["steps"] if "env" in s
    )
    assert "SCITEX_CARDS_DB" in step["env"]
    assert "postgres" in step["env"]["SCITEX_CARDS_DB"]


def test_verdict_step_invokes_the_rail_with_a_conclusion(gate_workflow) -> None:
    run = " ".join(
        s.get("run", "") for s in gate_workflow["jobs"]["verdict"]["steps"]
    )
    assert "ci_card_rail.py verdict" in run
    assert "--conclusion" in run
    # uv by absolute fallback: the runner service's PATH is the system
    # default and does not include ~/.local/bin.
    assert ".local/bin/uv" in run


# EOF
