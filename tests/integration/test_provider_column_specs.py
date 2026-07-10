"""Two-column provider integration: one Claude spec, one OpenAI spec.

openai-compat-3's card asks for integration tests holding a
Claude-column spec and an OpenAI-column spec side by side and asserting
both columns reduce to the SAME provider-agnostic surface. Live API
turns are out (no keys in CI), so the parity is pinned at the three
seams that make the columns interchangeable, each on REAL objects:

1. **Spec → config**: two real YAML specs differing ONLY in
   ``spec.provider`` load through the one ``load_config`` path.
2. **Config → entrypoint env** (``build_run_argv`` — the full apptainer
   argv): the OpenAI column carries the OPENAI_* injection and ZERO
   Anthropic wiring; the Claude column carries its unchanged Anthropic
   wiring and ZERO OPENAI_*.
3. **Session-type routing + NormalizedEvent shape**: both columns'
   ``spec.a2a.handler`` keys route to registered executors sharing
   ``BaseSyncExecutor`` (one task-event surface), and the OpenAI
   column's concrete session satisfies the ``ProviderSession`` Protocol
   — the contract whose ``send`` streams :class:`NormalizedEvent`s for
   BOTH SDK families (the Claude column's ProviderSession retrofit is
   explicitly future work per ``_provider_session``'s module docstring,
   so the Protocol itself is the shared shape both columns meet today).

Real seams only (no mocks). STX-TQ002 AAA + STX-TQ007 one-assert.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container.a2a.executors import EXECUTORS, BaseSyncExecutor
from scitex_agent_container._runners._provider_session import ProviderSession
from scitex_agent_container._runners.openai_session import OpenAISession
from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._apptainer_build_argv import build_run_argv

_SPEC_TEMPLATE = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: t
    sac-builtin: "off"
spec:
{provider_line}  runtime: claude-agent-sdk
  host: local
  workdir: /tmp/column-wd
  apptainer:
    image: /x.sif
    binds: []
  health:
    enabled: true
    interval: 60
  restart:
    policy: on-failure
    max_retries: 3
  claude:
    model: sonnet
"""


@pytest.fixture
def _sandbox_env(tmp_path: Path) -> Iterator[Path]:
    """Redirect ``$HOME`` + pin the OpenAI-column env for determinism.

    ``build_run_argv`` on the OpenAI column resolves the API key through
    ``$HOME/.env`` + host env (fail-loud when absent), and the family
    axis honours the ``SAC_PROVIDER`` ops override — both must be pinned
    for a reproducible argv. Everything restored on teardown.
    """
    keys = ("HOME", "SAC_PROVIDER", "SAC_OPENAI_API_KEY", "OPENAI_API_KEY")
    saved = {k: os.environ.get(k) for k in keys}
    home = tmp_path / "home"
    home.mkdir()
    os.environ["HOME"] = str(home)
    os.environ.pop("SAC_PROVIDER", None)
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ["SAC_OPENAI_API_KEY"] = "sk-column-test"
    try:
        yield tmp_path
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _load_column(tmp_path: Path, name: str, provider_line: str):
    spec_dir = tmp_path / "agents" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / "spec.yaml"
    spec.write_text(
        _SPEC_TEMPLATE.format(provider_line=provider_line), encoding="utf-8"
    )
    return load_config(str(spec))


def _claude_column(tmp_path: Path):
    return _load_column(tmp_path, "claude-col", "")


def _openai_column(tmp_path: Path):
    return _load_column(tmp_path, "openai-col", "  provider: openai\n")


