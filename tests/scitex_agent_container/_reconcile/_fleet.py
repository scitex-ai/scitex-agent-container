"""Shared no-mocks helpers for the reconcile suites.

Real on-disk v3 specs, real ``instances`` rows written by the production
writer, a real temp sac event log, and a real recorder standing in for the ONE
irreversible act. Split out of ``test__pass.py`` so it and
``test__pass_limits.py`` drive the same fleet without either file breaching
the 512-line cap.
"""

from __future__ import annotations

from pathlib import Path

import yaml as _yaml

from scitex_agent_container._reconcile._pass import reconcile_pass
from scitex_agent_container._reconcile._rule import Verdict
from scitex_agent_container._state import state_db

#: A fixed clock. Every suite injects it, so no test can be flaky on time.
NOW = 1_800_000_000.0
HOST = "host-a"


def _scaffold() -> dict:
    """Fully-explicit spec body (red-start ruling 2026-07-21)."""
    from tests.scitex_agent_container._helpers.explicit_spec import (
        explicit_spec,
    )

    return explicit_spec(
        {
            "host": "${HOSTNAME}",
            "runtime": "apptainer",
            "claude": {"model": "claude-opus-4-8[1m]"},
            "apptainer": {"image": "/opt/sac/scitex.sif", "binds": []},
            "health": {"enabled": True, "interval": 60},
        }
    )


class Recorder:
    """A real restart callable that records instead of restarting.

    Not a mock: a plain object with the production signature. ``names`` is
    the evidence a test reads to prove a restart did — or, more often and
    more importantly, did NOT — happen.
    """

    def __init__(self, *, ok: bool = True, boom: Exception | None = None) -> None:
        self.names: list[str] = []
        self._ok = ok
        self._boom = boom

    def __call__(self, name: str) -> bool:
        self.names.append(name)
        if self._boom is not None:
            raise self._boom
        return self._ok


def write_spec(registry: Path, name: str, *, policy: str = "on-failure") -> Path:
    """A real dir-as-SSoT v3 ``<name>/spec.yaml`` the loader accepts."""
    agent_dir = registry / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    body = _scaffold()
    body["workdir"] = f"~/.scitex/agent-container/runtime/agents/{name}"
    # Update in place — replacing the block would strip the other
    # required restart keys (red-start ruling: all keys must stay).
    body["restart"].update({"policy": policy, "max_retries": 3})
    spec = agent_dir / "spec.yaml"
    spec.write_text(
        _yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "metadata": {"labels": {}},
                "spec": body,
            }
        )
    )
    return spec


def sessions(*names: str) -> dict[str, int]:
    """The shape the real batched tmux probe returns for live sessions."""
    return {f"tui-{n}": int(NOW) for n in names}


def ghost(name: str = "alpha") -> str:
    """Tonight's corpse: a row still claiming ACTIVE, session long gone."""
    return state_db.record_instance_start(name=name, host=HOST, pid=4242)


def ended(name: str, reason: str) -> None:
    """A row whose end WAS recorded, with the given ``exit_reason``."""
    instance_id = state_db.record_instance_start(name=name, host=HOST, pid=4242)
    state_db.record_instance_stop(instance_id, exit_reason=reason)


def run_pass(registry, history, events, **overrides):
    """One pass with every real seam wired to this test's temp state.

    Defaults describe a HOST that can see tmux (``in_sif_fn`` False) and an
    EMPTY tmux (``snapshot_fn`` -> ``{}``), i.e. every agent is a corpse —
    the interesting case. Tests override what they are about.

    ``events`` is the sac event log this pass records to — a real temp JSONL
    path. A test that wants an UNWRITABLE one passes ``events_path=`` as an
    override, which lands on the same production keyword.

    ``db_path`` WAS THE SECOND POSITIONAL ARGUMENT AND IS GONE (2026-08-28).
    ``reconcile_pass`` dropped the keyword when ``instances`` moved to
    PostgreSQL, and forwarding a path the pass cannot honour would have let a
    caller believe it had redirected state it had not. Suites that still need
    a temp ``state.db`` on disk keep requesting the fixture; they just no
    longer hand it to this helper.
    """
    kwargs = {
        "specs_dir": registry,
        "history_file": history,
        "events_path": events,
        "now": NOW,
        "snapshot_fn": lambda **_: {},
        "in_sif_fn": lambda: False,
        "local_host_fn": lambda: HOST,
        "restart_fn": Recorder(),
    }
    kwargs.update(overrides)
    return reconcile_pass(**kwargs)


def verdict_of(outcome, name: str) -> Verdict:
    return next(r.verdict for r in outcome.reports if r.name == name)


def detail_of(outcome, name: str) -> str:
    return next(r.detail for r in outcome.reports if r.name == name)
