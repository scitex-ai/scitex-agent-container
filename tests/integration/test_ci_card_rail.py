"""The CI feedback rail's invariants (ADR-0024).

Two kinds of test live here, and the second kind is the point.

**Logic tests** cover decisions the rail makes with no store and no
network: which card id both halves derive, who receives a verdict, what
status a red gate produces.

**Shape tests** assert facts about the WORKFLOW, in the spirit of
``test_ci_python_matrix_shape.py``. They exist because every failure this
rail prevents is a silent one, and the ways to re-open that hole are all
single-line YAML edits: unpin ``runs-on`` (the verdict then runs where
loopback is a different host's daemon and a different postgres), drop
``if: always()`` (the rail then only congratulates), or drop the explicit
DSN (the store silently becomes a local file). None would fail a test
that only exercised Python, and none would look wrong in review.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_DIR = REPO_ROOT / ".github" / "ci"
GATE_WORKFLOW = (
    REPO_ROOT / ".github" / "workflows" / "pytest-matrix-on-ubuntu-py3-11-3-12-3-13.yml"
)

# Carried by exactly one runner in the org pool: the machine that runs
# `sac listen` and the card store.
CONTROL_PLANE_LABEL = "sac-control-plane"
LONG_SHA = "0123456789abcdef0123456789abcdef01234567"


def _load(module_name: str):
    """Import a rail module by path, the same way the runner does.

    The rail is deliberately NOT an installed package: it runs under
    ``uv run --with scitex-cards`` on a runner with no sac install, and
    from a git hook inside an agent container.
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


@pytest.fixture(scope="module")
def verdict_job(gate_workflow) -> dict:
    return gate_workflow["jobs"]["verdict"]


@pytest.fixture(scope="module")
def verdict_run_script(verdict_job) -> str:
    return " ".join(step.get("run", "") for step in verdict_job["steps"])


@pytest.fixture(scope="module")
def verdict_env(verdict_job) -> dict:
    return next(step["env"] for step in verdict_job["steps"] if "env" in step)


@pytest.fixture
def clean_agent_env():
    """Real environment, saved and restored -- no monkeypatch.

    ``pushing_agent`` reads ``os.environ`` directly, which is what it
    does in production, so the test drives the real thing.
    """
    names = (
        "SCITEX_TODO_AGENT_ID",
        "SAC_NAME",
        "SCITEX_AGENT_CONTAINER_AGENT",
        "CLAUDE_AGENT_ID",
    )
    saved = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    yield os.environ
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _agent(name: str, *, project: str, reachable: bool, started: str = "2026-08-10"):
    return {
        "name": name,
        "project": project,
        "inbox_reachable": "reachable" if reachable else "unreachable",
        "inbox_subscribers": 1 if reachable else 0,
        "started_at": started,
    }


# ---------------------------------------------------------------------------
# the card id is the ONLY channel between the two halves
# ---------------------------------------------------------------------------
def test_card_id_ignores_the_repo_owner_prefix(rail_cards) -> None:
    """Both halves must reach the same id from the same commit.

    Nothing is passed from a developer's git hook to a runner job that
    starts minutes later, so the id must be a pure function of facts both
    sides already hold -- and the two sides spell the repo differently.
    """
    # Arrange
    owner_qualified = "scitex-ai/scitex-agent-container"
    # Act
    from_push = rail_cards.card_id_for(owner_qualified, LONG_SHA)
    # Assert
    assert from_push == rail_cards.card_id_for("scitex-agent-container", LONG_SHA)


def test_card_id_uses_twelve_sha_characters(rail_cards) -> None:
    # Arrange
    repo = "scitex-ai/scitex-agent-container"
    # Act
    card_id = rail_cards.card_id_for(repo, LONG_SHA)
    # Assert
    assert card_id == "ci-scitex-agent-container-0123456789ab"


