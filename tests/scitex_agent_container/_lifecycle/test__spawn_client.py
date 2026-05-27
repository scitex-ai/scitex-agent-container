"""Tests for the in-container spawn-proxy HTTP client (ADR-0010 mech #3).

No-mocks pattern: the production module exposes an injectable
``opener`` callable that defaults to ``urllib.request.urlopen``. Tests
pass a hand-rolled opener that returns a ``urllib.response``-shaped
object (a real file-like body + ``status``). This mirrors the
established :mod:`scitex_agent_container._network.hub_client` test
style — no ``monkeypatch.setattr`` on the production internals.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name.
"""

from __future__ import annotations

import io
import json
import os
from typing import Iterator
from urllib import error as urlerror

import pytest

from scitex_agent_container._lifecycle._spawn_client import (
    SpawnRequestError,
    request_spawn,
)

# ---------------------------------------------------------------------------
# Fake response + opener factories (real objects, urllib protocol)
# ---------------------------------------------------------------------------


class _FakeResp:
    """A real callable response object matching the urllib contract.

    ``__enter__`` / ``__exit__`` make it usable as the context manager
    that ``urlrequest.urlopen(...)`` returns; ``read()`` yields the
    bytes body; ``status`` carries the HTTP code the production
    explicit-status guard reads.
    """

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
    """Build (opener, captured) — the opener records each call."""
    captured: dict = {}

    def opener(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        captured["headers"] = {k.lower(): v for k, v in dict(req.headers).items()}
        captured["timeout"] = timeout
        return _FakeResp(body, status)

    return opener, captured


def _opener_raising(exc: Exception):
    def opener(req, timeout=None):
        raise exc

    return opener


# ---------------------------------------------------------------------------
# Env fixtures (real save/restore — both SAC_ and SCITEX_AGENT_CONTAINER_)
# ---------------------------------------------------------------------------


_LISTEN_KEYS = (
    "SAC_LISTEN_BASE_URL",
    "SCITEX_AGENT_CONTAINER_LISTEN_BASE_URL",
    "SAC_LISTEN_BEARER",
    "SCITEX_AGENT_CONTAINER_LISTEN_BEARER",
    "SAC_NAME",
    "SCITEX_AGENT_CONTAINER_NAME",
)


@pytest.fixture
def listen_env() -> Iterator[callable]:
    """Yield a setter; clears + restores both prefixes for every key.

    Tests call ``set("LISTEN_BASE_URL", "http://h:9100")`` etc. — the
    SAC_ short form is written so :func:`_env.getenv` resolves it.
    """
    saved = {k: os.environ.get(k) for k in _LISTEN_KEYS}
    # Always start from a clean slate so a stray export in the operator's
    # shell can't make a "must fail" test silently pass.
    for k in _LISTEN_KEYS:
        os.environ.pop(k, None)

    def _set(suffix: str, value: str | None) -> None:
        long_form = f"SCITEX_AGENT_CONTAINER_{suffix}"
        short_form = f"SAC_{suffix}"
        if value is None:
            os.environ.pop(long_form, None)
            os.environ.pop(short_form, None)
        else:
            os.environ.pop(long_form, None)
            os.environ[short_form] = value

    try:
        yield _set
    finally:
        for k in _LISTEN_KEYS:
            os.environ.pop(k, None)
        for k, prev in saved.items():
            if prev is not None:
                os.environ[k] = prev


# ---------------------------------------------------------------------------
# Missing base URL — fail loud
# ---------------------------------------------------------------------------


def test_missing_base_url_raises_spawn_request_error(listen_env) -> None:
    # Arrange — listen_env yields with both base-url envs cleared.
    captured_message = ""
    # Act
    try:
        request_spawn("child", opener=lambda req, timeout=None: None)
    except SpawnRequestError as exc:
        captured_message = str(exc)
    # Assert — the error message must name the missing env var so the
    # operator knows which container var the apptainer runtime missed.
    assert "SAC_LISTEN_BASE_URL" in captured_message


def test_empty_child_name_raises_spawn_request_error(listen_env) -> None:
    # Arrange — even with a base URL set, empty child name must fail.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    raised = False
    # Act
    try:
        request_spawn("", opener=lambda req, timeout=None: None)
    except SpawnRequestError:
        raised = True
    # Assert
    assert raised is True


# ---------------------------------------------------------------------------
# Happy path — POST shape
# ---------------------------------------------------------------------------


def test_post_targets_agents_route_on_base_url(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100/")  # trailing slash stripped
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    request_spawn("c", opener=opener)
    # Assert
    assert captured["url"] == "http://host:9100/agents"


def test_post_uses_http_post_method(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    request_spawn("c", opener=opener)
    # Assert
    assert captured["method"] == "POST"


def test_post_body_includes_child_name(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    request_spawn("c", opener=opener)
    # Assert
    assert json.loads(captured["body"])["name"] == "c"


def test_post_body_defaults_caller_from_sac_name_env(listen_env) -> None:
    # Arrange — SAC_NAME present → resolved as caller automatically.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    listen_env("NAME", "parent-bot")
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    request_spawn("c", opener=opener)
    # Assert
    assert json.loads(captured["body"])["caller"] == "parent-bot"


def test_post_body_omits_caller_for_admin_path(listen_env) -> None:
    # Arrange — no SAC_NAME → admin path; the field is omitted entirely
    # so the server-side ``check_spawn(caller=None)`` admin branch runs.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    request_spawn("c", opener=opener)
    # Assert
    assert "caller" not in json.loads(captured["body"])


def test_explicit_caller_arg_overrides_sac_name_env(listen_env) -> None:
    # Arrange — env says "env-parent" but the explicit caller wins.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    listen_env("NAME", "env-parent")
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    request_spawn("c", caller="arg-parent", opener=opener)
    # Assert
    assert json.loads(captured["body"])["caller"] == "arg-parent"


def test_inline_spec_is_forwarded_with_overwrite_flag(listen_env) -> None:
    # Arrange — inline-spec path (server materialises under ~/.scitex/...).
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    spec = {"apiVersion": "scitex-agent-container/v3", "kind": "Agent", "spec": {}}
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    request_spawn("c", spec=spec, overwrite=True, opener=opener)
    # Assert
    body = json.loads(captured["body"])
    assert body["spec"] == spec and body["overwrite"] is True


def test_bearer_token_attached_as_authorization_header(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    listen_env("LISTEN_BEARER", "tok-abc")
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    request_spawn("c", opener=opener)
    # Assert
    assert captured["headers"].get("authorization") == "Bearer tok-abc"


def test_no_authorization_header_when_bearer_unset(listen_env) -> None:
    # Arrange — no bearer in env, none passed explicitly.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    request_spawn("c", opener=opener)
    # Assert
    assert "authorization" not in captured["headers"]


def test_content_type_header_is_application_json(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    request_spawn("c", opener=opener)
    # Assert
    assert captured["headers"].get("content-type") == "application/json"


# ---------------------------------------------------------------------------
# Success — returns parsed dict
# ---------------------------------------------------------------------------


def test_success_returns_parsed_response_dict(listen_env) -> None:
    # Arrange — server's agents_start returns the {name, returncode, ...} dict.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    body = json.dumps(
        {"name": "c", "returncode": 0, "stdout": "ok", "stderr": ""}
    ).encode()
    opener, _ = _opener_returning(body)
    # Act
    out = request_spawn("c", opener=opener)
    # Assert
    assert out["returncode"] == 0


# ---------------------------------------------------------------------------
# Failure modes — fail loud, never swallowed
# ---------------------------------------------------------------------------


def test_acl_403_deny_raises_with_reason_in_body(listen_env) -> None:
    # Arrange — server returns the ACL deny payload from deny_response().
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    deny_body = json.dumps(
        {"error": "ACL deny", "reason": "spawn denied: caller is a child"}
    ).encode()
    opener = _opener_raising(
        urlerror.HTTPError(
            "http://host:9100/agents", 403, "Forbidden", {}, io.BytesIO(deny_body)
        )
    )
    captured_status = None
    # Act
    try:
        request_spawn("c", opener=opener)
    except SpawnRequestError as exc:
        captured_status = exc.status
    # Assert
    assert captured_status == 403


def test_acl_403_deny_preserves_server_reason_in_body_attr(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    deny_body = json.dumps(
        {"error": "ACL deny", "reason": "spawn denied: caller is a child"}
    ).encode()
    opener = _opener_raising(
        urlerror.HTTPError(
            "http://host:9100/agents", 403, "Forbidden", {}, io.BytesIO(deny_body)
        )
    )
    # Act
    try:
        request_spawn("c", opener=opener)
    except SpawnRequestError as exc:
        captured_body = exc.body
    # Assert — the server's reason survives verbatim for caller logs.
    assert captured_body == {
        "error": "ACL deny",
        "reason": "spawn denied: caller is a child",
    }


def test_server_500_raises_spawn_request_error(listen_env) -> None:
    # Arrange — a 500 must propagate as a fail-loud error, not return None.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener = _opener_raising(
        urlerror.HTTPError(
            "http://host:9100/agents", 500, "boom", {}, io.BytesIO(b"")
        )
    )
    captured_status = None
    # Act
    try:
        request_spawn("c", opener=opener)
    except SpawnRequestError as exc:
        captured_status = exc.status
    # Assert
    assert captured_status == 500


def test_transport_error_raises_spawn_request_error(listen_env) -> None:
    # Arrange — listen unreachable (connection refused etc.).
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener = _opener_raising(urlerror.URLError("connection refused"))
    captured_status: object = "UNSET"
    # Act
    try:
        request_spawn("c", opener=opener)
    except SpawnRequestError as exc:
        captured_status = exc.status
    # Assert — no HTTP exchange happened, so status stays None.
    assert captured_status is None


def test_non_dict_2xx_body_raises_spawn_request_error(listen_env) -> None:
    # Arrange — server returns a JSON array instead of an object; must
    # fail loud rather than silently corrupt the caller's downstream use.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, _ = _opener_returning(b'[1,2,3]', status=200)
    raised = False
    # Act
    try:
        request_spawn("c", opener=opener)
    except SpawnRequestError:
        raised = True
    # Assert
    assert raised is True


def test_explicit_bearer_arg_overrides_env(listen_env) -> None:
    # Arrange — env has tok-env but the arg passes tok-arg.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    listen_env("LISTEN_BEARER", "tok-env")
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    request_spawn("c", bearer="tok-arg", opener=opener)
    # Assert
    assert captured["headers"].get("authorization") == "Bearer tok-arg"


def test_explicit_base_url_arg_overrides_env(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://env-host:9100")
    opener, captured = _opener_returning(b'{"name":"c","returncode":0}')
    # Act
    request_spawn("c", base_url="http://arg-host:9100", opener=opener)
    # Assert
    assert captured["url"] == "http://arg-host:9100/agents"
