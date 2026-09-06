"""The three gaps ``sac agents migrate-engines`` shipped with, at CLI level.

Real ``CliRunner`` against the real command, real spec files under
``tmp_path``, a real HTTP server on a real loopback port. No mocks: two of the
three facts here are about something NOT happening (a write that must not
occur, a root that must not be the only one searched), and a mock would report
only what the test author believed.

THE THREE:

1. **The version floor.** A sac predating 2026-09-03 rejects ``engines:`` as
   an unknown spec field, so the block strands an agent on such a host. The
   sweep refuses those specs by name, before the write, and an UNMEASURED host
   is refused too — fail closed.
2. **The preflight probed the wrong path.** The gateway base answers 404 while
   ``/v1/models`` answers 401; a probe of the base reports "listening" from a
   path the gateway does not serve.
3. **The roster default.** With neither ``--root`` nor the env var set, the
   sweep fell through to a resolver reading a DIFFERENT env var, landing on
   the container's own ``$HOME`` — one spec beside the fleet's 123, reported
   as a finished sweep.

STX-NM002: no mocks. STX-TQ002 / TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._maintenance._engines_floor import (
    REFUSED_HOST_NOT_MEASURED,
    REFUSED_HOST_PREDATES_ENGINES,
)
from scitex_agent_container.cli_pkg._agents_migrate_engines import (
    default_spec_roots,
)
from scitex_agent_container.cli_pkg._agents_migrate_engines_report import (
    preflight_payload,
)
from scitex_agent_container.config._engine_reach import (
    REACH_UNAUTHORIZED,
    REACH_WRONG_PATH,
)
from scitex_agent_container.config._qwen_gateway import (
    QWEN_GATEWAY_URL_ENV,
    qwen_gateway_probe_url,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc
from tests.scitex_agent_container.cli_pkg.test__agents_migrate_engines import (
    CAPABLE_HOST,
    _run,
    _write_settings,
    _write_spec,
    fleet,
)

__all__ = ["fleet"]

PREDATES = "spartan"
UNMEASURED = "scitex-compute-02"


# ---------------------------------------------------------------------------
# 1. THE VERSION FLOOR
# ---------------------------------------------------------------------------


def test_a_spec_on_a_pre_engines_host_is_refused_by_the_cli(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "grounded", host=PREDATES)
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert [e["agent"] for e in payload["refused"]] == ["grounded"]


def test_the_cli_refusal_carries_the_pre_engines_reason(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "grounded", host=PREDATES)
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert payload["refused"][0]["reason"] == REFUSED_HOST_PREDATES_ENGINES


def test_a_floored_spec_is_not_counted_as_migratable(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "grounded", host=PREDATES)
    _write_spec(fleet, "fine", host=CAPABLE_HOST)
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert payload["would_migrate"] == 1


def test_a_floor_refusal_does_not_fail_the_exit_code(fleet: Path) -> None:
    # Arrange — a NAMED refusal is a legitimate outcome a human resolves.
    _write_spec(fleet, "grounded", host=PREDATES)
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert payload["exit_code"] == 0


def test_a_floor_refusal_keeps_the_plan_safe_to_apply(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "grounded", host=PREDATES)
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert payload["safe_to_apply"] is True


def test_an_unmeasured_host_is_refused_by_the_cli(fleet: Path) -> None:
    # Arrange — compute-02 answered `hostname` and held no sac we could find.
    _write_spec(fleet, "unknown-ground", host=UNMEASURED)
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert payload["refused"][0]["reason"] == REFUSED_HOST_NOT_MEASURED


def test_apply_leaves_the_floored_spec_byte_identical(fleet: Path) -> None:
    # Arrange — the hazard itself: an agent stranded on a validator nobody
    # is watching.
    path = _write_spec(fleet, "grounded", host=PREDATES)
    before = path.read_bytes()
    # Act
    _run("--apply", "--no-diff", "--json")
    # Assert
    assert path.read_bytes() == before


def test_apply_still_writes_a_spec_on_a_capable_host(fleet: Path) -> None:
    # Arrange — a floor that stopped the whole batch would be its own outage.
    _write_spec(fleet, "grounded", host=PREDATES)
    _write_spec(fleet, "fine", host=CAPABLE_HOST)
    # Act
    payload = json.loads(_run("--apply", "--no-diff", "--json").stdout)
    # Assert
    assert payload["written"] == ["fine"]


def test_the_override_flag_lifts_the_floor(fleet: Path) -> None:
    # Arrange — a floor with no exit becomes a reason to bypass the tool.
    _write_spec(fleet, "grounded", host=PREDATES)
    # Act
    payload = json.loads(
        _run("--no-diff", "--json", "--host-supports-engines", PREDATES).stdout
    )
    # Assert
    assert payload["would_migrate"] == 1


def test_the_override_is_recorded_in_the_payload(fleet: Path) -> None:
    # Arrange — the claim someone made has to be readable after the fact.
    _write_spec(fleet, "grounded", host=PREDATES)
    # Act
    payload = json.loads(
        _run("--no-diff", "--json", "--host-supports-engines", PREDATES).stdout
    )
    # Assert
    assert payload["engine_floor_overrides"] == [PREDATES]


def test_a_placeholder_host_is_refused(fleet: Path) -> None:
    # Arrange — ${HOSTNAME} names no machine anyone measured, so which sac
    # would parse the block is unknowable. Fail closed.
    agent_dir = fleet / "anywhere"
    agent_dir.mkdir()
    _write_settings(agent_dir / "to_home")
    doc = explicit_doc(
        {"to_home": "./to_home", "claude": {"model": "opus[1m]"}, "host": "${HOSTNAME}"}
    )
    (agent_dir / "spec.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert payload["refused"][0]["reason"] == REFUSED_HOST_NOT_MEASURED


# ---------------------------------------------------------------------------
# 2. THE PREFLIGHT PROBES /v1/models, NOT THE BASE
# ---------------------------------------------------------------------------


class _LikeTheGateway(BaseHTTPRequestHandler):
    """The measured gateway shape: 404 at the base, 401 at /v1/models."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's own name
        self.send_response(401 if self.path.startswith("/v1/models") else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:
        """Silence the default stderr access log; it is not the fact under test."""


@pytest.fixture
def gateway_like_server():
    """A real server answering exactly as scitex-compute-04:18772 measured."""
    server = HTTPServer(("127.0.0.1", 0), _LikeTheGateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    saved = os.environ.get(QWEN_GATEWAY_URL_ENV)
    os.environ[QWEN_GATEWAY_URL_ENV] = base
    try:
        yield base
    finally:
        if saved is None:
            os.environ.pop(QWEN_GATEWAY_URL_ENV, None)
        else:
            os.environ[QWEN_GATEWAY_URL_ENV] = saved
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_probe_url_appends_the_models_path(gateway_like_server) -> None:
    # Arrange
    base = gateway_like_server
    # Act
    url = qwen_gateway_probe_url()
    # Assert
    assert url == f"{base}/v1/models"


def test_the_preflight_dials_the_models_path(gateway_like_server) -> None:
    # Arrange
    base = gateway_like_server
    # Act
    payload = preflight_payload()
    # Assert
    assert payload["url"] == f"{base}/v1/models"


def test_the_preflight_reads_the_gateway_as_auth_gated(gateway_like_server) -> None:
    # Arrange — probing the BASE returned 404 here, which read as "listening".
    _ = gateway_like_server
    # Act
    payload = preflight_payload()
    # Assert
    assert payload["state"] == REACH_UNAUTHORIZED


def test_the_preflight_reports_the_endpoint_as_served(gateway_like_server) -> None:
    # Arrange
    _ = gateway_like_server
    # Act
    payload = preflight_payload()
    # Assert
    assert payload["serves_endpoint"] is True


def test_the_preflight_still_names_the_base_it_derived_from(
    gateway_like_server,
) -> None:
    # Arrange — a reader must see WHICH address was dialled, not infer it.
    base = gateway_like_server
    # Act
    payload = preflight_payload()
    # Assert
    assert payload["base_url"] == base


def test_probing_the_base_would_have_reported_the_wrong_path(
    gateway_like_server,
) -> None:
    # Arrange — the defect, pinned: the OLD probe target on the SAME server.
    from scitex_agent_container.config._engine_reach import reach_verdict

    base = gateway_like_server
    # Act
    verdict = reach_verdict(base)
    # Assert
    assert verdict.state == REACH_WRONG_PATH


def test_the_cli_preflight_carries_the_probed_path(
    fleet: Path, gateway_like_server
) -> None:
    # Arrange
    _write_spec(fleet, "one", host=CAPABLE_HOST)
    # Act
    payload = json.loads(_run("--no-diff", "--json", "--preflight").stdout)
    # Assert
    assert payload["preflight"]["probe_path"] == "/v1/models"


# ---------------------------------------------------------------------------
# 3. THE ROSTER DEFAULT — every user-scope root, de-duplicated by agent name
# ---------------------------------------------------------------------------


@contextmanager
def _env(**pairs):
    """Set real env vars for the block, restore exactly what was there.

    A ``yield``-based fixture over ``os.environ`` rather than
    ``monkeypatch``: the production resolver reads the real environment, so
    the test sets the real environment. ``None`` removes a variable.
    """
    saved = {key: os.environ.get(key) for key in pairs}
    for key, value in pairs.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def two_roots(tmp_path: Path):
    """Two real user-scope roots, both pinned inside ``tmp_path``.

    ``$SCITEX_DIR`` moves the primary root and
    ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` adds the second — the same two
    seams the runtime uses. ``$SCITEX_AGENT_CONTAINER_AGENTS_DIR`` is
    explicitly UNSET, because that is the state the defect lives in.

    The cwd moves into ``tmp_path`` for the same reason: the resolver walks
    upward looking for a project-local registry, and this repo has one — a
    test run from the checkout would otherwise sweep the repo's own fixtures.
    """
    primary = tmp_path / "home-scitex" / "agent-container" / "agents"
    extra = tmp_path / "operator" / "agents"
    primary.mkdir(parents=True)
    extra.mkdir(parents=True)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        with _env(
            SCITEX_AGENT_CONTAINER_AGENTS_DIR=None,
            SCITEX_DIR=tmp_path / "home-scitex",
            SCITEX_AGENT_CONTAINER_YAML_DIRS=extra,
            SAC_AGENT_SCOPE="user",
            SCITEX_AGENT_CONTAINER_RUNTIME_DIR=tmp_path / "runtime",
            SAC_SPEC_CACHE_DISABLE="1",
        ):
            yield primary, extra
    finally:
        os.chdir(cwd)


def test_the_default_roots_include_every_user_scope_root(two_roots) -> None:
    # Arrange — the old default resolved ONE root, and the wrong one.
    primary, extra = two_roots
    # Act
    roots = default_spec_roots()
    # Assert
    assert {primary, extra} <= set(roots)


def test_the_agents_dir_env_var_still_wins(two_roots, tmp_path: Path) -> None:
    # Arrange — the documented override keeps its precedence.
    named = tmp_path / "named" / "agents"
    named.mkdir(parents=True)
    # Act
    with _env(SCITEX_AGENT_CONTAINER_AGENTS_DIR=named):
        roots = default_spec_roots()
    # Assert
    assert roots == (named,)


def _plant(root: Path, name: str, *, host: str = CAPABLE_HOST) -> Path:
    agent_dir = root / name
    agent_dir.mkdir(parents=True)
    _write_settings(agent_dir / "to_home")
    doc = explicit_doc(
        {"to_home": "./to_home", "claude": {"model": "opus[1m]"}, "host": host}
    )
    path = agent_dir / "spec.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def test_the_sweep_sees_agents_from_every_root(two_roots) -> None:
    # Arrange — the measured shape: one spec in the container's own root,
    # the fleet's specs in the operator's.
    primary, extra = two_roots
    _plant(primary, "lonely")
    _plant(extra, "fleet-one")
    _plant(extra, "fleet-two")
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert payload["specs"] == 3


def test_the_report_names_every_root_it_searched(two_roots) -> None:
    # Arrange — a count with no root is equally true of a finished migration
    # and of a total discovery failure.
    primary, extra = two_roots
    _plant(primary, "lonely")
    _plant(extra, "fleet-one")
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert payload["roots"] == [str(primary), str(extra)]


def test_an_agent_present_in_two_roots_is_planned_once(two_roots) -> None:
    # Arrange — the same NAME under two roots is two paths; writing both
    # migrates one agent twice and creates two answers about it.
    primary, extra = two_roots
    _plant(primary, "twin")
    _plant(extra, "twin")
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert payload["specs"] == 1


def test_the_de_duplication_keeps_the_earlier_root(two_roots) -> None:
    # Arrange
    primary, extra = two_roots
    kept = _plant(primary, "twin")
    dropped = _plant(extra, "twin")
    before = dropped.read_bytes()
    # Act
    _run("--apply", "--no-diff", "--json")
    # Assert
    assert (kept.read_bytes() != before) and (dropped.read_bytes() == before)


def test_a_resolved_root_that_does_not_exist_is_named_absent(
    two_roots, tmp_path: Path
) -> None:
    # Arrange — an operator whose tree was resolved and found MISSING must be
    # able to see that; silently dropping it lets the count read as covering
    # it. This is the same absent-vs-empty split _roster_state draws.
    _, extra = two_roots
    _plant(extra, "fleet-one")
    absent = tmp_path / "nowhere"
    # Act
    with _env(SCITEX_AGENT_CONTAINER_YAML_DIRS=f"{extra}:{absent}"):
        payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert payload["roots_absent"] == [str(absent)]


def test_a_root_that_exists_is_not_named_absent(two_roots) -> None:
    # Arrange — the control for the assertion above.
    _, extra = two_roots
    _plant(extra, "fleet-one")
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert payload["roots_absent"] == []
