"""The fixture helpers must produce specs that actually LOAD.

This file exists because of a specific failure, and it is worth stating so the
next person does not delete it as redundant.

``spec.host`` became REQUIRED on 2026-06-24. ``explicit_doc`` (the dict path)
supplies a default host; ``explicitize_yaml`` (the string-template path, used by
58 test files) did not. A fixture built through the string path therefore made
``load_config`` RAISE, the caller saw ``cfg is None``, and the code under test
took a *different branch* — so three tests in ``TestListJsonTimeoutBudget`` kept
reporting while exercising nothing. Two passed vacuously; one could only ever
fail from scheduling noise, which we read as flakiness.

A fixture-migration pass on 2026-07-21 edited the very file holding them and
still missed them. That is the shape of the problem: **a missed fixture and a
correct one are indistinguishable from the outside**, because the missed one
keeps passing. A sweep cannot establish its own completeness.

So the barrier is not "sweep again". It is this: assert the helpers' output
loads. When the next field becomes required, ONE test fails loudly here instead
of 58 fixtures silently changing what they exercise.

Every assertion goes through the real ``load_config`` and reads the real
``AgentConfig`` — not a re-parse of the text we just generated. Re-parsing would
only prove the helper can round-trip its own output, which is not the property
that broke. No mocks, per the repo's STX-TQ rules.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.config import load_config
from tests.scitex_agent_container._helpers.explicit_spec import (
    explicit_doc,
    explicitize_yaml,
)

# A caller who does not care about placement writes neither host nor hosts —
# the terse form both helpers are meant to support.
_MINIMAL_BODY = """apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
"""


def _load_from_body(root: Path, name: str, body: str):
    """Run a fixture body through the helper and the REAL loader."""
    d = root / name
    d.mkdir(parents=True)
    path = d / f"{name}.yaml"
    path.write_text(explicitize_yaml(body))
    return load_config(str(path))


def test_explicitize_yaml_output_is_loadable(tmp_path):
    """The string-template path must produce a spec load_config accepts."""
    # Arrange — the terse body, declaring no placement.
    body = _MINIMAL_BODY
    # Act — a raise here IS the regression this file guards.
    cfg = _load_from_body(tmp_path, "string-path", body)
    # Assert
    assert cfg is not None


def test_explicitize_yaml_defaults_placement_when_undeclared(tmp_path):
    """A terse fixture gets a usable host, not an empty one."""
    # Arrange
    body = _MINIMAL_BODY
    # Act
    cfg = _load_from_body(tmp_path, "defaulted", body)
    # Assert — non-empty is the property; the value is the expanded hostname.
    assert cfg.hosts_spec.host != ""


def test_explicit_doc_declares_placement_too(tmp_path):
    """The dict path already defaulted placement; pin that it still does.

    The two paths disagreed for six weeks and that asymmetry is what voided
    the tests. Pinning both sides is cheaper than re-deriving which path a
    given fixture used.
    """
    # Arrange
    overrides = {"runtime": "apptainer"}
    # Act
    spec = explicit_doc(overrides)["spec"]
    # Assert
    assert "host" in spec or "hosts" in spec


def test_explicitize_yaml_does_not_override_a_declared_host(tmp_path):
    """A fixture that DOES declare placement keeps its own value."""
    # Arrange — the default must not clobber an intentional choice.
    body = _MINIMAL_BODY.rstrip("\n") + "\n  host: deliberate-host\n"
    # Act
    cfg = _load_from_body(tmp_path, "declared", body)
    # Assert
    assert cfg.hosts_spec.host == "deliberate-host"


def test_explicitize_yaml_leaves_an_hosts_only_fixture_alone(tmp_path):
    """``hosts`` is the other half of the mutually-exclusive pair."""
    # Arrange
    body = _MINIMAL_BODY.rstrip("\n") + "\n  hosts: [alpha, beta]\n"
    # Act
    cfg = _load_from_body(tmp_path, "hosts-only", body)
    # Assert — adding host here would make the pair invalid, not terse.
    assert cfg.hosts_spec.hosts == ["alpha", "beta"]