def _argv_env(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, a in enumerate(argv):
        if a == "--env" and i + 1 < len(argv) and "=" in argv[i + 1]:
            k, _, v = argv[i + 1].partition("=")
            out[k] = v
    return out


def _column_argv(config, tmp_path: Path) -> list[str]:
    return build_run_argv(
        config,
        state_dir=tmp_path / "state" / config.name,
        sif_path=tmp_path / "x.sif",
    )


# ---------------------------------------------------------------------------
# 1. Spec → config: one loader, two columns
# ---------------------------------------------------------------------------


def test_claude_column_spec_loads_as_anthropic_family(
    _sandbox_env: Path,
) -> None:
    # Arrange
    cfg = _claude_column(_sandbox_env)
    # Act
    family = cfg.provider
    # Assert
    assert family == "anthropic"


def test_openai_column_spec_loads_as_openai_family(_sandbox_env: Path) -> None:
    # Arrange
    cfg = _openai_column(_sandbox_env)
    # Act
    family = cfg.provider
    # Assert
    assert family == "openai"


# ---------------------------------------------------------------------------
# 2. Config → entrypoint env: the full apptainer argv per column
# ---------------------------------------------------------------------------


def test_openai_column_argv_injects_openai_key(_sandbox_env: Path) -> None:
    # Arrange
    cfg = _openai_column(_sandbox_env)
    # Act
    env = _argv_env(_column_argv(cfg, _sandbox_env))
    # Assert
    assert env["OPENAI_API_KEY"] == "sk-column-test"


def test_openai_column_argv_marks_family_in_container(_sandbox_env: Path) -> None:
    # Arrange
    cfg = _openai_column(_sandbox_env)
    # Act
    env = _argv_env(_column_argv(cfg, _sandbox_env))
    # Assert
    assert env["SAC_PROVIDER"] == "openai"


def test_openai_column_argv_carries_no_anthropic_wiring(
    _sandbox_env: Path,
) -> None:
    # Arrange
    cfg = _openai_column(_sandbox_env)
    # Act
    env = _argv_env(_column_argv(cfg, _sandbox_env))
    # Assert
    assert not any(k.startswith(("ANTHROPIC", "CLAUDE_CONFIG")) for k in env)


def test_claude_column_argv_carries_no_openai_wiring(_sandbox_env: Path) -> None:
    # Arrange — the host DOES hold an OpenAI key (fixture); the Claude
    # column must still not receive it.
    cfg = _claude_column(_sandbox_env)
    # Act
    env = _argv_env(_column_argv(cfg, _sandbox_env))
    # Assert
    assert not any("OPENAI" in k for k in env)


def test_claude_column_argv_binds_oauth_credentials(_sandbox_env: Path) -> None:
    # Arrange — a real host creds file: the Claude column's OAuth
    # dir-bind must be UNCHANGED by the openai-compat-3 branch.
    creds = _sandbox_env / "home" / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text("{}")
    cfg = _claude_column(_sandbox_env)
    # Act
    argv = _column_argv(cfg, _sandbox_env)
    # Assert
    assert any(a == f"{creds.parent}:/tmp/sac-claude:ro" for a in argv)


# ---------------------------------------------------------------------------
# 3. Session-type routing + the shared NormalizedEvent-producing shape
# ---------------------------------------------------------------------------


def test_both_columns_route_to_registered_executors(_sandbox_env: Path) -> None:
    # Arrange — the two columns' a2a handler keys.
    keys = ("claude_session", "openai_session")
    # Act
    routed = all(k in EXECUTORS for k in keys)
    # Assert
    assert routed is True


def test_both_column_executors_share_the_task_event_surface(
    _sandbox_env: Path,
) -> None:
    # Arrange — one BaseSyncExecutor surface = one task-event shape for
    # every column (the serve CLI drives them identically).
    executors = (EXECUTORS["claude_session"], EXECUTORS["openai_session"])
    # Act
    shared = all(issubclass(e, BaseSyncExecutor) for e in executors)
    # Assert
    assert shared is True


def test_openai_column_session_satisfies_provider_session_protocol(
    _sandbox_env: Path,
) -> None:
    # Arrange — ProviderSession is the shape whose ``send`` streams
    # NormalizedEvents; construction needs no openai-agents install.
    session = OpenAISession("openai-col")
    # Act
    conforms = isinstance(session, ProviderSession)
    # Assert
    assert conforms is True


def test_openai_column_session_streams_normalized_events_shape() -> None:
    # Arrange — real-SDK tier: with openai-agents installed, a real
    # stream event normalizes into the shared NormalizedEvent vocabulary
    # (the same dataclass the future Claude-side ProviderSession must
    # yield). Uses the SDK's own event classes — no network. Construction
    # mirrors test_openai_session.py's real-SDK tier exactly.
    pytest.importorskip("agents")
    from agents.stream_events import RawResponsesStreamEvent
    from openai.types.responses import ResponseTextDeltaEvent

    from scitex_agent_container._runners._provider_session import NormalizedEvent
    from scitex_agent_container._runners.openai_session import (
        normalize_stream_event,
    )

    event = RawResponsesStreamEvent(
        data=ResponseTextDeltaEvent(
            content_index=0,
            delta="hi",
            item_id="i",
            logprobs=[],
            output_index=0,
            sequence_number=0,
            type="response.output_text.delta",
        )
    )
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert isinstance(normalized, NormalizedEvent)