def test_card_id_distinguishes_pushes_to_the_same_branch(rail_cards) -> None:
    """A verdict belongs to the commit it judged, not to the branch."""
    # Arrange
    first, second = "a" * 40, "b" * 40
    # Act
    ids = {rail_cards.card_id_for("r/x", first), rail_cards.card_id_for("r/x", second)}
    # Assert
    assert len(ids) == 2


# ---------------------------------------------------------------------------
# card status: never an unnamed gate, never a phantom runnable
# ---------------------------------------------------------------------------
def test_a_red_gate_is_never_filed_as_blocked(rail_cards) -> None:
    """`blocked` demands a gate, and no blocker describes a red suite.

    A gate-less blocked card nudges nobody and leaves the runnable count
    -- how 21 operator decisions sat invisible for weeks.
    """
    # Arrange
    statuses = rail_cards.STATUS_FOR_CONCLUSION
    # Act
    values = set(statuses.values())
    # Assert
    assert "blocked" not in values


def test_a_failed_run_files_the_card_failed(rail_cards) -> None:
    # Arrange
    conclusion = "failure"
    # Act
    status = rail_cards.STATUS_FOR_CONCLUSION[conclusion]
    # Assert
    assert status == "failed"


def test_a_successful_run_files_the_card_done(rail_cards) -> None:
    # Arrange
    conclusion = "success"
    # Act
    status = rail_cards.STATUS_FOR_CONCLUSION[conclusion]
    # Assert
    assert status == "done"


def test_every_terminal_conclusion_has_a_status(rail, rail_cards) -> None:
    """The two tables cannot drift apart without this failing."""
    # Arrange
    conclusions = set(rail.TERMINAL_CONCLUSIONS)
    # Act
    mapped = set(rail_cards.STATUS_FOR_CONCLUSION)
    # Assert
    assert conclusions == mapped


def test_a_pending_card_is_out_of_the_runnable_set(rail_cards) -> None:
    """`in_progress` would make a waiting card look like a stalled one.

    Queue p90 here is ~902s, so a pending card would sit in somebody's
    runnable work looking abandoned for a quarter of an hour.
    """
    # Arrange
    expected = "blocked"
    # Act
    status = rail_cards.PENDING_STATUS
    # Assert
    assert status == expected


def test_a_pending_card_names_compute_as_its_gate(rail_cards) -> None:
    """`compute` is the store's own word for "waiting on a machine"."""
    # Arrange
    expected = "compute"
    # Act
    blocker = rail_cards.PENDING_BLOCKER
    # Assert
    assert blocker == expected


def test_the_verdict_clears_the_pending_blocker(rail_cards) -> None:
    """A card parked on a gate must be un-parked when the gate opens.

    Otherwise the rail leaves one permanently-blocked card per push, on a
    gate that has already opened -- its own failure mode one level up.
    """
    # Arrange
    source = (CI_DIR / "ci_card_rail.py").read_text(encoding="utf-8")
    # Act
    verdict_half = source.split("def record_verdict", 1)[1]
    # Assert
    assert "blocker=BLOCKER_CLEARED" in verdict_half


def test_clearing_a_field_means_the_empty_string(rail_cards) -> None:
    # Arrange
    expected = ""
    # Act
    sentinel = rail_cards.BLOCKER_CLEARED
    # Assert
    assert sentinel == expected


class _FakeStore:
    """A minimal card store: enough to drive superseding, no postgres."""

    def __init__(self, tasks: list[dict]) -> None:
        self.tasks = {t["id"]: t for t in tasks}
        self.comments: list[str] = []

    def list_tasks(self, scope: str = "", **_kw) -> list[dict]:
        return [t for t in self.tasks.values() if t.get("scope") == scope]

    def update_task(self, task_id: str, **fields) -> dict:
        self.tasks[task_id].update(fields)
        return self.tasks[task_id]

    def comment_task(self, task_id: str, text: str, by: str = "") -> None:
        self.comments.append(f"{task_id}:{text}")


def _pending(card_id: str, scope: str) -> dict:
    return {"id": card_id, "scope": scope, "status": "blocked", "blocker": "compute"}


