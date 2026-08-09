"""sac must DECLARE its system deps, not leave them implicit in an apt line.

`scitex_dev.system_deps` is a working entry-point federation;
`discover_system_deps` aggregates it; apptainer-scitex.def calls it
"SSoT — NOT a hardcoded list". sac registered NOTHING in it while
apptainer-base.def hardcoded apt at :98, :109, :169 and :189-197 — so the
BASE image bypassed the federation the SCITEX def declares authoritative.

THE REASON FIELD IS THE POINT. An apt line records WHAT is installed; only
the reason records WHY, and "why" is what lets a future reader decide
whether a dep can be dropped. So the tests below check that reasons are
substantive rather than transcriptions of the package name — a declaration
whose reasons say "installs ripgrep" would satisfy the schema and defeat
the purpose.

ripgrep gets its own test because it is the sharpest case: the agent hooks
this fleet runs MANDATE it (enforce_ripgrep.sh DENIES `grep -r`), so an
undeclared mandate is one apt-line edit away from silently breaking every
agent's search.

No mocks (PA-306): the real provider, and the real entry-point metadata.
AAA markers, one assertion per test.
"""

from __future__ import annotations

from importlib.metadata import entry_points

import pytest

from scitex_agent_container._system_deps import provide

_GROUP = "scitex_dev.system_deps"


@pytest.fixture
def sac_entry_point():
    """The installed entry point, or skip when the package is not installed."""
    eps = [
        ep for ep in entry_points(group=_GROUP) if ep.name == "scitex-agent-container"
    ]
    if not eps:
        pytest.skip("scitex-agent-container entry point not installed in this env")
    return eps[0]


def test_provider_returns_at_least_one_dep():
    # Arrange
    provider = provide
    # Act
    deps = provider()
    # Assert
    assert deps


def test_every_dep_names_its_provider():
    # Arrange: the aggregator attributes each dep to the package that needs
    # it, so an unattributed entry cannot be traced back when questioned.
    deps = provide()
    # Act
    providers = {d.provider for d in deps}
    # Assert
    assert providers == {"scitex-agent-container"}


def test_every_dep_carries_a_purpose():
    # Arrange
    deps = provide()
    # Act
    missing = [d.package for d in deps if not (d.purpose or "").strip()]
    # Assert
    assert missing == []


def test_purposes_are_more_than_a_restatement_of_the_package():
    # Arrange: "ripgrep — installs ripgrep" would pass a bare non-empty
    # check and carry nothing. The purpose has to say what BREAKS without it.
    deps = provide()
    # Act
    thin = [d.package for d in deps if len(d.purpose.split()) < 6]
    # Assert
    assert thin == []


def test_ripgrep_is_declared_because_the_hooks_mandate_it():
    # Arrange: the sharpest case — enforce_ripgrep.sh denies `grep -r`, so
    # removing rg breaks every agent's search rather than degrading it.
    deps = provide()
    # Act
    rg = [d for d in deps if d.package == "ripgrep"]
    # Assert
    assert rg


def test_tmux_is_declared_because_a_session_is_a_tmux_session():
    # Arrange: sac's TuiSessionRuntime starts, stops and injects turns
    # through tmux, and tmux is the liveness signal that survives a
    # registry outage — it is load-bearing, not a convenience.
    deps = provide()
    # Act
    packages = {d.package for d in deps}
    # Assert
    assert "tmux" in packages


def test_the_entry_point_is_registered(sac_entry_point):
    # Arrange: the module existing is not enough — the aggregator finds
    # providers through the entry-point group, so an unregistered provider
    # is invisible no matter how well it is written. Skipped rather than
    # failed when the package is not pip-installed in the running env: that
    # is a property of the environment, not of this declaration.
    ep = sac_entry_point
    # Act
    name = ep.name
    # Assert
    assert name == "scitex-agent-container"


def test_the_entry_point_resolves_to_this_provider(sac_entry_point):
    # Arrange: a registered name that loads something else is worse than an
    # absent one, because it looks correct in the metadata.
    ep = sac_entry_point
    # Act
    loaded = ep.load()
    # Assert
    assert loaded is provide
