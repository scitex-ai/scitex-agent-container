"""Bridge fixture definitions without import-time socket reservation."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Iterator

import pytest

from scitex_agent_container.runtimes import _tui_turn_bridge as bridge


@pytest.fixture
def bridge_factory() -> Iterator[Callable[..., int]]:
    """Start real bridge servers on ephemeral ports; tear them all down."""
    servers = []
    threads = []
    exchanges: dict[str, dict] = {}
    sequence = {"value": 0}

    def exchange_open(
        *,
        agent: str,
        probe_url: str,
        exchange_id: str | None = None,
        delivery_id: str | None = None,
        initiator: str | None = None,
    ) -> tuple[str, str]:
        existing_id = next(
            (
                key
                for key, value in exchanges.items()
                if delivery_id and value.get("delivery_id") == delivery_id
            ),
            None,
        )
        if exchange_id is None and existing_id is None:
            sequence["value"] += 1
        exchange_id = (
            exchange_id
            or existing_id
            or (f"xch_20260912T000000Z_test-{sequence['value']}_abcdef")
        )
        opened_at = "2026-09-12T00:00:00+00:00"
        exchanges.setdefault(
            exchange_id,
            {
                "kind": "http",
                "code": 202,
                "message": f"accepted; poll `{probe_url}/{exchange_id}`",
                "final": False,
                "delivery_id": delivery_id,
                "initiator": initiator,
            },
        )
        return exchange_id, opened_at

    def exchange_finish(exchange_id: str, **kwargs: object) -> None:
        status = kwargs["status"]
        exchanges[exchange_id] = {
            **exchanges.get(exchange_id, {}),
            "kind": status.kind,
            "code": status.code,
            "message": status.message,
            "final": status.final,
        }

    def exchange_read(exchange_id: str) -> dict | None:
        return exchanges.get(exchange_id)

    def start(
        on_turn: Callable[..., None],
        agent_name: str = "figrecipe",
        *,
        on_control: Callable[[str], None] | None = None,
        open_exchange: Callable[..., tuple[str, str]] | None = None,
        read_exchange: Callable[[str], dict | None] | None = None,
    ) -> int:
        server = bridge.build_server(
            host="127.0.0.1",
            port=0,
            on_turn=on_turn,
            agent_name=agent_name,
            on_control=on_control,
            exchange_open=open_exchange or exchange_open,
            exchange_finish=exchange_finish,
            exchange_read=read_exchange or exchange_read,
        )
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        threads.append(thread)
        start.server_at[int(port)] = server
        return int(port)

    # port -> live server, for tests that must observe the bridge going idle.
    start.server_at = {}
    yield start
    for server in servers:
        server.shutdown()
        server.server_close()
    for thread in threads:
        thread.join(timeout=5)


@pytest.fixture
def isolated_home(tmp_path: Path) -> Iterator[Path]:
    """Redirect HOME so the bridge's state-dir writes stay in the sandbox."""
    import os

    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved
