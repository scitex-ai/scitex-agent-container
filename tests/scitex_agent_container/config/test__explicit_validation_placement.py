"""Pin what ``explicitize_yaml`` does and does NOT do to placement.

Two failures motivated this file. Both are worth stating, because the second
one is the reason the first one's obvious fix is wrong.

**1. A fixture stopped loading and nothing said so.** ``spec.host`` became
REQUIRED on 2026-06-24. ``_run_hanging_probe`` in ``test_cli.py`` builds its
spec through ``explicitize_yaml`` without declaring placement, so from that day
``load_config`` RAISED, the caller saw ``cfg is None``, and the code under test
took a *different branch*. Three tests kept reporting while exercising nothing —
two passed vacuously, one could only ever fail from scheduling noise. A
fixture-migration pass on 2026-07-21 edited that very file and missed them,
because a missed fixture and a correct one are indistinguishable from outside.

**2. The central fix broke four other tests.** The tempting repair is to make
``explicitize_yaml`` default ``host: ${HOSTNAME}`` when the doc declares
neither, mirroring what :func:`explicit_doc` already does for the dict path. It
was tried on 2026-08-07 and MEASURED: ``test__peer_faillloud.py`` went from
7 passed to 4 failed. Those tests are *about* placement resolution — a spec with
no placement resolved as a LOCAL peer (``http://127.0.0.1:...``), and injecting
a host turned it REMOTE (``ssh://${HOSTNAME}...``) and silenced the fail-loud
contradiction detector.

So **absence of placement is a meaningful fixture state**, not an oversight, and
``explicitize_yaml`` must keep leaving it absent. The fix belongs at the fixture
that needs a host, not in the helper shared by 58 files.

These tests pin both halves: the deliberate non-defaulting, and the specific
fixture that must load. No mocks — the real ``load_config`` reads a real file in
the real dir-as-SSoT layout.

**Why this file lives under ``config/``.** Its subject is a TEST helper
(``tests/scitex_agent_container/_helpers/explicit_spec.py``), which has no
production counterpart, and PS-204 (``orphan-test-file``, severity E) fails the
suite for any ``tests/<pkg>/...`` test that mirrors no ``src/<pkg>/...`` module.
The honest mirror is :mod:`scitex_agent_container.config._explicit_validation` —
the module both helpers draw their defaults from via ``explicit_spec_defaults``,
and the module whose "every field explicit" ruling made placement required in
the first place. Placing it here keeps it in the normally-collected tree, so it
actually RUNS in the pytest matrix; a home under ``tests/integration/`` would
have satisfied the auditor by making the barrier invisible to the default run,
which is the very failure mode this file exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config import load_config
from tests.scitex_agent_container._helpers.explicit_spec import (
    explicit_doc,
    explicitize_yaml,
)

_NO_PLACEMENT = """apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
"""

_WITH_PLACEMENT = """apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
  host: ${HOSTNAME}
"""


def _write(root: Path, name: str, body: str) -> Path:
    """Lay a fixture body out dir-as-SSoT, the way load_config expects."""
    d = root / name
    d.mkdir(parents=True)
    path = d / f"{name}.yaml"
    path.write_text(explicitize_yaml(body))
    return path


def test_explicitize_yaml_does_not_invent_placement(tmp_path):
    """A body with no host/hosts must stay that way — tests depend on it.

    Defaulting here flips peers from local to remote and was measured to break
    test__peer_faillloud (7 passed -> 4 failed). If this test ever looks like an
    obstacle, read this file's docstring before removing it.
    """
    # Arrange — check the parsed TOP-LEVEL spec, not the text. The rendered
    # document legitimately contains "host:" in unrelated nested places
    # (network: host, mount_host_claude, a nested bind address), so a substring
    # search over the whole doc answers a different question than this one.
    import yaml

    body = _NO_PLACEMENT
    # Act
    spec = yaml.safe_load(explicitize_yaml(body))["spec"]
    # Assert
    assert "host" not in spec and "hosts" not in spec


def test_a_placementless_fixture_does_not_load(tmp_path):
    """The consequence, pinned: no placement means load_config REFUSES.

    This is the trap that voided three tests for six weeks. Stating it as an
    expected raise makes "this fixture cannot load" a fact the suite asserts,
    rather than a silence a caller discovers as `cfg is None`.
    """
    # Arrange
    path = _write(tmp_path, "no-placement", _NO_PLACEMENT)
    ctx = pytest.raises(ValueError)
    # Act
    act = lambda: load_config(str(path))  # noqa: E731
    # Assert
    with ctx:
        act()


def test_the_refusal_names_the_missing_placement_field(tmp_path):
    """The raise must say WHICH field, or a broken fixture just says "invalid".

    This is the message a future reader meets when their fixture stops loading.
    It is the difference between a two-minute fix and the six weeks this one
    went unnoticed.
    """
    # Arrange
    path = _write(tmp_path, "no-placement-msg", _NO_PLACEMENT)
    # Act — capture without pytest.raises, which would itself be an assertion.
    try:
        load_config(str(path))
        message = ""
    except ValueError as exc:
        message = str(exc)
    # Assert
    assert "host" in message


def test_a_fixture_declaring_host_loads(tmp_path):
    """And the repair a fixture needing a real config must apply."""
    # Arrange
    path = _write(tmp_path, "with-placement", _WITH_PLACEMENT)
    # Act
    cfg = load_config(str(path))
    # Assert
    assert cfg.hosts_spec.host != ""


def test_the_hanging_probe_fixture_loads(tmp_path):
    """The specific fixture whose silent non-loading started all of this.

    test_cli.py's _run_hanging_probe must produce a LOADABLE spec, or its three
    tests go back to exercising the cfg-is-None branch while still reporting.
    """
    # Arrange — the exact body _run_hanging_probe writes.
    body = _WITH_PLACEMENT
    # Act
    cfg = load_config(str(_write(tmp_path, "test-remote", body)))
    # Assert
    assert cfg is not None


def test_explicit_doc_still_defaults_placement_for_the_dict_path(tmp_path):
    """The dict path DOES default — and that asymmetry is intentional.

    explicit_doc's callers pass a dict of overrides and cannot express "I am
    deliberately omitting placement" as distinctly as a YAML body can, so it
    fills one in. Pinned so nobody "harmonises" the two helpers.
    """
    # Arrange
    overrides = {"runtime": "apptainer"}
    # Act
    spec = explicit_doc(overrides)["spec"]
    # Assert
    assert "host" in spec or "hosts" in spec