def test_superseding_closes_the_stale_pending_card(rail_cards) -> None:
    """A branch that advances orphans its older card.

    The older run is killed by `cancel-in-progress`, and a cancelled run
    is deliberately not a verdict -- so no event can ever settle that
    card. Closing it `done` would assert a green nobody measured.
    """
    # Arrange
    scope = rail_cards.card_scope("o/sac", "feat/x")
    old_id = rail_cards.card_id_for("o/sac", "a" * 40)
    store = _FakeStore([_pending(old_id, scope)])
    # Act
    rail_cards.supersede_older(store, repo="o/sac", branch="feat/x", sha="b" * 40)
    # Assert
    assert store.tasks[old_id]["status"] == "cancelled"


def test_superseding_clears_the_stale_gate(rail_cards) -> None:
    # Arrange
    scope = rail_cards.card_scope("o/sac", "feat/x")
    old_id = rail_cards.card_id_for("o/sac", "a" * 40)
    store = _FakeStore([_pending(old_id, scope)])
    # Act
    rail_cards.supersede_older(store, repo="o/sac", branch="feat/x", sha="b" * 40)
    # Assert
    assert store.tasks[old_id]["blocker"] == rail_cards.BLOCKER_CLEARED


def test_superseding_never_closes_the_current_card(rail_cards) -> None:
    """The commit that just landed is the one card still owed a verdict."""
    # Arrange
    scope = rail_cards.card_scope("o/sac", "feat/x")
    current_id = rail_cards.card_id_for("o/sac", "b" * 40)
    store = _FakeStore([_pending(current_id, scope)])
    # Act
    closed = rail_cards.supersede_older(store, repo="o/sac", branch="feat/x", sha="b" * 40)
    # Assert
    assert closed == []


def test_superseding_leaves_settled_cards_alone(rail_cards) -> None:
    """A card that already got its verdict is history, not debris."""
    # Arrange
    scope = rail_cards.card_scope("o/sac", "feat/x")
    old_id = rail_cards.card_id_for("o/sac", "a" * 40)
    settled = {"id": old_id, "scope": scope, "status": "failed"}
    store = _FakeStore([settled])
    # Act
    rail_cards.supersede_older(store, repo="o/sac", branch="feat/x", sha="b" * 40)
    # Assert
    assert store.tasks[old_id]["status"] == "failed"


def test_superseding_is_scoped_to_one_branch(rail_cards) -> None:
    """A push to one branch says nothing about another branch's runs."""
    # Arrange
    other_id = rail_cards.card_id_for("o/sac", "c" * 40)
    other = _pending(other_id, rail_cards.card_scope("o/sac", "feat/other"))
    store = _FakeStore([other])
    # Act
    rail_cards.supersede_older(store, repo="o/sac", branch="feat/x", sha="b" * 40)
    # Assert
    assert store.tasks[other_id]["status"] == "blocked"


def test_the_verdict_half_supplies_a_creator_when_it_creates(rail_cards) -> None:
    """A GitHub runner's `run:` step carries NO agent identity at all.

    The store demands a creator and refuses to invent one, so creating a
    card there fails unless the rail supplies it. This is the path taken
    whenever the pre-push hook did not run -- every human push, every
    repo without the hook -- so the rail would fail exactly where it is
    meant to be the safety net. Found by running the real command under
    `env -i` plus the runner service's PATH; from a container shell every
    identity variable is populated and the call succeeds, which is why
    this cannot be caught by testing from a shell.
    """
    # Arrange
    source = (CI_DIR / "ci_card_rail.py").read_text(encoding="utf-8")
    # Act
    verdict_half = source.split("def record_verdict", 1)[1]
    # Assert
    assert 'create_only={"created_by": VERDICT_ACTOR}' in verdict_half


