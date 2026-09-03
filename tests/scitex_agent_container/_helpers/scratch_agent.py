"""One real ``/uvwork`` agent spec, shared by the two ADR-0024 launch suites.

``runtimes/test__apptainer_scratch.py`` (the layout and the emitted bind) and
``runtimes/test__apptainer_scratch_launch.py`` (the read-only / launch split)
both need a real, loadable spec whose NAME is what the bind path is keyed on.
Written once here so the two suites cannot drift onto differently-shaped
specs — a split-behaviour test that passes over a different agent than the
bind test is worth very little.

Nothing here is a mock (PA-306): a real v3 spec file on disk, read back by
the real loader.
"""

from __future__ import annotations

from pathlib import Path

from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

__all__ = ["UVWORK_AGENT_NAME", "load_uvwork_agent", "uvwork_binds"]

#: The spec DIRECTORY name, which for this ``host:`` spec is also its
#: effective id — so a path built from either spelling looks the same here.
#: The case where they DIFFER is a ``hosts:`` spec, covered in
#: ``_maintenance/test__scratch_migrate.py``.
UVWORK_AGENT_NAME = "agt"

_BASE_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: t
    sac-builtin: "off"
spec:
  runtime: tui
  host: ${HOSTNAME}
  workdir: /tmp/agt-work
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
    model: claude-opus-4-8[1m]
    flags:
      - --dangerously-skip-permissions
"""


def load_uvwork_agent(tmp_path: Path):
    """A real, loadable spec named ``agt`` (the directory name is the name)."""
    from scitex_agent_container.config import load_config

    spec_dir = tmp_path / "agents" / UVWORK_AGENT_NAME
    spec_dir.mkdir(parents=True)
    spec = spec_dir / "spec.yaml"
    spec.write_text(explicitize_yaml(_BASE_SPEC), encoding="utf-8")
    return load_config(str(spec))


def uvwork_binds(argv: list[str]) -> list[str]:
    """Every ``--bind`` value in ``argv`` whose DESTINATION is ``/uvwork``."""
    from scitex_agent_container.runtimes._apptainer_scratch import (
        UVWORK_CONTAINER_PATH,
    )

    out = []
    for i, arg in enumerate(argv):
        if arg == "--bind" and i + 1 < len(argv):
            parts = argv[i + 1].split(":")
            if (parts[1] if len(parts) > 1 else parts[0]) == UVWORK_CONTAINER_PATH:
                out.append(argv[i + 1])
    return out
