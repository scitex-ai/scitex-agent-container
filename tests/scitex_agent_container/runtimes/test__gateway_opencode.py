"""Tests for ``runtimes/_gateway_opencode.py`` + the opencode spec surface.

Serve HTTP is stubbed hermetically (stdlib ``http.server`` on a
loopback ephemeral port); the idempotency + busy/async/abort
SEMANTICS were measured live 2026-09-28 against a temp ``serve`` port
and are recorded in the driver's docstring, not re-proven here.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._opencode_approval import (
    parse_selected_opencode_approval_policy,
)
from scitex_agent_container.config._opencode_run_budget import (
    parse_selected_opencode_run_budget,
)
from scitex_agent_container.config._opencode_serve import (
    parse_selected_opencode_serve_port,
)
from scitex_agent_container.config._schema_compat import canonical_surface_errors
from scitex_agent_container.runtimes._gateway_harness import (
    GatewayHarness,
    GatewayHarnessError,
    get_gateway_harness,
)
from scitex_agent_container.runtimes._gateway_opencode import (
    OPENCODE_GATEWAY,
    SERVE_FILE,
    SESSION_MAP_FILE,
    session_directive,
)


def _opencode_config(**overrides):
    base = {"name": "worker", "harness": "opencode", "runtime": "tui"}
    base.update(overrides)
    return AgentConfig(**base)


def _opencode_spec(entry):
    return {
        "harness": "opencode",
        "runtime": "tui",
        "available_harnesses": {"opencode": entry},
    }


# ---------------------------------------------------------------------------
# Contract + guard
# ---------------------------------------------------------------------------


def test_opencode_driver_satisfies_the_protocol():
    # Arrange
    driver = OPENCODE_GATEWAY
    # Act
    conforms = isinstance(driver, GatewayHarness)
    # Assert
    assert conforms


def test_opencode_driver_registry_identity():
    # Arrange
    driver = OPENCODE_GATEWAY
    # Act
    identity = (driver.name, driver.spec_key)
    # Assert
    assert identity == ("opencode-tui", "opencode")


def test_registry_resolves_opencode():
    # Arrange
    name = "opencode"
    # Act
    resolved = get_gateway_harness(name)
    # Assert
    assert resolved is OPENCODE_GATEWAY


def test_owner_argv_pins_an_explicit_port():
    # Arrange
    config = _opencode_config(opencode_serve_port=4199)
    # Act
    argv = OPENCODE_GATEWAY.owner_argv(config, "/tmp/state")
    # Assert
    assert argv == [
        "/usr/bin/tini",
        "-s",
        "--",
        "python3",
        "-m",
        "scitex_agent_container.runtimes._opencode_tui_owner",
        "--state-dir",
        "/state/worker",
        "--agent-name",
        "worker",
        "--port",
        "4199",
        "--session-mode",
        "fresh",
        "--session-max-age-minutes",
        "",
        "--",
        "opencode",
        "attach",
        "http://127.0.0.1:4199",
        "--dir",
        "/home/agent/work",
    ]


def test_owner_argv_allocates_a_loopback_port_for_auto():
    # Arrange
    config = _opencode_config()
    # Act
    argv = OPENCODE_GATEWAY.owner_argv(config, "/tmp/state")
    # Assert
    assert argv[argv.index("--port") + 1] != "0"


def test_session_directive_defaults_to_fresh():
    # Arrange
    config = _opencode_config()
    config.claude.session = ""
    # Act
    directive = session_directive(config)
    # Assert
    assert directive == ("fresh", "")


def test_session_directive_reads_the_folded_entry_mode():
    # Arrange — post-load shape: the selected entry's session.mode
    # reaches config.claude.session through the loader's compat fold.
    config = _opencode_config()
    config.claude.session = "continue"
    # Act
    directive = session_directive(config)
    # Assert
    assert directive == ("continue", "")


def _example_spec_with_session(tmp_path, mode, max_age):
    """Copy the branch example with one session block swapped in."""
    from pathlib import Path

    example = (
        Path(__file__).parents[3]
        / "examples"
        / "providers"
        / "opencode-tui-spark.yaml"
    )
    text = example.read_text()
    text = text.replace(
        "      session:\n        mode: fresh\n        max_age_minutes: null",
        f"      session:\n        mode: {mode}\n        max_age_minutes: {max_age}",
    )
    agent_dir = tmp_path / "probe"
    agent_dir.mkdir()
    (agent_dir / "spec.yaml").write_text(text)
    return agent_dir / "spec.yaml"


def test_entry_continue_mode_folds_into_claude_session(tmp_path):
    # Arrange — the full load cascade on the branch example, not a
    # hand-built config: the selected entry's session.mode reaches
    # config.claude.session through the compat fold.
    from scitex_agent_container.config import load_config

    # Act
    config = load_config(str(_example_spec_with_session(tmp_path, "continue", 60)))
    # Assert
    assert session_directive(config) == ("continue", "")


def test_entry_max_age_folds_into_continue_window(tmp_path):
    # Arrange
    from scitex_agent_container.config import load_config

    # Act
    config = load_config(str(_example_spec_with_session(tmp_path, "fresh", 90)))
    # Assert
    assert config.claude.continue_max_age_minutes == 90


def test_session_directive_resume_requires_a_resume_id():
    # Arrange
    config = _opencode_config()
    config.claude.session = "resume"

    def action():
        return session_directive(config)

    # Act
    run = action
    # Assert
    with pytest.raises(GatewayHarnessError, match="resume_id"):
        run()


def test_session_directive_resume_carries_the_resume_id():
    # Arrange
    config = _opencode_config()
    config.claude.session = "resume"
    config.claude.resume_id = "ses_pinned"
    # Act
    directive = session_directive(config)
    # Assert
    assert directive == ("resume", "ses_pinned")


def test_session_directive_refuses_an_unknown_mode():
    # Arrange
    config = _opencode_config()
    config.claude.session = "bogus"

    def action():
        return session_directive(config)

    # Act
    run = action
    # Assert
    with pytest.raises(GatewayHarnessError, match="fresh, continue, or resume"):
        run()


def test_owner_argv_carries_the_resume_session():
    # Arrange
    config = _opencode_config()
    config.claude.session = "resume"
    config.claude.resume_id = "ses_pinned"
    # Act
    argv = OPENCODE_GATEWAY.owner_argv(config, "/tmp/state")
    # Assert
    assert argv[argv.index("--resume-session") + 1] == "ses_pinned"


# ---------------------------------------------------------------------------
# Spec surface: validation (spec-only edits, per-agent)
# ---------------------------------------------------------------------------


def _opencode_errors(entry):
    raw = {
        "spec": {
            "harness": "opencode",
            "runtime": "tui",
            "available_harnesses": {"opencode": entry},
        }
    }
    return [e for e in canonical_surface_errors(raw) if "opencode" in e]


def test_valid_opencode_entry_passes_validation():
    # Arrange
    entry = {
        "session": {"mode": "fresh", "max_age_minutes": None},
        "approval_policy": "never",
        "run_budget_seconds": 3600,
        "serve": {"port": 4199},
    }
    # Act
    errors = _opencode_errors(entry)
    # Assert
    assert errors == []


@pytest.mark.parametrize(
    ("entry", "fragment"),
    [
        ({"approval_policy": "yolo"}, "approval_policy"),
        ({"run_budget_seconds": -5}, "positive integer"),
        ({"serve": {"port": "fast"}}, "serve.port"),
        ({"serve": 4199}, "must be a mapping"),
    ],
)
def test_invalid_opencode_values_fail_loud(entry, fragment):
    # Arrange
    errors = _opencode_errors(entry)
    # Act
    matches = [error for error in errors if fragment in error]
    # Assert
    assert matches != []


def test_serve_block_is_rejected_on_other_harnesses():
    # Arrange
    raw = {
        "spec": {
            "harness": "hermes",
            "available_harnesses": {"hermes": {"serve": {"port": 1}}},
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert any("only valid for the opencode harness" in e for e in errors)


def test_hermes_keys_stay_rejected_on_the_opencode_entry():
    # Arrange
    raw = {
        "spec": {
            "harness": "opencode",
            "available_harnesses": {"opencode": {"background_review": True}},
        }
    }
    # Act
    errors = canonical_surface_errors(raw)
    # Assert
    assert any("only valid for the Hermes harness" in e for e in errors)


def test_parsers_read_the_selected_opencode_entry():
    # Arrange
    spec = _opencode_spec(
        {"approval_policy": "ask", "run_budget_seconds": 600, "serve": {"port": 1}}
    )
    # Act
    values = (
        parse_selected_opencode_approval_policy(spec),
        parse_selected_opencode_run_budget(spec),
        parse_selected_opencode_serve_port(spec),
    )
    # Assert
    assert values == ("ask", 600, 1)


def test_parsers_default_when_opencode_is_not_selected():
    # Arrange
    spec = dict(_opencode_spec({}), harness="hermes")
    # Act
    values = (
        parse_selected_opencode_approval_policy(spec),
        parse_selected_opencode_run_budget(spec),
        parse_selected_opencode_serve_port(spec),
    )
    # Assert
    assert values == ("never", None, None)


def test_per_agent_options_default_to_closed_approvals():
    # Arrange
    config = _opencode_config()
    # Act
    options = OPENCODE_GATEWAY.parse_agent_options(config)
    # Assert
    assert options["approval_policy"] == "never"


def test_per_agent_options_read_the_spec_block():
    # Arrange
    config = _opencode_config(
        opencode_approval_policy="ask",
        opencode_run_budget_seconds=600,
        opencode_serve_port=4199,
    )
    # Act
    options = OPENCODE_GATEWAY.parse_agent_options(config)
    # Assert
    assert options["serve_port"] == 4199


def test_per_agent_options_carry_the_resolved_engine():
    # Arrange
    config = _opencode_config(engine_key="e", model="m")
    # Act
    options = OPENCODE_GATEWAY.parse_agent_options(config)
    # Assert
    assert options["engine"] == {"key": "e", "model": "m"}


# ---------------------------------------------------------------------------
# Hermetic serve stub
# ---------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    """Scripted opencode serve: records requests, plays canned answers."""

    script: dict = {"busy": set(), "posts": [], "aborts": [], "messages": []}

    def log_message(self, *args):
        pass

    def _send(self, code, payload=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/session/status":
            payload = {sid: {"type": "busy"} for sid in self.script["busy"]}
            self._send(200, payload)
        elif "/message" in self.path:
            self._send(200, list(self.script["messages"]))
        else:
            self._send(404, {"error": "unknown"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/session":
            self._send(200, {"id": "ses_stub1"})
        elif self.path.endswith("/prompt_async"):
            self.script["posts"].append(("async", body))
            self._send(204, None)
        elif self.path.endswith("/message"):
            self.script["posts"].append(("sync", body))
            self._send(200, {"info": {"id": "msg_stub1"}, "parts": []})
        elif self.path.endswith("/abort"):
            self.script["aborts"].append(self.path)
            self._send(200, True)
        else:
            self._send(404, {"error": "unknown"})


@pytest.fixture()
def stub_serve(tmp_path):
    _StubHandler.script = {"busy": set(), "posts": [], "aborts": [], "messages": []}
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    (tmp_path / SERVE_FILE).write_text(json.dumps({"url": base_url}))
    yield tmp_path, base_url, _StubHandler.script
    server.shutdown()


def _options():
    return {"engine": {"key": "free", "model": "muse-spark-1.3-contributor-free"}}


def test_idle_submit_completes_synchronously(stub_serve):
    # Arrange
    state_dir, _base_url, script = stub_serve
    # Act
    receipt = OPENCODE_GATEWAY.submit_turn(
        state_dir, "worker", "hi", options=_options()
    )
    # Assert
    assert receipt["status"] == "completed"


def test_submit_mints_a_stable_caller_message_id(stub_serve):
    # Arrange
    state_dir, _base_url, script = stub_serve
    # Act
    receipt = OPENCODE_GATEWAY.submit_turn(
        state_dir, "worker", "hi", options=_options()
    )
    # Assert
    assert receipt["message_id"].startswith("msg_sac_worker_")


def test_submit_addresses_the_resolved_engine(stub_serve):
    # Arrange
    state_dir, _base_url, script = stub_serve
    # Act
    OPENCODE_GATEWAY.submit_turn(state_dir, "worker", "hi", options=_options())
    # Assert
    assert script["posts"][0][1]["model"] == {
        "providerID": "sac-free",
        "modelID": "muse-spark-1.3-contributor-free",
    }


def _busy_stub(stub_serve):
    # Arrange
    state_dir, _base_url, script = stub_serve
    script["busy"] = {"ses_stub1"}
    (state_dir / SESSION_MAP_FILE).write_text(json.dumps({"sac:worker": "ses_stub1"}))
    # Act
    busy = script["busy"]
    # Assert
    assert busy == {"ses_stub1"}
    return state_dir, script


def test_busy_steer_is_accepted_asynchronously(stub_serve):
    # Arrange
    state_dir, script = _busy_stub(stub_serve)
    # Act
    receipt = OPENCODE_GATEWAY.submit_turn(
        state_dir, "worker", "later", options=_options()
    )
    # Assert
    assert receipt["status"] == "accepted"


def test_explicit_queue_is_accepted_asynchronously(stub_serve):
    # Arrange
    state_dir, script = _busy_stub(stub_serve)
    # Act
    receipt = OPENCODE_GATEWAY.submit_turn(
        state_dir, "worker", "q", delivery_mode="queue", options=_options()
    )
    # Assert
    assert receipt["status"] == "accepted"


def test_caller_message_id_replays_the_same_key(stub_serve):
    # Arrange
    state_dir, script = _busy_stub(stub_serve)
    # Act
    receipt = OPENCODE_GATEWAY.submit_turn(
        state_dir,
        "worker",
        "again",
        delivery_mode="queue",
        message_id="msg_client_1",
        options=_options(),
    )
    # Assert
    assert receipt["message_id"] == "msg_client_1"


def test_stub_receives_the_client_message_id(stub_serve):
    # Arrange
    state_dir, script = _busy_stub(stub_serve)
    # Act
    OPENCODE_GATEWAY.submit_turn(
        state_dir,
        "worker",
        "again",
        delivery_mode="queue",
        message_id="msg_client_1",
        options=_options(),
    )
    # Assert
    assert script["posts"][-1][1]["messageID"] == "msg_client_1"


def test_caller_message_id_without_prefix_is_prefixed_deterministically(stub_serve):
    # Arrange
    state_dir, script = _busy_stub(stub_serve)
    # Act
    first = OPENCODE_GATEWAY.submit_turn(
        state_dir,
        "worker",
        "again",
        delivery_mode="queue",
        message_id="sac:worker:42",
        options=_options(),
    )
    # Assert
    assert first["message_id"] == "msg_sac_worker_42"


def test_abort_confirms_when_a_session_exists(stub_serve):
    # Arrange
    state_dir, script = _busy_stub(stub_serve)
    # Act
    confirmed = OPENCODE_GATEWAY.abort_turn(state_dir, "worker")
    # Assert
    assert confirmed is True


def test_abort_without_a_session_is_a_benign_false(stub_serve):
    # Arrange
    state_dir, _base_url, _script = stub_serve
    # Act
    confirmed = OPENCODE_GATEWAY.abort_turn(state_dir, "stranger")
    # Assert
    assert confirmed is False


def test_missing_serve_state_fails_states_loud(tmp_path):
    # Arrange
    missing = tmp_path
    # Act
    def action():
        return OPENCODE_GATEWAY.session_states(missing)

    # Assert
    with pytest.raises(GatewayHarnessError, match="not started"):
        action()


def test_missing_serve_state_fails_submit_loud(tmp_path):
    # Arrange
    missing = tmp_path

    def action():
        return OPENCODE_GATEWAY.submit_turn(
            missing, "worker", "hi", options=_options()
        )

    # Act
    run = action
    # Assert
    with pytest.raises(GatewayHarnessError, match="not started"):
        run()


def test_submit_without_engine_options_fails_loud(stub_serve):
    # Arrange
    state_dir, _base_url, _script = stub_serve

    def action():
        return OPENCODE_GATEWAY.submit_turn(state_dir, "worker", "hi", options={})

    # Act
    run = action
    # Assert
    with pytest.raises(GatewayHarnessError, match=r"options\['engine'\]"):
        run()


def test_list_messages_returns_the_stable_session_records(stub_serve):
    # Arrange
    state_dir, _base_url, script = stub_serve
    script["messages"] = [
        {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "hi"}]}
    ]
    (state_dir / SESSION_MAP_FILE).write_text(
        json.dumps({"sac:worker": "ses_stub1"})
    )
    # Act
    records = OPENCODE_GATEWAY.list_messages(state_dir, "worker")
    # Assert
    assert records[0]["parts"][0]["text"] == "hi"


def test_list_messages_refuses_an_agent_that_never_submitted(stub_serve):
    # Arrange
    state_dir, _base_url, _script = stub_serve

    def action():
        return OPENCODE_GATEWAY.list_messages(state_dir, "stranger")

    # Act
    run = action
    # Assert
    with pytest.raises(GatewayHarnessError, match="no session yet"):
        run()


# ---------------------------------------------------------------------------
# Derived profile: credential-free, spec-shaped
# ---------------------------------------------------------------------------


def _plan(**overrides):
    endpoint = SimpleNamespace(
        url="http://127.0.0.1:18779/v1/chat/completions",
        protocol="openai-chat-completions",
        auth_kind="bearer",
        auth_env="SCITEX_GENAI_GATEWAY_API_KEY",
    )
    engine = SimpleNamespace(key="free", model_id="muse-spark-1.3-contributor-free")
    base = {"harness": "opencode", "engine": engine, "endpoint": endpoint}
    base.update(overrides)
    return SimpleNamespace(**base)


def test_compile_profile_names_the_provider_model(env_save_restore):
    # Arrange
    env_save_restore.set(
        "SCITEX_GENAI_GATEWAY_API_KEY", "secret-must-not-be-serialized"
    )
    plan = _plan()
    # Act
    profile = OPENCODE_GATEWAY.compile_profile(
        plan, workdir="/work", options={"approval_policy": "never"}
    )
    # Assert
    assert profile["model"] == "sac-free/muse-spark-1.3-contributor-free"


def test_compile_profile_points_at_the_gateway_base_url(env_save_restore):
    # Arrange
    env_save_restore.set(
        "SCITEX_GENAI_GATEWAY_API_KEY", "secret-must-not-be-serialized"
    )
    plan = _plan()
    # Act
    profile = OPENCODE_GATEWAY.compile_profile(
        plan, workdir="/work", options={"approval_policy": "never"}
    )
    # Assert
    assert profile["provider"]["sac-free"]["options"]["baseURL"] == (
        "http://127.0.0.1:18779/v1"
    )


def test_compile_profile_references_the_key_by_name_only(env_save_restore):
    # Arrange
    env_save_restore.set(
        "SCITEX_GENAI_GATEWAY_API_KEY", "secret-must-not-be-serialized"
    )
    plan = _plan()
    # Act
    profile = OPENCODE_GATEWAY.compile_profile(
        plan, workdir="/work", options={"approval_policy": "never"}
    )
    # Assert
    assert profile["provider"]["sac-free"]["options"]["apiKey"] == (
        "{env:SCITEX_GENAI_GATEWAY_API_KEY}"
    )


def test_compile_profile_never_serializes_the_secret(env_save_restore):
    # Arrange
    env_save_restore.set(
        "SCITEX_GENAI_GATEWAY_API_KEY", "secret-must-not-be-serialized"
    )
    plan = _plan()
    # Act
    profile = OPENCODE_GATEWAY.compile_profile(
        plan, workdir="/work", options={"approval_policy": "never"}
    )
    # Assert
    assert "secret-must-not-be-serialized" not in repr(profile)


def test_compile_profile_maps_closed_approvals_to_allow(env_save_restore):
    # Arrange
    env_save_restore.set(
        "SCITEX_GENAI_GATEWAY_API_KEY", "secret-must-not-be-serialized"
    )
    plan = _plan()
    # Act
    profile = OPENCODE_GATEWAY.compile_profile(
        plan, workdir="/work", options={"approval_policy": "never"}
    )
    # Assert
    assert profile["permission"] == "allow"


def test_compile_profile_honours_ask(env_save_restore):
    # Arrange
    env_save_restore.set(
        "SCITEX_GENAI_GATEWAY_API_KEY", "secret-must-not-be-serialized"
    )
    plan = _plan()
    # Act
    profile = OPENCODE_GATEWAY.compile_profile(
        plan, workdir="/work", options={"approval_policy": "ask"}
    )
    # Assert
    assert profile["permission"] == "ask"


def test_compile_profile_refuses_foreign_harnesses():
    # Arrange
    plan = _plan(harness="hermes")

    def action():
        return OPENCODE_GATEWAY.compile_profile(plan, workdir="/work", options={})

    # Act
    run = action
    # Assert
    with pytest.raises(ValueError, match="harness 'hermes'"):
        run()


def test_compile_profile_routes_responses_through_the_openai_sdk(
    env_save_restore,
):
    # Arrange
    env_save_restore.set(
        "SCITEX_GENAI_GATEWAY_API_KEY", "secret-must-not-be-serialized"
    )
    responses = SimpleNamespace(
        url="http://127.0.0.1:18765/v1/responses",
        protocol="openai-responses",
        auth_kind="bearer",
        auth_env="SCITEX_GENAI_GATEWAY_API_KEY",
    )
    plan = _plan(endpoint=responses)
    # Act
    profile = OPENCODE_GATEWAY.compile_profile(
        plan, workdir="/work", options={"approval_policy": "never"}
    )
    # Assert
    assert profile["provider"]["sac-free"]["npm"] == "@ai-sdk/openai"


def test_compile_profile_strips_the_responses_suffix(env_save_restore):
    # Arrange
    env_save_restore.set(
        "SCITEX_GENAI_GATEWAY_API_KEY", "secret-must-not-be-serialized"
    )
    responses = SimpleNamespace(
        url="http://127.0.0.1:18765/v1/responses",
        protocol="openai-responses",
        auth_kind="bearer",
        auth_env="SCITEX_GENAI_GATEWAY_API_KEY",
    )
    plan = _plan(endpoint=responses)
    # Act
    profile = OPENCODE_GATEWAY.compile_profile(
        plan, workdir="/work", options={"approval_policy": "never"}
    )
    # Assert
    assert profile["provider"]["sac-free"]["options"]["baseURL"] == (
        "http://127.0.0.1:18765/v1"
    )


def test_compile_profile_refuses_native_protocols():
    # Arrange
    native = SimpleNamespace(
        url="",
        protocol="hermes-native:opencode-go",
        auth_kind="bearer",
        auth_env="OPENCODE_GO_API_KEY",
    )
    plan = _plan(endpoint=native)

    def action():
        return OPENCODE_GATEWAY.compile_profile(plan, workdir="/work", options={})

    # Act
    run = action
    # Assert
    with pytest.raises(ValueError, match="openai-chat-completions or openai-responses"):
        run()


def test_compile_profile_disables_task_when_spawning_is_forbidden(env_save_restore):
    # Arrange
    env_save_restore.set(
        "SCITEX_GENAI_GATEWAY_API_KEY", "secret-must-not-be-serialized"
    )
    plan = _plan(may_spawn=False)
    # Act
    profile = OPENCODE_GATEWAY.compile_profile(
        plan, workdir="/work", options={"approval_policy": "never"}
    )
    # Assert
    assert profile["permission"] == {"*": "allow", "task": "deny"}


def test_compile_profile_keeps_flat_permission_when_spawning_is_allowed(
    env_save_restore,
):
    # Arrange
    env_save_restore.set(
        "SCITEX_GENAI_GATEWAY_API_KEY", "secret-must-not-be-serialized"
    )
    plan = _plan(may_spawn=True)
    # Act
    profile = OPENCODE_GATEWAY.compile_profile(
        plan, workdir="/work", options={"approval_policy": "never"}
    )
    # Assert
    assert profile["permission"] == "allow"


def test_compile_profile_carries_the_engine_reasoning_effort(env_save_restore):
    # Arrange
    env_save_restore.set(
        "SCITEX_GENAI_GATEWAY_API_KEY", "secret-must-not-be-serialized"
    )
    engine = SimpleNamespace(key="free", model_id="m", reasoning_effort="xhigh")
    plan = _plan(engine=engine)
    # Act
    profile = OPENCODE_GATEWAY.compile_profile(
        plan, workdir="/work", options={"approval_policy": "never"}
    )
    # Assert
    assert profile["provider"]["sac-free"]["models"]["m"]["options"] == {
        "reasoningEffort": "xhigh"
    }


def test_compile_profile_omits_effort_options_when_unset(env_save_restore):
    # Arrange
    env_save_restore.set(
        "SCITEX_GENAI_GATEWAY_API_KEY", "secret-must-not-be-serialized"
    )
    plan = _plan()
    # Act
    profile = OPENCODE_GATEWAY.compile_profile(
        plan, workdir="/work", options={"approval_policy": "never"}
    )
    # Assert
    assert "options" not in profile["provider"]["sac-free"]["models"][
        "muse-spark-1.3-contributor-free"
    ]
