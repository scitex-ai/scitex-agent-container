"""Actual status HTTP plus owned files/processes and authenticated live RPC."""
from __future__ import annotations

import json
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from starlette.testclient import TestClient
from websockets.sync.server import serve

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state
from scitex_agent_container._state import registry as registry_module
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc
from tests.scitex_agent_container._lifecycle.test__session_identity import _host_claim
from tests.scitex_agent_container._runners import test__hermes_owned_session_id as owned

_TOKEN = 'synthetic-status-listener-token'
_KEY = 'synthetic-fixture-key-0000000000'


@pytest.fixture
def status_case(tmp_path, env_save_restore):
    source = owned.owned_state.__wrapped__(tmp_path)
    original = next(source)
    state, marker, descriptor, _child, _config, _runtime, rows = _host_claim(original)
    calls = []
    baseline_root = _session_state.DEFAULT_STATE_ROOT
    baseline_registry = registry_module.REGISTRY_DIR
    registry_dir = tmp_path / 'registry'
    _session_state.DEFAULT_STATE_ROOT = state.parent
    registry_module.REGISTRY_DIR = registry_dir
    env_save_restore.set('SCITEX_AGENT_CONTAINER_REGISTRY_DIR', str(registry_dir))
    env_save_restore.set('SCITEX_AGENT_CONTAINER_RUNTIME_DIR', str(tmp_path / 'runtime'))
    env_save_restore.set('SAC_AGENT_SCOPE', 'user')
    spec = tmp_path / 'definitions' / state.name / 'spec.yaml'
    spec.parent.mkdir(parents=True)
    spec.write_text(yaml.safe_dump(explicit_doc({
        'host': '${HOSTNAME}', 'harness': 'hermes', 'runtime': 'tui',
        'workdir': str(tmp_path / 'work'),
    })))
    registry_module.Registry().add(state.name, str(spec), f'tui-{state.name}')

    def handle(connection):
        if parse_qs(urlsplit(connection.request.path).query).get('token') != [_KEY]:
            connection.close(code=1008)
            return
        for encoded in connection:
            request = json.loads(encoded)
            calls.append(request['method'])
            if request['method'] != 'session.active_list':
                connection.close(code=1008)
                return
            connection.send(json.dumps({'jsonrpc': '2.0', 'id': request['id'],
                                        'result': {'sessions': rows}}))

    def readiness(connection, request):
        if urlsplit(request.path).path == '/api/sessions':
            authorized = request.headers.get('Authorization') == f'Bearer {_KEY}'
            return connection.respond(200 if authorized else 401,
                                      json.dumps({'sessions': []}))
        return None

    try:
        with serve(handle, '127.0.0.1', 0, process_request=readiness) as gateway:
            descriptor['port'] = gateway.socket.getsockname()[1]
            for name, value in [('hermes-owned-session.json', marker),
                                ('hermes-tui-gateway.json', descriptor),
                                ('hermes-tui-gateway.ready.json', descriptor)]:
                (state / name).write_text(json.dumps(value))
            thread = threading.Thread(target=gateway.serve_forever, daemon=True)
            thread.start()
            # Endpoint-only client: no listener background lifecycle loops.
            client = TestClient(create_app(token=_TOKEN))
            try:
                yield client, state, marker, rows, calls
            finally:
                client.close()
                gateway.shutdown()
                thread.join(timeout=2)
    finally:
        _session_state.DEFAULT_STATE_ROOT = baseline_root
        registry_module.REGISTRY_DIR = baseline_registry
        source.close()


def _get(case):
    client, state, *_rest = case
    return client.get(f'/agents/{state.name}/status',
                      headers={'Authorization': f'Bearer {_TOKEN}'})


def test_host_alias_status_uses_proven_runtime_without_activating_or_sending(status_case):
    # Arrange — actual owned files and authenticated WebSocket observer.
    state = status_case[1]
    names = ('hermes-owned-session.json', 'hermes-tui-gateway.json',
             'hermes-tui-gateway.ready.json', 'hermes-api.key')
    before = {name: (state / name).read_bytes() for name in names}
    # Act
    response = _get(status_case)
    body = response.json()
    unchanged = before == {name: (state / name).read_bytes() for name in names}
    # Assert
    assert (
        response.status_code, body['session_id'], body['session_evidence'],
        set(status_case[4]), unchanged,
        body['runtime_control']['pending_clarification_observation'],
        body['runtime_control']['gateway_readiness']['status'],
    ) == (
        200, 'hermes-live-123', {'state': 'observed', 'reason': ''},
        {'session.active_list'}, True,
        {'state': 'unknown', 'reason': 'not_queried_passively'}, 'ok',
    )


@pytest.fixture(params=['wrong-pair', 'public-key', 'wrong-bind'])
def invalid_case(status_case, request):
    _client, state, marker, rows, _calls = status_case

    def wrong_pair():
        rows[0]['session_key'] = 'foreign-stored-session'

    def wrong_bind():
        marker['state_inode'] += 1
        (state / 'hermes-owned-session.json').write_text(json.dumps(marker))

    changes = {'wrong-pair': wrong_pair, 'wrong-bind': wrong_bind,
               'public-key': lambda: (state / 'hermes-api.key').chmod(0o644)}
    changes[request.param]()
    return status_case


def test_unproven_identity_preserves_status_without_claiming_a_session(invalid_case):
    # Arrange
    status_case = invalid_case
    # Act
    response = _get(status_case)
    body = response.json()
    # Assert
    assert (
        response.status_code, body['session_id'], body['session_id_source'],
        body['session_evidence'], 'activity' in body, 'liveness' in body,
        _KEY in response.text, 'foreign-stored-session' in response.text,
    ) == (
        200, None, 'unknown', {'state': 'unknown', 'reason': 'session_ownership_unproven'},
        True, True, False, False,
    )


def test_sdk_marker_precedence_skips_native_identity_query(status_case):
    # Arrange
    (status_case[1] / 'session_id').write_text('retained-sdk-marker')
    # Act
    response = _get(status_case)
    # Assert
    assert (response.status_code, response.json()['session_id'], status_case[4]) == (
        200, 'retained-sdk-marker', [])
