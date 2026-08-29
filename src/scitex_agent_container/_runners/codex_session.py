"""Concrete :class:`HarnessSession` backed by the ``openai-codex`` SDK.

Card ``sac-codex-python-sdk-harness-20260814`` — sac's FOURTH harness,
and the first added since the v4 descriptor registry landed
(:mod:`config._harness_registry`).

WHAT THE SDK ACTUALLY IS (measured, not assumed)
-------------------------------------------------
``openai-codex`` (PyPI, 0.144.4, Apache-2.0) is NOT an in-process API
client. Its ``CodexClient`` runs::

    subprocess.Popen([codex_bin, "app-server", "--listen", "stdio://"],
                     stdin=PIPE, stdout=PIPE, ...)

and speaks JSON-RPC over that pipe (``sdk/python/src/openai_codex/
client.py``). The ``codex`` binary itself arrives as the pinned
``openai-codex-cli-bin`` wheel dependency, so ``pip install
openai-codex`` really does put a 285 MB native binary in site-packages;
nothing has to be fetched at run time.

That subprocess does NOT make this harness ``hosted="external"``. The
registry's ``hosted`` axis asks who owns the SAC-VISIBLE loop, and the
answer here is the same as for ``claude-agent-sdk`` (which also spawns
the ``claude`` binary): OUR runner is the container's inner process and
the vendor process is its child. See the descriptor's inline comment.

WHY THE TOOLS ARE THE POINT
----------------------------
The ``openai-agents`` harness is conversational until sac supplies every
tool (:class:`~.openai_session.OpenAIAgentsSession`'s ``mcp_servers``
plumbing exists for exactly that). Codex is the opposite: file edit,
shell exec, ``apply_patch`` and its own sandbox ship INSIDE the binary,
so a model reachable through it is an agent without sac wiring a single
tool. That is the whole reason this harness is worth a row.

THE OPTIONAL DEPENDENCY IS OPTIONAL FOR REAL
---------------------------------------------
``openai-codex`` is an extra (``pip install
scitex-agent-container[codex-sdk]``). This module imports it LAZILY
inside :meth:`CodexSession.start` — importing the module and
CONSTRUCTING a :class:`CodexSession` works on a Claude-only deployment;
only OPENING a session needs the SDK, and a missing one raises
:class:`CodexSessionError` carrying the pip hint rather than a bare
``ImportError`` from an unrelated line. :func:`normalize_thread_item` is
duck-typed on the SDK's own ``type`` discriminator strings, so event
normalization is pure and testable with no SDK and no network.

Vocabulary mapping (codex thread items → NormalizedEvent.kind)
---------------------------------------------------------------
``agent_message`` → ``text_delta``; ``reasoning`` → ``reasoning``;
``command_execution`` / ``file_change`` / ``mcp_tool_call`` /
``web_search`` → ``tool_call`` (with the item's own payload as
``tool_input``); ``todo_list`` → ``task``; ``error`` → ``error``. The
terminal ``kind="result"`` event is synthesized from the ``TurnResult``
once the turn completes, carrying ``final_response``, the thread id as
``session_id`` (that is the value ``--resume-session-id`` takes) and
``usage``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Mapping, Sequence

from ._harness_session import Message, NormalizedEvent, RunResult

logger = logging.getLogger(__name__)

__all__ = [
    "CodexSession",
    "CodexSessionError",
    "normalize_thread_item",
    "usage_as_dict",
    "_parse_argv",
    "main",
]

_INSTALL_HINT = (
    "codex_session requires `openai-codex` "
    "(`pip install scitex-agent-container[codex-sdk]`). NOTE the extra is "
    "`codex-sdk`, NOT the pre-existing `codex` extra — that one installs "
    "scitex-genai for `spec.claude.provider: codex`, a different axis."
)

#: Thread-item ``type`` values that are TOOL activity. Mapped to
#: ``kind="tool_call"`` with the item itself as the payload, so a
#: consumer sees WHAT the agent did without this module having to model
#: each vendor payload shape (they are recorded verbatim in ``raw``).
_TOOL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "web_search",
    }
)

#: Thread-item ``type`` values that are lifecycle/progress notifications.
_TASK_ITEM_TYPES = frozenset({"todo_list"})


class CodexSessionError(RuntimeError):
    """Raised when the Codex session cannot satisfy a precondition."""


def _import_codex() -> Any:
    """Import and return the ``openai_codex`` module, or raise the pip hint."""
    try:
        import openai_codex
    except Exception as exc:  # stx-allow: fallback (reason: optional dep at runtime; broaden beyond ImportError so a misbuilt binary wheel surfaces as an actionable CodexSessionError rather than an opaque OSError from the bundled cli-bin package)
        raise CodexSessionError(_INSTALL_HINT) from exc
    return openai_codex


# ---------------------------------------------------------------------------
# Thread-item normalization (pure; duck-typed on the SDK's discriminator)
# ---------------------------------------------------------------------------


def _item_text(item: Any) -> str:
    """Best-effort display text for a thread item."""
    for attr in ("text", "content", "message", "command", "summary"):
        value = getattr(item, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _item_payload(item: Any) -> dict[str, Any]:
    """The item's fields as a plain dict (pydantic model or mapping)."""
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        # stx-allow: fallback (reason: a future item type may hold a value pydantic cannot dump in python mode; the event stays usable and `raw` keeps the original)
        try:
            dumped = dump(mode="python")
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            dumped = None
        if isinstance(dumped, dict):
            return dumped
    if isinstance(item, Mapping):
        return dict(item)
    return {}