def test_creator_is_only_sent_on_creation(rail_cards) -> None:
    """`created_by` is accepted by add_task and REJECTED by update_task.

    One dict cannot serve both calls, which is the whole reason
    `create_only` exists as a separate channel.
    """
    # Arrange
    calls: list[str] = []

    class _Store:
        TaskNotFoundError = KeyError

        def get_task(self, task_id: str):
            return {"id": task_id}

        def update_task(self, **fields):
            calls.append("update:" + ",".join(sorted(fields)))
            return fields

    # Act
    rail_cards.upsert_card(
        _Store(), "ci-x-1", title="t", create_only={"created_by": "ci"}, status="done"
    )
    # Assert
    assert "created_by" not in calls[0]


def test_a_green_verdict_does_not_claim_mergeability(rail) -> None:
    """This rail sees ONE workflow; `needs:` cannot cross workflow files.

    Saying "self-merge" from the pytest gate alone is a verdict over the
    checks that happen to be in view, dressed as a verdict over the
    checks that should have run -- the same shape as reading a queued
    check as green, which is the bug this rail exists to answer.
    """
    # Arrange
    kwargs = dict(
        repo="scitex-ai/sac",
        branch="feat/x",
        sha="abcdef1234567890",
        conclusion="success",
        leg="pytest-matrix",
        run_url="https://example.test/1",
        card_id="ci-sac-abcdef123456",
    )
    # Act
    text = rail.verdict_text(**kwargs)
    # Assert
    assert "self-merge" not in text.lower()


def test_a_green_verdict_names_what_it_did_not_see(rail) -> None:
    # Arrange
    kwargs = dict(
        repo="scitex-ai/sac",
        branch="feat/x",
        sha="abcdef1234567890",
        conclusion="success",
        leg="pytest-matrix",
        run_url="https://example.test/1",
        card_id="ci-sac-abcdef123456",
    )
    # Act
    text = rail.verdict_text(**kwargs)
    # Assert
    assert "report separately" in text


def test_title_leads_with_the_verdict(rail_cards) -> None:
    """The one word a reader woken at 4am needs, first."""
    # Arrange
    repo, branch, sha = "scitex-ai/sac", "feat/x", "abcdef1234567890"
    # Act
    title = rail_cards.card_title(repo, branch, sha, "failure")
    # Assert
    assert title.startswith("[CI FAILURE]")


def test_title_identifies_the_branch_a_human_would_look_for(rail_cards) -> None:
    # Arrange
    repo, branch, sha = "scitex-ai/sac", "feat/x", "abcdef1234567890"
    # Act
    title = rail_cards.card_title(repo, branch, sha, "failure")
    # Assert
    assert "sac" in title and "feat/x" in title and "abcdef12" in title


# ---------------------------------------------------------------------------
# recipient resolution -- the difference between delivered and silent
# ---------------------------------------------------------------------------
def test_recipient_prefers_a_reachable_agent_over_a_deaf_one(rail) -> None:
    """The regression that motivated not reusing sac's resolve_owner.

    Two specs declare ``project: scitex-agent-container``. That function
    walks ``sorted(glob("*/spec.yaml"))`` and takes the first, and "-"
    sorts before "/", so ``scitex-agent-container-04`` wins -- measured
    with zero inbox subscribers since 2026-08-10. Delivering there raises
    nothing anywhere; it simply reaches nobody.
    """
    # Arrange
    agents = [
        _agent("sac-04", project="sac", reachable=False),
        _agent("sac", project="sac", reachable=True),
    ]
    # Act
    who, _how = rail.resolve_recipient(card=None, repo="x/sac", agents=agents)
    # Assert
    assert who == "sac"


def test_spec_resolution_reports_how_it_decided(rail) -> None:
    # Arrange
    agents = [_agent("sac", project="sac", reachable=True)]
    # Act
    _who, how = rail.resolve_recipient(card=None, repo="x/sac", agents=agents)
    # Assert
    assert how == "spec"


def test_recorded_pusher_beats_any_inference(rail) -> None:
    """A record of who pushed outranks a guess from the repo name."""
    # Arrange
    agents = [_agent("some-other-agent", project="sac", reachable=True)]
    # Act
    who, _how = rail.resolve_recipient(
        card={"agent": "the-actual-pusher"}, repo="x/sac", agents=agents
    )
    # Assert
    assert who == "the-actual-pusher"


