"""Interactive discovery must not resolve every lazy API namespace."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _observe_in_fresh_process(script: str) -> dict:
    source = Path(__file__).resolve().parents[2] / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys, json; sys.path.insert(0, {str(source)!r})\n"
            + script
            + "\nsys.stdout.write(json.dumps(observation))\n",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_dir_advertises_public_names_without_importing_their_implementations():
    # Arrange
    script = """
import scitex_agent_container as sac

names = dir(sac)
observation = {
    'public_names_present': set(sac.__all__).issubset(names),
    'sorted_names': names == sorted(set(names)),
    'api_modules': sorted(name for name in sys.modules if name.startswith('scitex_agent_container._api')),
    'config_loaded': 'scitex_agent_container.config' in sys.modules,
    'registry_loaded': 'scitex_agent_container._state.registry' in sys.modules,
}
"""
    # Act
    observation = _observe_in_fresh_process(script)
    # Assert
    assert observation == {
        "public_names_present": True,
        "sorted_names": True,
        "api_modules": [],
        "config_loaded": False,
        "registry_loaded": False,
    }


def test_accessing_one_noun_only_loads_that_api_namespace():
    # Arrange
    script = """
import scitex_agent_container as sac

agent = sac.agent
unknown_name_refused = False
try:
    sac.not_a_public_api
except AttributeError:
    unknown_name_refused = True
observation = {
    'start_callable': callable(agent.start),
    'cached_identity': sac.agent is agent,
    'loaded_api_modules': sorted(name for name in sys.modules if name.startswith('scitex_agent_container._api.')),
    'public_names_present': set(sac.__all__).issubset(dir(sac)),
    'unknown_name_refused': unknown_name_refused,
}
"""
    # Act
    observation = _observe_in_fresh_process(script)
    # Assert
    assert observation == {
        "start_callable": True,
        "cached_identity": True,
        "loaded_api_modules": ["scitex_agent_container._api.agent"],
        "public_names_present": True,
        "unknown_name_refused": True,
    }