def normalize_thread_item(item: Any) -> NormalizedEvent | None:
    """Map ONE codex thread item to a :class:`NormalizedEvent`.

    Returns ``None`` for items with no harness-agnostic meaning (unknown
    future types), which callers drop. Pure and duck-typed on the SDK's
    own ``type`` discriminator string, so it needs neither the SDK nor a
    network connection — hand-built fixture objects with a ``type``
    attribute exercise every branch. See the module docstring for the
    full vocabulary mapping.
    """
    itype = str(getattr(item, "type", "") or "")

    if itype == "agent_message":
        return NormalizedEvent(kind="text_delta", text=_item_text(item), raw=item)
    if itype == "reasoning":
        return NormalizedEvent(kind="reasoning", text=_item_text(item), raw=item)
    if itype in _TOOL_ITEM_TYPES:
        return NormalizedEvent(
            kind="tool_call",
            tool_name=itype,
            tool_input=_item_payload(item),
            raw=item,
        )
    if itype in _TASK_ITEM_TYPES:
        return NormalizedEvent(kind="task", text=itype, raw=item)
    if itype == "error":
        return NormalizedEvent(kind="error", error=_item_text(item), raw=item)
    return None


def usage_as_dict(usage: Any) -> dict[str, Any]:
    """Flatten the SDK's usage object into the RunResult usage dict.

    Duck-typed and ``None``-tolerant so a missing/partial usage object
    degrades to an empty dict rather than raising mid-terminal-event.
    """
    if usage is None:
        return {}
    if isinstance(usage, Mapping):
        source: Mapping[str, Any] = usage
        return {k: v for k, v in source.items() if isinstance(v, int)}
    out: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------