def test_card_resolution_reports_how_it_decided(rail) -> None:
    # Arrange
    agents = [_agent("some-other-agent", project="sac", reachable=True)]
    # Act
    _who, how = rail.resolve_recipient(
        card={"agent": "the-actual-pusher"}, repo="x/sac", agents=agents
    )
    # Assert
    assert how == "card"


def test_unresolvable_recipient_is_reported_not_guessed(rail) -> None:
    """The caller must fail loudly rather than drop the verdict."""
    # Arrange
    agents: list[dict] = []
    # Act
    who, _how = rail.resolve_recipient(card=None, repo="x/nobody", agents=agents)
    # Assert
    assert who is None


def test_deaf_is_still_chosen_when_it_is_the_only_candidate(rail) -> None:
    """A deaf owner beats no addressee -- the event is durable.

    ``publish_to_agent`` persists to ``channel_events`` before it
    publishes, so a verdict addressed to an unsubscribed agent replays on
    its next connect. Refusing to address it would discard a recoverable
    delivery.
    """
    # Arrange
    agents = [_agent("only-one", project="sac", reachable=False)]
    # Act
    who, _how = rail.resolve_recipient(card=None, repo="x/sac", agents=agents)
    # Assert
    assert who == "only-one"


# ---------------------------------------------------------------------------
# identity resolution
# ---------------------------------------------------------------------------
def test_unexpanded_template_is_not_an_identity(rail, clean_agent_env) -> None:
    """``${SCITEX_TODO_AGENT_ID}`` arrives literally in some processes.

    Measured in the cards MCP server on this host. Accepting it would
    file cards owned by an agent whose name is a shell template.
    """
    # Arrange
    clean_agent_env["SCITEX_TODO_AGENT_ID"] = "${SCITEX_TODO_AGENT_ID}"
    clean_agent_env["SAC_NAME"] = "real-agent"
    # Act
    resolved = rail.pushing_agent()
    # Assert
    assert resolved == "real-agent"


def test_no_identity_anywhere_returns_none(rail, clean_agent_env) -> None:
    """An owner-less card is rejected by the store, so this must be visible.

    The fixture has already removed every identity variable; returning
    None is what lets the push half report that plainly instead of
    filing a card with no owner.
    """
    # Arrange
    present = [v for v in rail.AGENT_ID_ENV_VARS if clean_agent_env.get(v)]
    # Act
    resolved = rail.pushing_agent() if not present else "fixture-did-not-clean"
    # Assert
    assert resolved is None


# ---------------------------------------------------------------------------
# the message a woken human reads
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_verdict_text_states_the_conclusion(rail, conclusion: str) -> None:
    # Arrange
    run_url = "https://github.com/o/r/actions/runs/1"
    # Act
    text = rail.verdict_text(
        repo="scitex-ai/sac",
        branch="feat/x",
        sha="abcdef1234567890",
        conclusion=conclusion,
        leg="pytest-matrix",
        run_url=run_url,
        card_id="ci-sac-abcdef123456",
    )
    # Assert
    assert conclusion.upper() in text


def test_verdict_text_links_the_run(rail) -> None:
    # Arrange
    run_url = "https://github.com/o/r/actions/runs/1"
    # Act
    text = rail.verdict_text(
        repo="scitex-ai/sac",
        branch="feat/x",
        sha="abcdef1234567890",
        conclusion="failure",
        leg="pytest-matrix",
        run_url=run_url,
        card_id="ci-sac-abcdef123456",
    )
    # Assert
    assert run_url in text


def test_a_red_verdict_names_what_broke(rail) -> None:
    """Arriving without naming the failing test solves half the problem."""
    # Arrange
    detail = "failed: pytest-matrix\n  E   assert 2 == 4"
    # Act
    text = rail.verdict_text(
        repo="scitex-ai/sac",
        branch="feat/x",
        sha="abcdef1234567890",
        conclusion="failure",
        leg="pytest-matrix",
        run_url="https://example.test/1",
        card_id="ci-sac-abcdef123456",
        detail=detail,
    )
    # Assert
    assert "assert 2 == 4" in text


