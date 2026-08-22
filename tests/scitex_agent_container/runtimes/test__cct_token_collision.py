"""ONE bot per agent, asserted in the SPECS — the cross-host half of the invariant.

Covers ``runtimes/_cct_token_collision`` and its observation half
``runtimes/_cct_token_census``. The fault: two specs resolve to the SAME
Telegram bot token, so whichever of them run 409 each other and the operator's
inbound messages are dropped. Measured live 2026-08-22 — ``scitex-hub`` on
compute-04 and ``proj-scitex-hub`` on compute-03 — while the HOST-LOCAL poller
check returned ok on both hosts, because it structurally cannot see across
them.

WHY THIS SUITE IS HERMETIC EVEN THOUGH THE FLEET IS THE POSITIVE CONTROL.
The check was verified against the real spec tree and reported that pair
(plus two more nobody had reported). But a CI gate that depends on the fleet
STAYING broken is a gate that goes green the moment somebody fixes it, so the
collision here is built from synthetic specs on disk and a real temp pool.

The load-bearing tests are the NEGATIVE ones: the handyman family (an
explicitly empty token) and the mute majority (a requested rail that resolves
nothing) must never be reported as collisions. If this check flags them it is
wrong — that arrangement IS the invariant, upheld by hand.

Real on-disk v3 specs, a real ``PoolRead`` through the documented ``pool=``
seam, real ``AgentConfig`` objects — no mocks (PA-306). STX-TQ002 AAA markers,
STX-TQ007 one assert per test. Slot names use a ``ZZ_``-prefixed namespace so
an operator shell's real pool vars can never collide with the fixtures.

Named ``test__cct_token_collision.py`` for the PS-202/PS-204 mirror against
``src/scitex_agent_container/runtimes/_cct_token_collision.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml as _yaml

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._cct_token_census import (
    TokenClaim,
    census_from_resolutions,
    census_specs,
    spec_host,
)
from scitex_agent_container.runtimes._cct_token_collision import (
    COLLISION_OK,
    COLLISION_UNKNOWN,
    COLLISION_VIOLATION,
    check_token_collisions,
    group_collisions,
    verdict_for,
)
from scitex_agent_container.runtimes._cct_token_resolution import resolve_cct_token
from scitex_agent_container.runtimes._secret_pool import PoolRead
from tests.scitex_agent_container._helpers.explicit_spec import explicit_spec

_CHANNEL = "server:claude-code-telegrammer"
# A value-shaped string. No verdict, hint or projection may contain it.
_SECRET = "zz-not-a-real-bot-token-0000000000"


@pytest.fixture
def fleet(tmp_path: Path) -> Iterator[Path]:
    """A real, empty spec tree passed explicitly via ``agents_dir``.

    Deliberately NOT a redirected ``$SCITEX_DIR``: passing the directory is
    the same statement with none of the coupling, and the pool travels
    through the ``pool=`` seam rather than the environment.
    """
    agents = tmp_path / "agents"
    agents.mkdir(parents=True)
    yield agents


def _pool(slots: dict[str, str] | None = None, *, trusted: bool = True) -> PoolRead:
    """A real PoolRead — the documented injection seam, not a mock."""
    return PoolRead(
        env=dict(slots if slots is not None else {"CCT_BOT_TOKEN_ZZ_FOREIGN": _SECRET}),
        trusted=trusted,
        detail="" if trusted else "no canonical secret file resolved (test)",
    )


def _write_spec(
    agents: Path,
    name: str,
    *,
    channel: bool = True,
    host: str = "${HOSTNAME}",
    env: dict | None = None,
) -> None:
    """A REAL fully-explicit v3 ``<name>/spec.yaml`` the loader accepts.

    Dir-as-SSoT: the AGENT NAME comes from the directory, exactly as on a real
    host. ``explicit_spec`` deep-merges the blocks this suite is about onto the
    production paste-defaults so the other required fields stay present.
    """
    body = explicit_spec(
        {
            "host": host,
            "workdir": str(agents.parent),
            "claude": {"channels": [_CHANNEL] if channel else []},
            "apptainer": {"env": dict(env or {})},
        }
    )
    target = agents / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "spec.yaml").write_text(
        _yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "metadata": {"labels": {}},
                "spec": body,
            }
        ),
        encoding="utf-8",
    )


def _cfg(name: str, *, channel: bool = True, env: dict | None = None) -> AgentConfig:
    cfg = AgentConfig(name=name)
    if channel:
        cfg.claude.channels = [_CHANNEL]
    if env:
        cfg.env = dict(env)
    return cfg


def _claim(agent: str, fp: str, host: str = "") -> TokenClaim:
    return TokenClaim(agent=agent, token_fp=fp, host=host)


def _rows(*configs_and_hosts, pool: PoolRead):
    """``(resolution, host, spec)`` triples, the pure seam's input shape."""
    return [
        (resolve_cct_token(cfg, dest=None, pool=pool), host, f"/zz/{cfg.name}.yaml")
        for cfg, host in configs_and_hosts
    ]


