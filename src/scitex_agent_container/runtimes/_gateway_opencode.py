"""Opencode gateway driver — implementation #2 of the gateway contract.

Speaks to ``opencode serve`` over loopback HTTP. Semantics were probed
2026-09-28 against a temp ``serve`` port (never the production ``:4096``):

* ``POST /session`` creates; ``GET /session/status`` maps live sessions
  to ``busy`` (idle sessions are omitted — the ``active_list`` shape);
* ``POST /session/:id/message`` (sync, waits) is accepted even while
  busy and completes in order; ``POST .../prompt_async`` answers 204;
* ``POST /session/:id/abort`` answers ``true`` and returns to idle;
* a client-set ``messageID`` is honored: reposting it returns the SAME
  run instead of duplicating — the idempotency-key equivalent;
* ``POST /session/:id/fork`` and ``--session``/``--continue`` resume.

The driver never launches anything itself: ``owner_argv`` builds the
owner-module argv (explicit ``serve.port`` or a free-loopback pick —
the single allocation point), and the owner persists the loopback URL
into the incarnation state this driver reads.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from ._gateway_types import GatewayHarnessError

__all__ = [
    "OPENCODE_GATEWAY",
    "SERVE_FILE",
    "SESSION_MAP_FILE",
    "STARTUP_FILE",
    "effective_serve_port",
    "post_session_message",
    "session_directive",
]

#: Incarnation state file carrying the loopback serve URL (written by
#: the start path; read by every RPC here).
SERVE_FILE = "opencode-serve.json"

#: Incarnation state file mapping the stable SAC session key to the
#: opencode ``ses_*`` id (written on first submit).
SESSION_MAP_FILE = "opencode-sessions.json"

#: Incarnation state file carrying engine-bound first turns compiled
#: from ``spec.startup_prompts`` (written by materialize when prompts
#: are non-empty; consumed by the owner before the TUI attaches).
#: ``{provider_id, model_id, texts[]}`` — secrets stay ``{env:}``
#: templates in the profile; this file carries no credentials.
STARTUP_FILE = "opencode-startup.json"

#: Startup turns may run a full reasoning model; the bridge default
#: would abandon slow useful work (the Qwen lesson: client deadline
#: later than upstream).
STARTUP_TURN_TIMEOUT_S = 600.0


def _serve_url(state_dir: Path) -> str:
    """Return the persisted loopback serve URL or fail loud."""
    try:
        payload = json.loads((Path(state_dir) / SERVE_FILE).read_text())
    except (OSError, ValueError):
        payload = {}
    url = payload.get("url") if isinstance(payload, dict) else None
    if not url:
        raise GatewayHarnessError(
            "Opencode serve state is absent; the agent is not started "
            f"({SERVE_FILE} missing under {state_dir})."
        )
    return str(url)


def _request(
    method: str, url: str, payload: dict[str, Any] | None, timeout_s: float
) -> tuple[int, Any]:
    """One JSON round-trip; transport failures become GatewayHarnessError."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        raise GatewayHarnessError(
            f"Opencode serve {method} {url} answered HTTP {exc.code}: {detail}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GatewayHarnessError(
            f"Opencode serve at {url} is unreachable: {exc}"
        ) from exc


def _resolve_session(
    state_dir: Path, base_url: str, agent_name: str, timeout_s: float
) -> str:
    """Return the stable session id, creating it on first submit."""
    state_dir = Path(state_dir)
    key = f"sac:{agent_name}"
    mapped = _mapped_session_id(state_dir, agent_name)
    if mapped is not None:
        return mapped
    _status, created = _request(
        "POST", f"{base_url}/session", {"title": key}, timeout_s
    )
    session_id = str(created["id"])
    map_path = state_dir / SESSION_MAP_FILE
    try:
        known = json.loads(map_path.read_text())
    except (OSError, ValueError):
        known = {}
    if not isinstance(known, dict):
        known = {}
    map_path.write_text(json.dumps({**known, key: session_id}))
    return session_id