# ---------------------------------------------------------------------------
# WORKFLOW SHAPE -- the single-line edits that would silently re-break it
# ---------------------------------------------------------------------------
def test_verdict_job_is_pinned_to_the_control_plane_host(verdict_job) -> None:
    """ADR-0024 assumed "the self-hosted runners execute on this host".

    They do not. ``vars.CI_RUNS_ON`` is ``scitex-org-cpu``: four runners
    on four machines, and only scitex-04-org-cpu-01 sits on the box
    running ``sac listen`` and the card store. Unpinned, this job would
    75% of the time POST to another machine's loopback and write to
    another postgres -- delivered to nobody, recorded nowhere, silent.
    """
    # Arrange
    runs_on = verdict_job["runs-on"]
    # Act
    labels = set(runs_on) if isinstance(runs_on, list) else set()
    # Assert
    assert CONTROL_PLANE_LABEL in labels


def test_verdict_job_does_not_inherit_the_shared_runner_pool(verdict_job) -> None:
    """A literal label list, never ``vars.CI_RUNS_ON``."""
    # Arrange
    runs_on = verdict_job["runs-on"]
    # Act
    is_literal_list = isinstance(runs_on, list)
    # Assert
    assert is_literal_list


def test_verdict_job_reports_red(verdict_job) -> None:
    """Without ``always()`` a failed gate SKIPS this job.

    The rail would then deliver green verdicts and stay silent on red --
    a congratulations service, not a feedback rail.
    """
    # Arrange
    condition = str(verdict_job.get("if", ""))
    # Act
    fires_unconditionally = "always()" in condition
    # Assert
    assert fires_unconditionally


def test_verdict_job_waits_for_the_gate(verdict_job) -> None:
    # Arrange
    needs = verdict_job["needs"]
    # Act
    waits_on_test = "test" in needs
    # Assert
    assert waits_on_test


def test_verdict_job_passes_the_card_store_explicitly(verdict_env) -> None:
    """An unset DSN does not fail -- it silently picks a local file.

    A ``run:`` step gets a non-interactive shell sourcing no profile, so
    the DSN must come from the workflow. Writing the verdict into a store
    no board reads is the same silent-nobody failure as a deaf recipient.
    """
    # Arrange
    keys = set(verdict_env)
    # Act
    declares_store = "SCITEX_CARDS_DB" in keys
    # Assert
    assert declares_store


def test_verdict_job_points_at_a_postgres_store(verdict_env) -> None:
    # Arrange
    dsn = verdict_env["SCITEX_CARDS_DB"]
    # Act
    is_postgres = "postgres" in dsn
    # Assert
    assert is_postgres


def test_verdict_job_can_read_failed_job_logs(gate_workflow) -> None:
    """Naming the failure needs ``actions: read``.

    A workflow that withholds a permission its own job needs is one of
    the inert-but-configured shapes this fleet keeps rediscovering.
    """
    # Arrange
    permissions = gate_workflow["permissions"]
    # Act
    granted = permissions.get("actions")
    # Assert
    assert granted == "read"


def test_verdict_step_invokes_the_rail(verdict_run_script) -> None:
    # Arrange
    script = verdict_run_script
    # Act
    invokes = "ci_card_rail.py verdict" in script
    # Assert
    assert invokes


def test_verdict_step_passes_the_gate_result(verdict_run_script) -> None:
    # Arrange
    script = verdict_run_script
    # Act
    passes_conclusion = "--conclusion" in script
    # Assert
    assert passes_conclusion


def test_verdict_step_resolves_uv_by_absolute_path(verdict_run_script) -> None:
    """The runner service's PATH is the system default.

    It does not include ~/.local/bin, so a bare ``uv`` is not found here
    even though it is on the operator's interactive PATH.
    """
    # Arrange
    script = verdict_run_script
    # Act
    has_absolute_fallback = ".local/bin/uv" in script
    # Assert
    assert has_absolute_fallback


# EOF