# ---------------------------------------------------------------------------
# the fault
# ---------------------------------------------------------------------------


def test_two_specs_on_one_bot_are_a_violation(fleet: Path) -> None:
    # Arrange — the 2026-08-22 shape, reproduced hermetically.
    _write_spec(fleet, "zz-hub", host="zz-compute-04", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HUB"})
    _write_spec(
        fleet, "zz-proj-hub", host="zz-compute-03", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HUB"}
    )
    # Act
    verdict = check_token_collisions(
        agents_dir=str(fleet), pool=_pool({"CCT_BOT_TOKEN_ZZ_HUB": _SECRET})
    )
    # Assert
    assert verdict.state == COLLISION_VIOLATION


def test_the_violation_names_both_agents_and_both_hosts(fleet: Path) -> None:
    # Arrange — the remedy is a config decision about which one yields, so a
    # report that names one of them is not actionable.
    _write_spec(fleet, "zz-hub", host="zz-compute-04", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HUB"})
    _write_spec(
        fleet, "zz-proj-hub", host="zz-compute-03", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HUB"}
    )
    # Act
    verdict = check_token_collisions(
        agents_dir=str(fleet), pool=_pool({"CCT_BOT_TOKEN_ZZ_HUB": _SECRET})
    )
    # Assert
    assert verdict.collisions[0].describe() == (
        "zz-hub@zz-compute-04 + zz-proj-hub@zz-compute-03"
    )


def test_a_cross_host_pair_is_marked_cross_host(fleet: Path) -> None:
    # Arrange — the case NO per-host process check can see; saying so is what
    # stops a reader concluding a kill on one host settled it.
    _write_spec(fleet, "zz-hub", host="zz-compute-04", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HUB"})
    _write_spec(
        fleet, "zz-proj-hub", host="zz-compute-03", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HUB"}
    )
    # Act
    verdict = check_token_collisions(
        agents_dir=str(fleet), pool=_pool({"CCT_BOT_TOKEN_ZZ_HUB": _SECRET})
    )
    # Assert
    assert verdict.collisions[0].cross_host is True


def test_two_specs_on_different_bots_are_ok(fleet: Path) -> None:
    # Arrange — the negative control for the detector itself.
    _write_spec(fleet, "zz-one", env={"CCT_BOT_TOKEN_SLOT": "ZZ_ONE"})
    _write_spec(fleet, "zz-two", env={"CCT_BOT_TOKEN_SLOT": "ZZ_TWO"})
    pool = _pool(
        {
            "CCT_BOT_TOKEN_ZZ_ONE": f"{_SECRET}-a",
            "CCT_BOT_TOKEN_ZZ_TWO": f"{_SECRET}-b",
        }
    )
    # Act
    verdict = check_token_collisions(agents_dir=str(fleet), pool=pool)
    # Assert
    assert verdict.state == COLLISION_OK


# ---------------------------------------------------------------------------
# the populations that hold NOTHING and therefore cannot collide
# ---------------------------------------------------------------------------


def test_the_handyman_pattern_is_not_a_violation(fleet: Path) -> None:
    # Arrange — seven agents with an explicitly EMPTY token beside the one
    # that owns the shared bot. This arrangement IS the invariant.
    _write_spec(fleet, "zz-handyman-06", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HANDYMAN"})
    for n in range(1, 6):
        _write_spec(fleet, f"zz-handyman-0{n}", channel=False, env={"CCT_BOT_TOKEN": ""})
    # Act
    verdict = check_token_collisions(
        agents_dir=str(fleet), pool=_pool({"CCT_BOT_TOKEN_ZZ_HANDYMAN": _SECRET})
    )
    # Assert
    assert verdict.state == COLLISION_OK


def test_the_deliberately_tokenless_are_counted_as_such(fleet: Path) -> None:
    # Arrange — excluded, but never silently: an unexplained exclusion is how
    # a census quietly stops covering the thing it claims to cover.
    for n in range(1, 6):
        _write_spec(fleet, f"zz-handyman-0{n}", channel=False, env={"CCT_BOT_TOKEN": ""})
    # Act
    verdict = check_token_collisions(agents_dir=str(fleet), pool=_pool())
    # Assert
    assert len(verdict.census.disabled) == 5


def test_agents_that_resolve_nothing_are_not_a_violation(fleet: Path) -> None:
    # Arrange — the mute majority: 81 specs declared the channel and 15
    # resolved a token (2026-08-12). None of them can take another's bot.
    _write_spec(fleet, "zz-mute-a")
    _write_spec(fleet, "zz-mute-b")
    # Act
    verdict = check_token_collisions(agents_dir=str(fleet), pool=_pool())
    # Assert
    assert verdict.state == COLLISION_OK


def test_agents_that_resolve_nothing_are_still_named(fleet: Path) -> None:
    # Arrange — "not a collision" must not become "not reported": this is the
    # mute-and-deaf fault, owned by `sac agents cct-audit`, and it belongs in
    # the population line rather than folded into the clean count.
    _write_spec(fleet, "zz-mute-a")
    _write_spec(fleet, "zz-mute-b")
    # Act
    verdict = check_token_collisions(agents_dir=str(fleet), pool=_pool())
    # Assert
    assert sorted(verdict.census.unresolved) == ["zz-mute-a", "zz-mute-b"]


# ---------------------------------------------------------------------------
# UNKNOWN is never an all-clear
# ---------------------------------------------------------------------------


def test_an_inconclusive_pool_read_is_unknown_not_ok(fleet: Path) -> None:
    # Arrange — a clean result under a read sac could not trust says nothing.
    _write_spec(fleet, "zz-one", env={"CCT_BOT_TOKEN_SLOT": "ZZ_ONE"})
    # Act
    verdict = check_token_collisions(agents_dir=str(fleet), pool=_pool(trusted=False))
    # Assert
    assert verdict.state == COLLISION_UNKNOWN


def test_a_violation_outranks_an_inconclusive_pool_read(fleet: Path) -> None:
    # Arrange — a HIT is conclusive even when a MISS is not, so a duplicate
    # found under an untrusted read is still a duplicate.
    _write_spec(fleet, "zz-hub", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HUB"})
    _write_spec(fleet, "zz-proj-hub", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HUB"})
    pool = _pool({"CCT_BOT_TOKEN_ZZ_HUB": _SECRET}, trusted=False)
    # Act
    verdict = check_token_collisions(agents_dir=str(fleet), pool=pool)
    # Assert
    assert verdict.state == COLLISION_VIOLATION


def test_a_spec_tree_that_does_not_exist_is_unknown_not_ok(tmp_path: Path) -> None:
    """A root that was never read must not render as a clean fleet.

    CASE E from the adversarial pass: a non-existent spec tree returned "ok"
    with scanned=True. Zero claimants is a legitimate OK ("nothing can
    conflict"), so "enumerated and found nothing" and "never looked" collapse
    into the same verdict unless the missing root is caught. This check exists
    to refuse exactly that collapse.
    """
    # Arrange — a path nobody created.
    missing = tmp_path / "no-such-spec-tree"
    # Act
    verdict = check_token_collisions(agents_dir=str(missing), pool=_pool())
    # Assert
    assert verdict.state == COLLISION_UNKNOWN


def test_a_missing_spec_tree_is_reported_as_not_scanned(tmp_path: Path) -> None:
    # Arrange
    missing = tmp_path / "no-such-spec-tree"
    # Act
    verdict = check_token_collisions(agents_dir=str(missing), pool=_pool())
    # Assert — the verdict must SAY it never looked, not merely decline to be ok.
    assert verdict.scanned is False


def test_an_empty_spec_tree_is_still_ok(tmp_path: Path) -> None:
    """The other half: a root that EXISTS and holds nothing is genuinely clean.

    Without this, the fix above could be 'return unknown whenever the count is
    zero', which would make an empty fleet permanently alarming.
    """
    # Arrange — the directory exists, it just has no specs in it.
    empty = tmp_path / "empty-spec-tree"
    empty.mkdir()
    # Act
    verdict = check_token_collisions(agents_dir=str(empty), pool=_pool())
    # Assert
    assert verdict.state == COLLISION_OK


def test_an_unloadable_spec_is_unknown(fleet: Path) -> None:
    # Arrange — a spec sac cannot read is a claim it cannot COMPUTE.
    _write_spec(fleet, "zz-one", env={"CCT_BOT_TOKEN_SLOT": "ZZ_ONE"})
    broken = fleet / "zz-broken"
    broken.mkdir()
    (broken / "spec.yaml").write_text("{{{ not yaml", encoding="utf-8")
    # Act
    verdict = check_token_collisions(
        agents_dir=str(fleet), pool=_pool({"CCT_BOT_TOKEN_ZZ_ONE": _SECRET})
    )
    # Assert
    assert verdict.state == COLLISION_UNKNOWN


def test_an_unloadable_spec_is_named_not_dropped(fleet: Path) -> None:
    # Arrange
    broken = fleet / "zz-broken"
    broken.mkdir()
    (broken / "spec.yaml").write_text("{{{ not yaml", encoding="utf-8")
    # Act
    verdict = check_token_collisions(agents_dir=str(fleet), pool=_pool())
    # Assert
    assert verdict.census.unreadable == ("zz-broken",)


def test_a_violation_outranks_an_unloadable_spec(fleet: Path) -> None:
    # Arrange — one broken YAML must not mute a computed duplicate.
    _write_spec(fleet, "zz-hub", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HUB"})
    _write_spec(fleet, "zz-proj-hub", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HUB"})
    broken = fleet / "zz-broken"
    broken.mkdir()
    (broken / "spec.yaml").write_text("{{{ not yaml", encoding="utf-8")
    # Act
    verdict = check_token_collisions(
        agents_dir=str(fleet), pool=_pool({"CCT_BOT_TOKEN_ZZ_HUB": _SECRET})
    )
    # Assert
    assert verdict.state == COLLISION_VIOLATION


def test_a_missing_spec_tree_examines_nothing(tmp_path: Path) -> None:
    # Arrange — the zero that must not read like a clean fleet.
    # Act
    verdict = check_token_collisions(
        agents_dir=str(tmp_path / "nope"), pool=_pool()
    )
    # Assert
    assert verdict.census.examined == 0


# ---------------------------------------------------------------------------
# population — R4: a clean count means nothing without its denominator
# ---------------------------------------------------------------------------


def test_the_population_line_states_how_many_specs_were_examined(fleet: Path) -> None:
    # Arrange — "0 collisions across 0" and "0 across 3" must not render alike.
    _write_spec(fleet, "zz-one", env={"CCT_BOT_TOKEN_SLOT": "ZZ_ONE"})
    _write_spec(fleet, "zz-mute")
    _write_spec(fleet, "zz-hand", channel=False, env={"CCT_BOT_TOKEN": ""})
    # Act
    verdict = check_token_collisions(
        agents_dir=str(fleet), pool=_pool({"CCT_BOT_TOKEN_ZZ_ONE": _SECRET})
    )
    # Assert
    assert verdict.population().startswith("3 spec(s) examined, 1 claim a bot token")


def test_an_empty_fleet_says_it_examined_nothing(fleet: Path) -> None:
    # Arrange
    # Act
    verdict = check_token_collisions(agents_dir=str(fleet), pool=_pool())
    # Assert
    assert "0 spec(s) examined" in verdict.population()


# ---------------------------------------------------------------------------
# scope + secrecy
# ---------------------------------------------------------------------------


def test_every_verdict_states_the_scope_and_points_at_the_other_half(
    fleet: Path,
) -> None:
    # Arrange — an unstated limit is the same defect as a wrong hint.
    _write_spec(fleet, "zz-one", env={"CCT_BOT_TOKEN_SLOT": "ZZ_ONE"})
    # Act
    verdict = check_token_collisions(
        agents_dir=str(fleet), pool=_pool({"CCT_BOT_TOKEN_ZZ_ONE": _SECRET})
    )
    # Assert
    assert "sac doctor --pollers" in verdict.to_dict()["scope_note"]


def test_no_token_value_reaches_the_rendered_verdict(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "zz-hub", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HUB"})
    _write_spec(fleet, "zz-proj-hub", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HUB"})
    verdict = check_token_collisions(
        agents_dir=str(fleet), pool=_pool({"CCT_BOT_TOKEN_ZZ_HUB": _SECRET})
    )
    # Act
    rendered = (
        repr(verdict.to_dict())
        + verdict.detail
        + verdict.hint()
        + verdict.summary()
        + verdict.population()
    )
    # Assert
    assert _SECRET not in rendered


def test_an_ok_verdict_offers_no_remedy(fleet: Path) -> None:
    # Arrange — an all-clear that hands out a fix is a hint people learn to
    # ignore on the day it matters.
    _write_spec(fleet, "zz-one", env={"CCT_BOT_TOKEN_SLOT": "ZZ_ONE"})
    # Act
    verdict = check_token_collisions(
        agents_dir=str(fleet), pool=_pool({"CCT_BOT_TOKEN_ZZ_ONE": _SECRET})
    )
    # Assert
    assert verdict.hint() == ""


def test_the_violation_hint_says_killing_a_process_does_not_fix_it(
    fleet: Path,
) -> None:
    # Arrange — the 2026-08-22 remediation stopped at the process and the
    # specs still collided, so the remedy must say so in words.
    _write_spec(fleet, "zz-hub", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HUB"})
    _write_spec(fleet, "zz-proj-hub", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HUB"})
    # Act
    verdict = check_token_collisions(
        agents_dir=str(fleet), pool=_pool({"CCT_BOT_TOKEN_ZZ_HUB": _SECRET})
    )
    # Assert
    assert "Killing a process does NOT fix this" in verdict.hint()


# ---------------------------------------------------------------------------
# the pure seams
# ---------------------------------------------------------------------------


def test_group_collisions_groups_by_fingerprint() -> None:
    # Arrange
    claims = [_claim("a", "sha256:aaa"), _claim("b", "sha256:aaa"), _claim("c", "sha256:bbb")]
    # Act
    groups = group_collisions(claims)
    # Assert
    assert [g.agents for g in groups] == [("a", "b")]


def test_group_collisions_ignores_claims_with_no_fingerprint() -> None:
    # Arrange — two agents holding nothing are not two agents holding the same
    # thing; grouping on "" would manufacture a collision out of absence.
    claims = [_claim("a", ""), _claim("b", "")]
    # Act
    groups = group_collisions(claims)
    # Assert
    assert groups == ()


def test_a_same_host_pair_is_not_marked_cross_host() -> None:
    # Arrange
    claims = [_claim("a", "sha256:aaa", "zz-h1"), _claim("b", "sha256:aaa", "zz-h1")]
    # Act
    groups = group_collisions(claims)
    # Assert
    assert groups[0].cross_host is False


def test_verdict_for_is_pure_over_a_constructed_census() -> None:
    # Arrange — the seam that lets the condition be asserted with no disk.
    pool = _pool({"CCT_BOT_TOKEN_ZZ_HUB": _SECRET})
    rows = _rows(
        (_cfg("zz-a", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HUB"}), "zz-h1"),
        (_cfg("zz-b", env={"CCT_BOT_TOKEN_SLOT": "ZZ_HUB"}), "zz-h2"),
        pool=pool,
    )
    census = census_from_resolutions(rows, pool_trusted=True)
    # Act
    verdict = verdict_for(census)
    # Assert
    assert verdict.state == COLLISION_VIOLATION


def test_census_specs_reads_the_spec_tree_not_the_registry(fleet: Path) -> None:
    # Arrange — a STOPPED agent is absent from the registry and its collision
    # returns the moment it starts, so the spec tree is the right population.
    _write_spec(fleet, "zz-stopped", env={"CCT_BOT_TOKEN_SLOT": "ZZ_ONE"})
    # Act
    census = census_specs(
        agents_dir=str(fleet), pool=_pool({"CCT_BOT_TOKEN_ZZ_ONE": _SECRET})
    )
    # Assert
    assert [c.agent for c in census.claims] == ["zz-stopped"]


def test_spec_host_renders_a_fallback_chain_in_full() -> None:
    # Arrange — every entry is a place the agent could run, and all of them
    # matter to "which one yields".
    cfg = AgentConfig(name="zz-multi")
    cfg.hosts_spec.host = ["zz-h1", "zz-h2"]
    # Act
    got = spec_host(cfg)
    # Assert
    assert got == "zz-h1|zz-h2"