def _pick_free_loopback_port() -> int:
    """Ask the kernel for an unused TCP port on 127.0.0.1.

    Bind + immediately close (the ``_lifecycle/_broker_self`` doctrine):
    the serve subprocess spawned moments later almost always reacquires
    it on an uncontended loopback. ``auto`` pays this race instead of
    serve-port discovery; an explicit ``serve.port`` skips it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def effective_serve_port(config: Any) -> int:
    """Return the serve port for this incarnation's argv build."""
    port = getattr(config, "opencode_serve_port", None)
    if port is None:
        return _pick_free_loopback_port()
    return int(port)


def session_directive(config: Any) -> tuple[str, str]:
    """Return the ``(mode, resume_id)`` session directive for ``config``.

    Reads ``config.claude.session`` exactly like the Hermes TUI does:
    the selected harness entry's ``session.mode`` reaches that field
    through the loader's compat fold, and the top-level
    ``spec.session`` shortcut overrides it through the same cascade —
    so no opencode-specific session surface is needed. ``resume``
    requires ``config.claude.resume_id`` and fails loud without it
    (the Hermes resume contract).
    """
    claude = getattr(config, "claude", None)
    mode = str(getattr(claude, "session", "") or "").strip().lower() or "fresh"
    if mode == "resume":
        resume_id = str(getattr(claude, "resume_id", "") or "").strip()
        if not resume_id:
            raise GatewayHarnessError(
                "Opencode session mode 'resume' requires spec.claude.resume_id "
                "or the CLI --resume <session-id>; refusing to degrade to a "
                "fresh session"
            )
        return "resume", resume_id
    if mode not in {"fresh", "continue"}:
        raise GatewayHarnessError(
            f"Opencode session mode must be fresh, continue, or resume, "
            f"got {mode!r}"
        )
    return mode, ""