class CodexSession:
    """:class:`HarnessSession` implementation over ``openai-codex``.

    Lifecycle mirrors the Protocol: one :meth:`start` (spawn the codex
    app-server, open or RESUME a thread), N :meth:`send` turns (each one
    ``thread.run``, streaming :class:`NormalizedEvent`, terminating in
    ``kind="result"``), then :meth:`close`.

    Args:
        agent_name: sac agent identity — used for logging only; codex
            keeps its own thread state under ``$CODEX_HOME``.
        model: Model id passed to codex. ``None`` → ``SAC_CODEX_MODEL``
            env → codex's own default.
        model_provider: ``[model_providers.<id>]`` key from
            ``config.toml``. This is how a sac agent reaches a
            self-hosted OpenAI-compatible endpoint; ``None`` →
            ``SAC_CODEX_MODEL_PROVIDER`` env → codex's default (the
            OpenAI-hosted provider).
        instructions: Extra developer instructions for the thread
            (``None`` keeps codex's own).
        cwd: Working directory codex operates in. ``None`` → the
            process's cwd.
        sandbox: Codex sandbox mode (``read-only`` /
            ``workspace-write`` / ``full-access``). ``None`` →
            ``SAC_CODEX_SANDBOX`` env → codex's default.

            MEASURED CAVEAT (2026-08-14, scitex-compute-04): codex
            sandboxes with bubblewrap, and bwrap inside an apptainer
            container fails with ``Can't bind mount /oldroot/ on
            /newroot/`` — every tool call then exits 1 while the model
            reports success. An agent running INSIDE a sac container is
            already sandboxed by that container, so ``full-access`` is
            the correct value there; the default is left to codex so a
            bare-host run keeps its protection.
        config_overrides: ``key=value`` strings forwarded as codex
            ``--config`` flags (``CodexConfig.config_overrides``). The
            escape hatch for anything not modelled above.
        thread_id: Resume a PRIOR thread instead of starting one. This
            is what ``--resume-session-id`` threads through, and it is
            real: the descriptor declares ``can_resume=True`` on the
            strength of ``AsyncCodex.thread_resume``.
        codex_bin: Explicit path to the ``codex`` executable. ``None``
            uses the bundled ``openai-codex-cli-bin`` binary.
    """

    def __init__(
        self,
        agent_name: str,
        *,
        model: str | None = None,
        model_provider: str | None = None,
        instructions: str | None = None,
        cwd: str | None = None,
        sandbox: str | None = None,
        config_overrides: Sequence[str] = (),
        thread_id: str | None = None,
        codex_bin: str | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.model = model
        self.model_provider = model_provider
        self.instructions = instructions
        self.cwd = cwd
        self.sandbox = sandbox
        self.config_overrides = tuple(config_overrides)
        self.thread_id = thread_id
        self.codex_bin = codex_bin
        self._codex: Any = None
        self._thread: Any = None
        self._started = False

    # -- HarnessSession surface ----------------------------------------

    async def start(self) -> None:
        """Open the session: spawn the app-server, start or resume a thread.

        Raises :class:`CodexSessionError` if ``openai-codex`` is absent
        (with the pip hint) or if the SDK refuses to open — a codex that
        cannot authenticate must fail HERE, loudly, rather than at the
        first turn where it would read as a model error.
        """
        from ._codex_options import build_codex_config, resolve_thread_options

        codex_mod = _import_codex()
        config = build_codex_config(
            codex_mod,
            codex_bin=self.codex_bin,
            cwd=self.cwd,
            config_overrides=self.config_overrides,
        )
        try:
            self._codex = codex_mod.AsyncCodex(config)
            await self._codex.__aenter__()
            options = resolve_thread_options(
                codex_mod,
                model=self.model,
                model_provider=self.model_provider,
                instructions=self.instructions,
                cwd=self.cwd,
                sandbox=self.sandbox,
            )
            if self.thread_id:
                self._thread = await self._codex.thread_resume(
                    self.thread_id, **options
                )
            else:
                self._thread = await self._codex.thread_start(**options)
        except CodexSessionError:
            raise
        except Exception as exc:  # stx-allow: fallback (reason: the SDK surface here is a subprocess spawn + JSON-RPC handshake + auth; every failure shape must reach the caller as one actionable CodexSessionError, never a bare OSError/JsonRpcError from an unrelated frame)
            raise CodexSessionError(
                f"codex session failed to open for {self.agent_name!r}: {exc}"
            ) from exc
        self.thread_id = getattr(self._thread, "id", None) or self.thread_id
        self._started = True

    async def send(self, message: Message) -> AsyncIterator[NormalizedEvent]:
        """Run one turn via ``thread.run`` and yield normalized events.

        The last event of a completed turn is ``kind="result"`` carrying
        the :class:`RunResult`; a failing turn yields ``kind="error"``
        instead (per the Protocol docstring both are turn-ending).
        """
        if not self._started:
            raise CodexSessionError("CodexSession.send() called before start().")

        try:
            result = await self._thread.run(message.content)
        except asyncio.CancelledError:  # cooperative cancellation stays loud
            raise
        except Exception as exc:  # stx-allow: fallback (reason: SDK/subprocess/network surface is broad; the Protocol contract is a turn-ending kind="error" event, not an exception mid-iteration)
            yield NormalizedEvent(kind="error", error=str(exc), raw=exc)
            return

        for item in getattr(result, "items", None) or ():
            normalized = normalize_thread_item(item)
            if normalized is not None:
                yield normalized

        error = getattr(result, "error", None)
        if error is not None:
            yield NormalizedEvent(kind="error", error=str(error), raw=result)
            return

        yield NormalizedEvent(
            kind="result",
            result=RunResult(
                text=str(getattr(result, "final_response", "") or ""),
                # The thread id IS sac's resume handle — recording it on
                # every turn is what makes can_resume=True honest.
                session_id=getattr(self._thread, "id", None) or self.thread_id,
                usage=usage_as_dict(getattr(result, "usage", None)),
                stop_reason=str(getattr(result, "status", "") or ""),
            ),
            raw=result,
        )

    async def close(self) -> None:
        """Tear down the session: close the app-server subprocess.

        An un-closed :class:`CodexSession` leaks a native ``codex
        app-server`` process, so the handle is dropped even when the
        close itself raises — the error is re-raised after the state is
        cleared so a failure stays visible without stranding the next
        start.
        """
        codex, self._codex = self._codex, None
        self._thread = None
        self._started = False
        if codex is None:
            return
        await codex.__aexit__(None, None, None)


# CLI entry — it mirrors the claude/openai session parsers so the runtime
# adapter's fixed argv lands without ``ArgumentError`` on extra flags, and it
# lives in _codex_session_cli because this module would otherwise cross the
# 512-line cap. Re-exported so `python -m
# scitex_agent_container._runners.codex_session` — the module name the
# apptainer argv builder emits — still resolves.
from ._codex_session_cli import _parse_argv, main  # noqa: E402,F401 (re-export)

if __name__ == "__main__":  # pragma: no cover — exercised by the adapter
    raise SystemExit(main())
