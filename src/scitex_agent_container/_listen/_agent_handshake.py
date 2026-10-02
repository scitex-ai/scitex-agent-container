"""Fresh server challenges and source-observed native agentic handshakes."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict

from scitex_dev.status import StatusCode
from starlette.responses import JSONResponse

from .._lifecycle._relocate_handshake import HandshakeFacts, evaluate_handshake
from .._state._agentic_handshake import (
    DAEMON,
    advance_handshake,
    challenge_prompt,
    expected_answer,
    make_contract,
    open_handshake,
    read_handshake,
    record_acceptance,
)
from ..runtimes._codex_activity import CodexActivityError, read_codex_activity
from ..runtimes._codex_activity_binding import (
    assert_codex_binding_current,
    bind_codex_runtime,
)
from ..runtimes._codex_activity_projection import _launch_identity
from ..runtimes._codex_handshake_proof import read_codex_tool_proof


def _host(request):
    from .._state.state_store import _resolve_host

    return getattr(request.app.state, "local_host", None) or _resolve_host(None)


def _capture_target(name: str, host: str):
    """Resolve only canonical state/birth and the exact owned primary CLI FD."""
    from .._runners._session_state import read_instance_id, state_dir_for
    from .._state.state_store_incarnations import get_incarnation
    from .._state.state_store_instances import read_instance

    state_dir = state_dir_for(name)
    instance_id = read_instance_id(state_dir)
    if not instance_id:
        raise CodexActivityError("canonical target instance is unavailable")
    record = read_instance(instance_id)
    _launch_identity(
        get_incarnation(instance_id),
        instance_id=instance_id,
        agent_name=name,
        host=host,
    )
    binding = bind_codex_runtime(
        record or {}, instance_id=instance_id, agent_name=name, host=host
    )
    observed = read_codex_activity(
        binding.rollout_path,
        expected_thread_id=binding.thread_id,
        observed_at=time.time(),
        expected_file_identity=binding.rollout_identity,
    )
    if read_instance_id(state_dir) != instance_id:
        raise CodexActivityError("canonical target changed during capture")
    assert_codex_binding_current(binding, read_instance(instance_id) or {})
    return (
        {
            "agent": name,
            "host": host,
            "instance_id": instance_id,
            "boot_id": binding.boot_id,
            "session_id": binding.thread_id,
        },
        observed.event_seq,
        binding,
    )


def _observe_proof(contract):
    target = contract["target"]
    current, cursor, binding = _capture_target(target["agent"], target["host"])
    if (
        current != target
        or list(binding.rollout_identity) != contract["source_identity"]
    ):
        raise CodexActivityError("challenge runtime or session source changed")
    if cursor < contract["cursor"]:
        raise CodexActivityError("challenge runtime activity regressed")
    proof = read_codex_tool_proof(
        binding,
        observed_at=time.time(),
        cursor=contract["cursor"],
        issued_at=contract["issued_at"],
        answer=expected_answer(contract),
    )
    # A response and its process/instance provenance must survive the read.
    again, _, rebound = _capture_target(target["agent"], target["host"])
    if again != target or rebound.rollout_identity != binding.rollout_identity:
        raise CodexActivityError("challenge owner changed during proof observation")
    return (
        proof if proof is None or proof.completed_at <= contract["deadline"] else None
    )


def _response(exchange_id, contract, values):
    state = json.loads(values["message"])
    return {
        "exchange_id": exchange_id,
        "dispatch_id": contract["nonce"],
        "target": contract["target"],
        "deadline": contract["deadline"],
        "status_code": {
            "kind": values["kind"],
            "code": values["code"],
            "message": values["message"],
            "final": values["final"],
        },
        "proven": state.get("proven"),
        "phase": state.get("phase"),
        "proof": state.get("tool_proof"),
    }


async def agent_handshake_start(request):
    """POST: request one server-issued challenge; no client-selected identity."""
    name = request.path_params["name"]
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"timeout_s"}:
            raise ValueError(
                "only timeout_s may be supplied; server selects all identities"
            )
        target, cursor, binding = await asyncio.to_thread(
            _capture_target, name, _host(request)
        )
        contract = make_contract(
            target,
            cursor=cursor,
            now=time.time(),
            timeout_s=body.get("timeout_s", 120.0),
        )
        contract["source_identity"] = list(binding.rollout_identity)
        exchange_id = await asyncio.to_thread(open_handshake, contract)
        prompt = challenge_prompt(contract, exchange_id)
        from .._state.dispatch_ledger import record_dispatch
        from .._state.state_store_channel import persist_event
        from ..a2a._inbox_bus import mint_event

        await asyncio.to_thread(
            record_dispatch,
            agent=DAEMON,
            from_agent=DAEMON,
            to_agent=name,
            text=prompt,
            conversation_id=exchange_id,
            dispatch_id=contract["nonce"],
            ts=contract["issued_at"],
        )
        event = mint_event(
            name,
            prompt,
            from_agent=DAEMON,
            conversation_id=exchange_id,
            dispatch_id=contract["nonce"],
            requires_reply=True,
            kind="agentic_challenge",
            extra={
                "handshake": {
                    "exchange_id": exchange_id,
                    "target": target,
                    "nonce": contract["nonce"],
                }
            },
        )
        event["_row_id"] = await asyncio.to_thread(
            persist_event, target=name, event=event
        )
        subscribers = await request.app.state.inbox.publish(name, event)
        await asyncio.to_thread(record_acceptance, exchange_id, accepted=True)
        values, contract = await asyncio.to_thread(
            read_handshake, exchange_id, agent=name
        )
        result = _response(exchange_id, contract, values)
        result["transport"] = {
            "persisted": True,
            "subscriber_count": subscribers,
            "agentic_proof": False,
        }
        return JSONResponse(result, status_code=202)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, CodexActivityError):
            return JSONResponse(
                {
                    "proven": None,
                    "phase": "unknown",
                    "reason": "No fenced native target is observable.",
                },
                status_code=503,
            )
        return JSONResponse({"error": str(exc)}, status_code=400)


async def agent_handshake_ack(request):
    """POST: observe model-authored ACK for one already issued server exchange."""
    name = request.path_params["name"]
    exchange_id = request.path_params["exchange_id"]
    try:
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("feedback"), dict):
            raise ValueError("feedback must be an object")
        values, contract = await asyncio.to_thread(
            read_handshake, exchange_id, agent=name
        )
        feedback = body["feedback"]
        target, _, binding = await asyncio.to_thread(
            _capture_target, name, _host(request)
        )
        if (
            target != contract["target"]
            or list(binding.rollout_identity) != contract["source_identity"]
        ):
            raise CodexActivityError("challenge owner or session source changed")
        proof = feedback.get("handshake_proof")
        if not isinstance(proof, dict) or proof.get("exchange_id") != exchange_id:
            raise ValueError("proof must name this exact issued exchange")
        from .._mcp._channel_agentic_feedback import absorb_agentic_feedback

        saved = await asyncio.to_thread(
            absorb_agentic_feedback,
            {
                "kind": "agentic_ack",
                "from_agent": name,
                "extra": feedback,
            },
            agent=DAEMON,
        )
        if not saved:
            return JSONResponse(
                {"proven": False, "phase": "ack_refused"}, status_code=422
            )
        return JSONResponse(
            {
                "exchange_id": exchange_id,
                "proven": None,
                "phase": "awaiting_native_tool_evidence",
                "poll": f"/agents/{name}/handshakes/{exchange_id}",
            },
            status_code=202,
        )
    except CodexActivityError:
        return JSONResponse(
            {
                "proven": None,
                "phase": "unknown",
                "reason": "The issued native target is not observable.",
            },
            status_code=503,
        )
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except (LookupError, PermissionError):
        return JSONResponse({"error": "No matching issued handshake."}, status_code=404)


async def agent_handshake_status(request):
    """GET: source-observe proof; acceptance/ACK alone can never finalize 200."""
    name = request.path_params["name"]
    exchange_id = request.path_params["exchange_id"]
    try:
        values, contract = await asyncio.to_thread(
            read_handshake, exchange_id, agent=name
        )
        if values["final"]:
            if values["code"] == 200:
                if time.time() > contract["deadline"]:
                    return JSONResponse(
                        {
                            "exchange_id": exchange_id,
                            "proven": None,
                            "phase": "expired",
                            "reason": "The issued handshake lease has expired.",
                        },
                        status_code=410,
                    )
                target, _, binding = await asyncio.to_thread(
                    _capture_target, name, _host(request)
                )
                if (
                    target != contract["target"]
                    or list(binding.rollout_identity) != contract["source_identity"]
                ):
                    raise CodexActivityError(
                        "completed handshake no longer has its issued target"
                    )
            return JSONResponse(_response(exchange_id, contract, values))
        state = json.loads(values["message"])
        proof = None
        if state.get("accepted") is not True and time.time() < contract["deadline"]:
            return JSONResponse(
                _response(exchange_id, contract, values), status_code=202
            )
        if state.get("candidate") is not None:
            proof = await asyncio.to_thread(_observe_proof, contract)
        if proof is None and time.time() < contract["deadline"]:
            return JSONResponse(
                _response(exchange_id, contract, values), status_code=202
            )
        facts = HandshakeFacts(
            challenge_accepted=state.get("accepted"),
            reply_observed=state.get("candidate") is not None,
            observed_by=f"sac.listen/{contract['target']['host']}",
            reply_nonce=(state.get("candidate") or {}).get("nonce"),
            reply_answer=expected_answer(contract) if proof is not None else "",
        )
        verdict = evaluate_handshake(
            facts, nonce=contract["nonce"], expected_answer=expected_answer(contract)
        )
        message = json.dumps(
            {
                "phase": "proven" if verdict.proven is True else "not_proven",
                "proven": verdict.proven,
                "reason": verdict.reason,
                "candidate": state.get("candidate"),
                "tool_proof": asdict(proof) if proof is not None else None,
                "completion_semantics": "native lifecycle completion; no nested tool success inference",
            },
            sort_keys=True,
        )
        await asyncio.to_thread(
            advance_handshake,
            exchange_id,
            StatusCode(kind="http", code=verdict.code, message=message),
        )
        values, contract = await asyncio.to_thread(
            read_handshake, exchange_id, agent=name
        )
        return JSONResponse(_response(exchange_id, contract, values))
    except CodexActivityError:
        return JSONResponse(
            {
                "proven": None,
                "phase": "unknown",
                "reason": "The exact native owner or source is not observable.",
            },
            status_code=503,
        )
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid handshake exchange."}, status_code=400)
    except (LookupError, PermissionError):
        return JSONResponse({"error": "No matching issued handshake."}, status_code=404)


def handshake_routes(prefix):
    """Add the bounded handshake surface to the existing authenticated app."""
    from starlette.routing import Route

    return [
        Route(f"{prefix}/{{name}}/handshakes", agent_handshake_start, methods=["POST"]),
        Route(
            f"{prefix}/{{name}}/handshakes/{{exchange_id}}",
            agent_handshake_status,
            methods=["GET"],
        ),
        Route(
            f"{prefix}/{{name}}/handshakes/{{exchange_id}}/ack",
            agent_handshake_ack,
            methods=["POST"],
        ),
    ]