def post_session_message(
    base_url: str,
    session_id: str,
    *,
    provider_id: str,
    model_id: str,
    text: str,
    message_id: str,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """POST one synchronous message; return the completed-turn receipt.

    Shared by the driver (bridge turns) and the owner (startup turns):
    one round-trip shape, one receipt shape. ``message_id`` must already
    be serve-acceptable (see :func:`message_key`).
    """
    body = {
        "messageID": message_id,
        "model": {"providerID": provider_id, "modelID": model_id},
        "parts": [{"type": "text", "text": text}],
    }
    _status_code, _completed = _request(
        "POST", f"{base_url}/session/{session_id}/message", body, timeout_s
    )
    return {
        "status": "completed",
        "session_id": session_id,
        "message_id": message_id,
    }


def message_key(agent_name: str, message_id: str | None) -> str:
    """Return a serve-acceptable idempotency key for one turn.

    The serve API rejects client-set ``messageID`` values that do not
    start with ``msg`` (HTTP 400 ``Expected a string starting with
    "msg"`` — measured 2026-09-29 against the canary serve). Generated
    keys embed the SAC agent name after a ``msg_sac_`` prefix; caller
    keys are prefixed deterministically when needed, so replaying the
    same caller key still addresses the same run. Non-word characters
    are folded to ``_`` (ids travel in JSON bodies and URL paths).
    """
    if message_id:
        key = message_id if message_id.startswith("msg") else f"msg_{message_id}"
    else:
        safe_agent = re.sub(r"[^A-Za-z0-9_-]", "_", agent_name)
        key = f"msg_sac_{safe_agent}_{uuid.uuid4().hex[:12]}"
    return re.sub(r"[^A-Za-z0-9_-]", "_", key)


def _mapped_session_id(state_dir: Path, agent_name: str) -> str | None:
    """Return the mapped ``ses_*`` id for ``sac:<agent>``, if any.

    Tolerates both map shapes: legacy plain-string values and the
    ``{"id", "created_at"}`` objects the session-aware owner writes.
    """
    try:
        known = json.loads((Path(state_dir) / SESSION_MAP_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(known, dict):
        return None
    raw = known.get(f"sac:{agent_name}")
    if isinstance(raw, dict):
        raw = raw.get("id")
    return str(raw) if raw else None


def _strip_api_suffix(url: str, protocol: str) -> str:
    """Return the provider base URL (mirrors the Hermes ``_api_root``)."""
    suffix = {
        "openai-chat-completions": "/chat/completions",
        "openai-responses": "/responses",
    }[protocol]
    return url.removesuffix(suffix).rstrip("/")


class _OpencodeGatewayHarness:
    """Gateway-contract driver over the opencode serve HTTP API."""

    @property
    def name(self) -> str:
        return "opencode-tui"

    @property
    def spec_key(self) -> str:
        return "opencode"

    def owner_argv(self, config: Any, state_dir: Path) -> list[str]:
        """Owner-module argv: loopback serve as owner, TUI attached.

        ``/state/<name>`` mirrors the Hermes owner literal. The attach
        command runs inside the container after ``--``; the owner
        publishes ``opencode-serve.json`` once serve is healthy.
        """
        port = effective_serve_port(config)
        url = f"http://127.0.0.1:{port}"
        workdir = str(getattr(config, "workdir", "") or "/home/agent/work")
        mode, resume_id = session_directive(config)
        max_age = getattr(
            getattr(config, "claude", None), "continue_max_age_minutes", None
        )
        argv = [
            "/usr/bin/tini",
            "-s",
            "--",
            "python3",
            "-m",
            "scitex_agent_container.runtimes._opencode_tui_owner",
            "--state-dir",
            f"/state/{config.name}",
            "--agent-name",
            config.name,
            "--port",
            str(port),
            "--session-mode",
            mode,
            "--session-max-age-minutes",
            "" if max_age is None else str(max_age),
        ]
        if mode == "resume":
            argv += ["--resume-session", resume_id]
        return argv + [
            "--",
            "opencode",
            "attach",
            url,
            "--dir",
            workdir,
        ]

    def session_states(
        self, state_dir: Path, *, timeout_s: float = 10.0
    ) -> dict[str, str]:
        _status_code, payload = _request(
            "GET", f"{_serve_url(state_dir)}/session/status", None, timeout_s
        )
        states: dict[str, str] = {}
        if isinstance(payload, dict):
            for session_id, row in payload.items():
                kind = row.get("type") if isinstance(row, dict) else None
                states[str(session_id)] = "busy" if kind == "busy" else "idle"
        return states

    def list_messages(
        self,
        state_dir: Path,
        agent_name: str,
        *,
        timeout_s: float = 10.0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return raw ``{info, parts}`` records for the stable session.

        Reads the session id from ``opencode-sessions.json`` (written on
        first submit) — never the sqlite store. Fail-loud when the agent
        never submitted or the server is unreachable.
        """
        base_url = _serve_url(state_dir)
        session_id = _mapped_session_id(state_dir, agent_name)
        if not session_id:
            raise GatewayHarnessError(
                f"Opencode agent {agent_name!r} has no session yet "
                f"({SESSION_MAP_FILE} missing {agent_name!r}); "
                "it may not have started a session yet."
            )
        _status_code, payload = _request(
            "GET",
            f"{base_url}/session/{session_id}/message?limit={int(limit)}",
            None,
            timeout_s,
        )
        if not isinstance(payload, list):
            raise GatewayHarnessError(
                f"Opencode serve returned a malformed message list: {payload!r}"
            )
        return [row for row in payload if isinstance(row, dict)]

    def submit_turn(
        self,
        state_dir: Path,
        agent_name: str,
        text: str,
        *,
        delivery_mode: str = "steer",
        timeout_s: float = 10.0,
        message_id: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_url = _serve_url(state_dir)
        session_id = _resolve_session(state_dir, base_url, agent_name, timeout_s)
        engine = (options or {}).get("engine") or {}
        provider_id = f"sac-{engine.get('key', '')}"
        model_id = str(engine.get("model", ""))
        if not engine.get("key") or not model_id:
            raise GatewayHarnessError(
                "Opencode submit needs options['engine'] {key, model}; "
                "pass parse_agent_options(config) through."
            )
        key = message_key(agent_name, message_id)
        states = self.session_states(state_dir, timeout_s=timeout_s)
        busy = states.get(session_id) == "busy"
        if delivery_mode == "queue" or (delivery_mode == "steer" and busy):
            _status_code, _ignored = _request(
                "POST",
                f"{base_url}/session/{session_id}/prompt_async",
                {
                    "messageID": key,
                    "model": {"providerID": provider_id, "modelID": model_id},
                    "parts": [{"type": "text", "text": text}],
                },
                timeout_s,
            )
            return {
                "status": "accepted",
                "session_id": session_id,
                "message_id": key,
            }
        return post_session_message(
            base_url,
            session_id,
            provider_id=provider_id,
            model_id=model_id,
            text=text,
            message_id=key,
            timeout_s=timeout_s,
        )

    def abort_turn(
        self, state_dir: Path, agent_name: str, *, timeout_s: float = 10.0
    ) -> bool:
        try:
            base_url = _serve_url(state_dir)
        except GatewayHarnessError:
            return False
        session_id = _mapped_session_id(state_dir, agent_name)
        if not session_id:
            return False  # Never submitted: nothing in flight to cancel.
        _status_code, payload = _request(
            "POST", f"{base_url}/session/{session_id}/abort", None, timeout_s
        )
        return bool(payload)

    def parse_agent_options(self, config: Any) -> dict[str, Any]:
        return {
            "session": str(getattr(getattr(config, "claude", None), "session", "")),
            "approval_policy": str(
                getattr(config, "opencode_approval_policy", "never")
            ),
            "run_budget_seconds": getattr(config, "opencode_run_budget_seconds", None),
            "serve_port": getattr(config, "opencode_serve_port", None),
            "engine": {
                "key": str(getattr(config, "engine_key", "") or ""),
                "model": str(getattr(config, "model", "") or ""),
            },
        }

    def compile_profile(
        self, plan: Any, *, workdir: str, options: dict[str, Any]
    ) -> dict[str, Any]:
        del workdir  # Serve inherits the container --pwd; the profile is path-free.
        if getattr(plan, "harness", "") != "opencode":
            raise ValueError(f"Opencode compiler received harness {plan.harness!r}")
        protocol = plan.endpoint.protocol
        # The provider npm package follows the Go endpoint table
        # (https://opencode.ai/docs/go/): chat-completions endpoints go
        # through the generic OpenAI-compatible SDK, responses endpoints
        # through the Responses-capable OpenAI SDK (e.g. Muse Spark on
        # Go, which answers ModelProtocolUnsupported on chat-completions
        # — measured 2026-09-28).
        npm_package = {
            "openai-chat-completions": "@ai-sdk/openai-compatible",
            "openai-responses": "@ai-sdk/openai",
        }.get(protocol)
        if npm_package is None:
            raise ValueError(
                f"unsupported opencode endpoint protocol {protocol!r}; "
                "use an openai-chat-completions or openai-responses engine"
            )
        auth_env = plan.endpoint.auth_env if plan.endpoint.auth_kind != "none" else ""
        if not auth_env:
            raise ValueError("Opencode profile needs a keyed engine endpoint")
        provider_id = f"sac-{plan.engine.key}"
        model_id = plan.engine.model_id
        model_entry: dict[str, Any] = {"name": model_id}
        # Reasoning effort rides the engine declaration into the
        # provider-local model options (opencode variant knob). Empty
        # means the provider default; the Go relay accepts
        # none/minimal/low/medium/high/xhigh/max.
        effort = str(getattr(plan.engine, "reasoning_effort", "") or "").strip()
        if effort:
            model_entry["options"] = {"reasoningEffort": effort}
        provider: dict[str, Any] = {
            "npm": npm_package,
            "name": f"SAC {plan.engine.key}",
            "options": {
                "baseURL": _strip_api_suffix(plan.endpoint.url, protocol),
                "apiKey": "{env:" + auth_env + "}",
            },
            "models": {model_id: model_entry},
        }
        approval = options.get("approval_policy", "never")
        base_permission = "allow" if approval == "never" else "ask"
        # spec.lineage.may_spawn is the neutral spawn envelope. Opencode
        # has no delegation width/depth knobs (documented limitation —
        # opencode schedules subagents itself), but the task tool that
        # launches subagents IS permission-gated, so may_spawn: false
        # removes it instead of merely discouraging it in prose.
        if getattr(plan, "may_spawn", True):
            permission: Any = base_permission
        else:
            permission = {"*": base_permission, "task": "deny"}
        return {
            "permission": permission,
            "provider": {provider_id: provider},
            "model": f"{provider_id}/{model_id}",
        }


OPENCODE_GATEWAY = _OpencodeGatewayHarness()
