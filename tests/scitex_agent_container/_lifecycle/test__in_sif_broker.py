"""SAC-from-SAC broker — in-SIF detection + host-listen spawn POST.

When ``sac agents start <child>`` runs INSIDE an apptainer SIF, the
runtime cannot exec ``apptainer`` locally (no nested apptainer on most
HPCs). The fix is to detect the in-SIF condition early in
``agent_start`` and POST the spawn RPC to the host-side ``sac listen``
server instead — the host has ``apptainer`` and owns container
lifecycle (operator-mandated 2026-06-01).

This module tests two collaborators:

* :func:`_in_sif_broker.is_in_sif` — pure env-detection.
* :func:`_in_sif_broker.broker_start_to_host` — POSTs to host listen
  via the existing :mod:`_spawn_client` (same wire as ``agent_spawn``).

And the integration point:

* :func:`agent_start` in-SIF redirects to the broker BEFORE any runtime
  / drift / rotation / ACL work runs locally; not-in-SIF path is
  unchanged (regression guard).

NO MOCKS — the spawn-client opener seam is the real injection surface
(:mod:`test__spawn_client` uses it the same way). Each test: AAA
markers (TQ002), one assertion (TQ007), 3+-word name.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

import pytest

from scitex_agent_container._lifecycle._in_sif_broker import (
    InSifBrokerError,
    broker_start_to_host,
    is_in_sif,
)
from scitex_agent_container._lifecycle._start import agent_start
from scitex_agent_container._state import state_db
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config import AgentConfig

# ---------------------------------------------------------------------------
# Real (non-mock) fake response + opener (urllib protocol)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _opener_returning(body: bytes, status: int = 200):
    captured: dict = {}

    def opener(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        captured["headers"] = {k.lower(): v for k, v in dict(req.headers).items()}
        captured["timeout"] = timeout
        return _FakeResp(body, status)

    return opener, captured


# ---------------------------------------------------------------------------
# Env fixtures — toggle the in-SIF env vars + the listen base URL
# ---------------------------------------------------------------------------


_SIF_KEYS = ("APPTAINER_CONTAINER", "SINGULARITY_CONTAINER")
_LISTEN_KEYS = (
    "SAC_LISTEN_BASE_URL",
    "SCITEX_AGENT_CONTAINER_LISTEN_BASE_URL",
    "SAC_LISTEN_BEARER",
    "SCITEX_AGENT_CONTAINER_LISTEN_BEARER",
    "SAC_NAME",
    "SCITEX_AGENT_CONTAINER_NAME",
)


@pytest.fixture
def sif_env() -> Iterator[Any]:
    """Yield a setter for the in-SIF env vars (both apptainer + singularity)."""
    saved = {k: os.environ.get(k) for k in _SIF_KEYS}
    for k in _SIF_KEYS:
        os.environ.pop(k, None)

    def _set(value: str | None, *, key: str = "APPTAINER_CONTAINER") -> None:
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    try:
        yield _set
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


@pytest.fixture
def listen_env() -> Iterator[Any]:
    """Yield a setter for SAC_LISTEN_* + SAC_NAME (mirrors test__spawn_client)."""
    saved = {k: os.environ.get(k) for k in _LISTEN_KEYS}
    for k in _LISTEN_KEYS:
        os.environ.pop(k, None)

    def _set(suffix: str, value: str | None) -> None:
        short = f"SAC_{suffix}"
        long_ = f"SCITEX_AGENT_CONTAINER_{suffix}"
        os.environ.pop(long_, None)
        if value is None:
            os.environ.pop(short, None)
        else:
            os.environ[short] = value

    try:
        yield _set
    finally:
        for k in _LISTEN_KEYS:
            os.environ.pop(k, None)
        for k, prev in saved.items():
            if prev is not None:
                os.environ[k] = prev


# ---------------------------------------------------------------------------
# is_in_sif — pure env detection
# ---------------------------------------------------------------------------


def test_is_in_sif_true_when_apptainer_container_env_set(sif_env) -> None:
    # Arrange — apptainer auto-sets APPTAINER_CONTAINER to the SIF path.
    sif_env("/path/to/agent.sif", key="APPTAINER_CONTAINER")
    # Act
    detected = is_in_sif()
    # Assert
    assert detected is True


def test_is_in_sif_true_when_singularity_container_env_set(sif_env) -> None:
    # Arrange — legacy singularity name; the runtime must honour both.
    sif_env("/path/to/agent.sif", key="SINGULARITY_CONTAINER")
    # Act
    detected = is_in_sif()
    # Assert
    assert detected is True


def test_is_in_sif_false_when_both_env_vars_unset(sif_env) -> None:
    # Arrange — neither var set → bare host.
    sif_env(None, key="APPTAINER_CONTAINER")
    sif_env(None, key="SINGULARITY_CONTAINER")
    # Act
    detected = is_in_sif()
    # Assert
    assert detected is False


def test_is_in_sif_false_when_apptainer_container_empty_string(sif_env) -> None:
    # Arrange — defensive: an explicit empty string is still "not in SIF".
    sif_env("", key="APPTAINER_CONTAINER")
    # Act
    detected = is_in_sif()
    # Assert
    assert detected is False


# ---------------------------------------------------------------------------
# broker_start_to_host — POST shape via the spawn_client opener seam
# ---------------------------------------------------------------------------


def test_broker_posts_to_agents_route_on_host_listen(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"child","returncode":0}')
    # Act
    broker_start_to_host("child", opener=opener)
    # Assert
    assert captured["url"] == "http://host:9100/agents"


def test_broker_uses_http_post_method(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"child","returncode":0}')
    # Act
    broker_start_to_host("child", opener=opener)
    # Assert
    assert captured["method"] == "POST"


def test_broker_body_includes_child_name(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"child","returncode":0}')
    # Act
    broker_start_to_host("child", opener=opener)
    # Assert
    assert json.loads(captured["body"])["name"] == "child"


def test_broker_forwards_sac_name_as_caller(listen_env) -> None:
    # Arrange — SAC_NAME identifies the parent inside the SIF.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    listen_env("NAME", "parent-bot")
    opener, captured = _opener_returning(b'{"name":"child","returncode":0}')
    # Act
    broker_start_to_host("child", opener=opener)
    # Assert
    assert json.loads(captured["body"])["caller"] == "parent-bot"


def test_broker_forwards_bearer_token_when_set(listen_env) -> None:
    # Arrange — runtime injects the bus bearer alongside the base URL.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    listen_env("LISTEN_BEARER", "tok-abc")
    opener, captured = _opener_returning(b'{"name":"child","returncode":0}')
    # Act
    broker_start_to_host("child", opener=opener)
    # Assert
    assert captured["headers"].get("authorization") == "Bearer tok-abc"


def test_broker_returns_server_returncode_on_success(listen_env) -> None:
    # Arrange — the server's agents_start handler returns rc=0 on success.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, _ = _opener_returning(b'{"name":"child","returncode":0,"stdout":"ok"}')
    # Act
    result = broker_start_to_host("child", opener=opener)
    # Assert
    assert result["returncode"] == 0


def test_broker_raises_when_listen_base_url_missing(listen_env) -> None:
    # Arrange — runtime forgot to inject SAC_LISTEN_BASE_URL.
    listen_env("LISTEN_BASE_URL", None)
    raised = False
    msg = ""
    # Act
    try:
        broker_start_to_host(
            "child", opener=lambda req, timeout=None: _FakeResp(b"", 200)
        )
    except InSifBrokerError as exc:
        raised = True
        msg = str(exc)
    # Assert — fail-loud with the env var name (operator can find the gap).
    assert raised is True and "SAC_LISTEN_BASE_URL" in msg


def test_broker_raises_on_host_listen_acl_deny(listen_env) -> None:
    # Arrange — host listen rejects with 403 (child caller, root-only policy).
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    listen_env("NAME", "worker-a")
    opener, _ = _opener_returning(b'{"error":"spawn denied"}', status=403)
    raised_status = None
    # Act
    try:
        broker_start_to_host("child", opener=opener)
    except InSifBrokerError as exc:
        raised_status = exc.status
    # Assert — 403 surfaces verbatim, never silently swallowed.
    assert raised_status == 403


# ---------------------------------------------------------------------------
# PR-α (lead msg d96a468c 2026-06-06): cohort one-shot diagnostic chain.
# ``broker_start_to_host`` and ``maybe_broker_in_sif_spawn`` forward
# ``foreground`` / ``one_shot`` to ``request_spawn`` → body fields → host
# listen's /agents handler argv. Three forwarding tests so a refactor
# that drops either kwarg trips a red test before the diagnostic value
# silently regresses.
# ---------------------------------------------------------------------------


def test_broker_forwards_foreground_to_body(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    broker_start_to_host("child", opener=opener, foreground=True)
    # Assert
    assert json.loads(captured["body"])["foreground"] is True


def test_broker_forwards_one_shot_to_body(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    broker_start_to_host("child", opener=opener, one_shot=True)
    # Assert
    assert json.loads(captured["body"])["one_shot"] is True


def test_broker_forwards_assume_yes_to_body(listen_env) -> None:
    # Arrange — consent-propagation fix (2026-07-05, paper-scitex-clew report).
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    broker_start_to_host("child", opener=opener, assume_yes=True)
    # Assert
    assert json.loads(captured["body"])["assume_yes"] is True


def test_broker_omits_assume_yes_when_default_false(listen_env) -> None:
    # Arrange — regression guard: default (no consent given) is unchanged.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    broker_start_to_host("child", opener=opener)
    # Assert
    assert "assume_yes" not in json.loads(captured["body"])


def test_maybe_broker_in_sif_forwards_assume_yes_to_body(sif_env, listen_env) -> None:
    # Arrange — flip is_in_sif() to True so the chokepoint actually brokers.
    from scitex_agent_container._lifecycle._in_sif_broker import (
        maybe_broker_in_sif_spawn,
    )

    sif_env("/path/to/parent.sif", key="APPTAINER_CONTAINER")
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    maybe_broker_in_sif_spawn("child", dry_run=False, opener=opener, assume_yes=True)
    # Assert
    assert json.loads(captured["body"])["assume_yes"] is True


def test_maybe_broker_in_sif_forwards_foreground_to_body(sif_env, listen_env) -> None:
    # Arrange — flip is_in_sif() to True so the chokepoint actually
    # brokers (rather than returning False for the bare-host path).
    from scitex_agent_container._lifecycle._in_sif_broker import (
        maybe_broker_in_sif_spawn,
    )

    sif_env("/path/to/parent.sif", key="APPTAINER_CONTAINER")
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    maybe_broker_in_sif_spawn(
        "child", dry_run=False, opener=opener, foreground=True, one_shot=True
    )
    # Assert
    body = json.loads(captured["body"])
    assert body["foreground"] is True and body["one_shot"] is True


# ---------------------------------------------------------------------------
# agent_start integration — in-SIF detection redirects to broker
# ---------------------------------------------------------------------------


class _RuntimeRecorder:
    """Honest fake runtime — records whether start() was called.

    The whole point of the in-SIF redirect is that we MUST NOT touch a
    runtime when we are inside a SIF. This recorder makes that
    observable without mocks.
    """

    def __init__(self) -> None:
        self.start_called = False

    def is_running(self, config: AgentConfig) -> bool:
        return False

    def start(self, config: AgentConfig, **kwargs: Any) -> bool:
        self.start_called = True
        return True


class _FakeHandover:
    def ensure_instance_uuid(self, config: AgentConfig) -> str:
        return "uuid"

    def hydrate_from_hub(self, config: AgentConfig) -> bool:
        return True

    def start_failback_poller(self, config: AgentConfig) -> None:
        pass


@pytest.fixture
def isolated_state(tmp_path: Path) -> Iterator[Path]:
    """Real isolated state.db + runtime dir + HOME (mirrors test__start_spawn_acl)."""
    db = tmp_path / "state.db"
    runtime_dir = tmp_path / "runtime"
    home = tmp_path / "home"
    home.mkdir()
    keys = {
        "SCITEX_AGENT_CONTAINER_STATE_DB": str(db),
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR": str(runtime_dir),
        "HOME": str(home),
        "SCITEX_DIR": str(home / ".scitex"),
    }
    saved = {k: os.environ.get(k) for k in keys}
    saved_default = state_db.DEFAULT_DB_PATH
    os.environ.update(keys)
    state_db.DEFAULT_DB_PATH = db
    state_db.init_schema(db)
    try:
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


def _write_spec(yaml_root: Path, name: str) -> Path:
    agent_dir = yaml_root / name
    agent_dir.mkdir(parents=True)
    spec = agent_dir / "spec.yaml"
    spec.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  host: ${HOSTNAME}\n"
        f"  workdir: {yaml_root / (name + '-work')}\n"
        "  apptainer:\n    image: /x.sif\n    binds: []\n"
        "  restart:\n    policy: on-failure\n    max_retries: 3\n"
        "  claude:\n"
        "    model: sonnet\n"
        "  health:\n"
        "    enabled: false\n"
        "    interval: 60\n"
    )
    return spec


def test_agent_start_in_sif_skips_local_runtime_start(
    isolated_state, sif_env, listen_env, tmp_path
) -> None:
    # Arrange — inside a SIF; the broker takes over BEFORE any runtime work.
    sif_env("/path/to/agent.sif", key="APPTAINER_CONTAINER")
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    spec = _write_spec(tmp_path / "yaml", "capsule-child")
    recorder = _RuntimeRecorder()
    opener, _ = _opener_returning(b'{"name":"capsule-child","returncode":0}')
    # Act — opener seam is injected via _in_sif_broker_opener kwarg (real seam).
    agent_start(
        str(spec),
        registry=Registry(registry_dir=tmp_path / "reg"),
        runtime_factory=lambda _c: recorder,
        handover_mod=_FakeHandover(),
        sleep_fn=lambda _s: None,
        in_sif_opener=opener,
    )
    # Assert — runtime.start() was NEVER called; the broker took over.
    assert recorder.start_called is False


def test_agent_start_in_sif_posts_to_host_listen(
    isolated_state, sif_env, listen_env, tmp_path
) -> None:
    # Arrange — confirms the redirect actually POSTs to /agents on the host.
    sif_env("/path/to/agent.sif", key="APPTAINER_CONTAINER")
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    listen_env("NAME", "parent-root")
    spec = _write_spec(tmp_path / "yaml", "capsule-child")
    opener, captured = _opener_returning(b'{"name":"capsule-child","returncode":0}')
    # Act
    agent_start(
        str(spec),
        registry=Registry(registry_dir=tmp_path / "reg"),
        runtime_factory=lambda _c: _RuntimeRecorder(),
        handover_mod=_FakeHandover(),
        sleep_fn=lambda _s: None,
        in_sif_opener=opener,
    )
    # Assert — exactly one POST to /agents with the child name in the body.
    body = json.loads(captured["body"])
    assert (
        captured["url"] == "http://host:9100/agents" and body["name"] == "capsule-child"
    )


def test_agent_start_in_sif_raises_when_listen_url_missing(
    isolated_state, sif_env, listen_env, tmp_path
) -> None:
    # Arrange — in-SIF but the runtime forgot to inject SAC_LISTEN_BASE_URL.
    sif_env("/path/to/agent.sif", key="APPTAINER_CONTAINER")
    listen_env("LISTEN_BASE_URL", None)
    spec = _write_spec(tmp_path / "yaml", "capsule-child")
    raised_msg = ""
    # Act
    try:
        agent_start(
            str(spec),
            registry=Registry(registry_dir=tmp_path / "reg"),
            runtime_factory=lambda _c: _RuntimeRecorder(),
            handover_mod=_FakeHandover(),
            sleep_fn=lambda _s: None,
            in_sif_opener=lambda req, timeout=None: _FakeResp(b"", 200),
        )
    except InSifBrokerError as exc:
        raised_msg = str(exc)
    # Assert — fail-loud names the missing env var so the operator can fix.
    assert "SAC_LISTEN_BASE_URL" in raised_msg


def test_agent_start_forwards_assume_yes_to_broker_body(
    isolated_state, sif_env, listen_env, tmp_path
) -> None:
    # Arrange — consent-propagation fix (2026-07-05, paper-scitex-clew
    # report): agent_start's own assume_yes kwarg must reach the wire.
    sif_env("/path/to/agent.sif", key="APPTAINER_CONTAINER")
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    spec = _write_spec(tmp_path / "yaml", "capsule-child")
    opener, captured = _opener_returning(b'{"name":"capsule-child","returncode":0}')
    # Act
    agent_start(
        str(spec),
        registry=Registry(registry_dir=tmp_path / "reg"),
        runtime_factory=lambda _c: _RuntimeRecorder(),
        handover_mod=_FakeHandover(),
        sleep_fn=lambda _s: None,
        in_sif_opener=opener,
        assume_yes=True,
    )
    # Assert
    assert json.loads(captured["body"])["assume_yes"] is True


def test_agent_start_not_in_sif_uses_local_runtime(
    isolated_state, sif_env, listen_env, tmp_path
) -> None:
    # Arrange — NO in-SIF env vars set → regression guard: local flow intact.
    sif_env(None, key="APPTAINER_CONTAINER")
    sif_env(None, key="SINGULARITY_CONTAINER")
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    spec = _write_spec(tmp_path / "yaml", "capsule-child")
    recorder = _RuntimeRecorder()

    # Opener that EXPLODES if called → proves the broker was NOT taken.
    def _exploding_opener(req, timeout=None):
        raise AssertionError("broker must not run when not in SIF")

    # Act
    agent_start(
        str(spec),
        registry=Registry(registry_dir=tmp_path / "reg"),
        runtime_factory=lambda _c: recorder,
        handover_mod=_FakeHandover(),
        sleep_fn=lambda _s: None,
        in_sif_opener=_exploding_opener,
    )
    # Assert — runtime.start() WAS called; broker not invoked.
    assert recorder.start_called is True
