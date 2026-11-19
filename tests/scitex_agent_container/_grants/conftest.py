"""Shared fixture for the spec.grants render / infer tests.

``world`` is a fake host (``$HOME`` redirected for the module) that holds
every host source a grant can deliver, plus one Claude-TUI spec in it.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Iterator

import pytest
import yaml

_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: t
spec:
  runtime: tui
  host: ${{HOSTNAME}}
  workdir: /tmp/agt-work
  apptainer:
    image: /x.sif
    binds: []
  claude:
    model: claude-opus-4-8[1m]
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def world(tmp_path) -> Iterator[tuple[dict, Path, Path]]:
    """A fake host + one spec; yields ``(raw, spec_path, render_root)``."""
    from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

    base = tmp_path
    home = base / "home"
    (home / ".scitex" / "cards").mkdir(parents=True)
    (home / ".scitex" / "claude-code-telegrammer").mkdir(parents=True)
    _write(home / ".claude" / "commands" / "probe-cmd.md", "# probe\n")
    _write(home / ".claude" / "skills" / "scitex" / "SKILL.md", "# skill\n")
    expires = int((time.time() + 86400) * 1000)
    _write(
        home / ".claude" / ".credentials.json",
        json.dumps({"claudeAiOauth": {"expiresAt": expires}}),
    )
    tokens = home / ".scitex" / "agent-container" / "tokens"
    _write(
        tokens / f"listen-{socket.gethostname()}.token", "test-bearer-not-a-secret\n"
    )
    # pgpass rows for the project roles the specs below derive (exact role,
    # any host): the PostgreSQL-identity preflight never accepts a wildcard user.
    from scitex_agent_container.runtimes._pg_identity_env import derive_pg_role

    roles = {derive_pg_role("agt", project_name=p) for p in ("t", "scitex-scholar")}
    rows = "".join(f"*:*:*:{role}:test-not-a-secret\n" for role in sorted(roles))
    _write(home / ".pgpass", rows).chmod(0o600)
    accounts = home / ".scitex" / "agent-container" / "accounts"
    _write(accounts / "openai" / "fleet-lead-native" / "auth.json", "{}\n")
    agents = home / ".scitex" / "agent-container" / "agents"
    _write(agents / "_shared" / "to_home" / "BASELINE_PROBE.md", "baseline\n")
    spec = _write(agents / "agt" / "spec.yaml", explicitize_yaml(_SPEC))
    _write(agents / "agt" / "to_home" / ".envrc", "export GRANT_ENVRC_PROBE=1\n")
    pool = _write(base / "pool.src", "export GITHUB_TOKEN=test-gh-token-not-a-secret\n")
    quota = _write(base / "quota-cache.json", "{}\n")
    env = {
        "HOME": str(home),
        "SAC_SECRETS_ENVRC": str(pool),
        "SAC_QUOTA_CACHE_HOST_PATH": str(quota),
        "PGPASSFILE": str(home / ".pgpass"),
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        yield yaml.safe_load(spec.read_text()), spec, base / "render"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
