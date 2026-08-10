"""CLI tests for ``sac agents hooks`` — THE SEAM, not just the resolver.

WHY THIS FILE EXISTS. PR #949 shipped a defect at exactly this seam: click
passed ``strict=False`` when a flag was ABSENT, the resolver read that explicit
``False`` as "the caller demanded leniency", and the gate was silently disabled
on every CLI start. Every unit test passed, because none of them drove the CLI
into the resolver — they called the resolver directly, where the absent flag
arrives as ``None`` and the default holds.

So these tests invoke the REAL top-level ``main`` group with ``CliRunner``, all
the way down to the refusal, and the load-bearing case is the one where NO FLAG
IS PASSED. A gate that only works when you remember to ask for it is not a gate.

PA-306 no-mocks. Everything is real:

* the real registered command, reached through the real LazyGroup (so a missing
  registration in ``agent_group.py`` fails here rather than in production);
* a REAL ``<registry>/<name>/spec.yaml`` written to ``tmp_path`` and resolved
  through ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` — the same search chain a live
  container uses — built with the shared ``explicit_doc`` scaffold so it
  satisfies the explicit-spec red-start validator;
* REAL hook directories, in the two shapes that disagreed on 2026-08-10;
* the environment controlled per-invocation via ``CliRunner.invoke(env=)``
  (``None`` unsets), so the suite is hermetic even when it runs INSIDE a sac
  agent container that has ``SAC_NAME`` / ``HOME`` set to real values.

STX-TQ002/TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._agents_hooks import _true_or_unset
from scitex_agent_container.cli_pkg._main import main

from .._claude_hooks._trees import effective_home, layer_only_home
from .._helpers.explicit_spec import explicit_doc

_AGENT = "hook-floor-fixture"
_MISSED = "log_post_tool_use.sh"
_FLOOR = {"post-tool-use": [_MISSED]}


def _write_registry(tmp_path: Path, floor) -> Path:
    """Write a real, loadable ``<registry>/<name>/spec.yaml``; return registry."""
    overrides: dict = {"host": "${HOSTNAME}"}
    if floor is not None:
        overrides["required_claude_hooks"] = floor
    doc = explicit_doc(overrides)
    agent_dir = tmp_path / "agents" / _AGENT
    agent_dir.mkdir(parents=True)
    (agent_dir / "spec.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    return tmp_path / "agents"


def _env(tmp_path: Path, *, home: Path, floor) -> dict:
    """Hermetic env: this process IS the fixture agent, with ``home`` as $HOME."""
    return {
        "HOME": str(home),
        "SAC_NAME": _AGENT,
        "SCITEX_AGENT_CONTAINER_NAME": None,
        "SAC_ALLOW_MISSING_HOOKS": None,
        "SCITEX_AGENT_CONTAINER_ALLOW_MISSING_HOOKS": None,
        "SCITEX_AGENT_CONTAINER_YAML_DIRS": str(_write_registry(tmp_path, floor)),
        "SCITEX_DIR": str(tmp_path / "empty-scitex-dir"),
        "APPTAINER_CONTAINER": None,
        "SINGULARITY_CONTAINER": None,
    }


def _invoke(env: dict, *args: str):
    return CliRunner().invoke(main, ["agents", "hooks", *args], env=env)


class TestTheAbsentFlagDoesNotDisableTheGate:
    """The PR #949 defect class, driven through the ACTUAL CLI entry point."""

    def test_unpassed_override_resolves_to_none(self):
        # Arrange — click hands a bare flag ``False`` when it is not passed.
        # Act
        resolved = _true_or_unset(None, None, False)
        # Assert — None, NOT False: "no instruction", not "be lenient".
        assert resolved is None

    def test_passed_override_resolves_to_true(self):
        # Arrange
        # Act
        resolved = _true_or_unset(None, None, True)
        # Assert
        assert resolved is True

    def test_cli_with_no_flags_still_refuses(self, tmp_path: Path):
        # Arrange — the end-to-end shape of the #949 bug: what the CLI passes
        # when NOTHING is given must still reach the gate as "no instruction".
        env = _env(tmp_path, home=layer_only_home(tmp_path), floor=_FLOOR)
        # Act
        result = _invoke(env)
        # Assert
        assert result.exit_code == 1

    def test_cli_with_no_flags_names_the_missing_hook(self, tmp_path: Path):
        # Arrange
        env = _env(tmp_path, home=layer_only_home(tmp_path), floor=_FLOOR)
        # Act
        result = _invoke(env)
        # Assert
        assert _MISSED in result.output


class TestTheFourOutcomesThroughTheCli:
    def test_satisfied_floor_exits_zero(self, tmp_path: Path):
        # Arrange — the EFFECTIVE view carries the hook the layer view lacks.
        env = _env(tmp_path, home=effective_home(tmp_path), floor=_FLOOR)
        # Act
        result = _invoke(env)
        # Assert
        assert result.exit_code == 0

    def test_undeclared_floor_exits_zero(self, tmp_path: Path):
        # Arrange — 101 of 102 fleet specs are here; they must keep booting.
        env = _env(tmp_path, home=layer_only_home(tmp_path), floor=None)
        # Act
        result = _invoke(env)
        # Assert
        assert result.exit_code == 0

    def test_undeclared_floor_emits_no_refusal(self, tmp_path: Path):
        # Arrange — "no refusal, no warning spam".
        env = _env(tmp_path, home=layer_only_home(tmp_path), floor=None)
        # Act
        result = _invoke(env)
        # Assert
        assert "refusing to start" not in result.output

    def test_override_exits_zero_despite_the_missing_hook(self, tmp_path: Path):
        # Arrange
        env = _env(tmp_path, home=layer_only_home(tmp_path), floor=_FLOOR)
        # Act
        result = _invoke(env, "--allow-missing-hooks")
        # Assert
        assert result.exit_code == 0

    def test_override_records_that_it_overrode(self, tmp_path: Path):
        # Arrange — a silent override is a slower version of the warning nobody
        # read, so the bypass must appear in the operator's output.
        env = _env(tmp_path, home=layer_only_home(tmp_path), floor=_FLOOR)
        # Act
        result = _invoke(env, "--allow-missing-hooks")
        # Assert
        assert "BYPASSED" in result.output

    def test_env_override_exits_zero(self, tmp_path: Path):
        # Arrange — the container's boot step is its own process, so `spec.env`
        # is the transport that actually survives to it.
        env = _env(tmp_path, home=layer_only_home(tmp_path), floor=_FLOOR)
        env["SAC_ALLOW_MISSING_HOOKS"] = "1"
        # Act
        result = _invoke(env)
        # Assert
        assert result.exit_code == 0


class TestTheCliConsultsTheEffectiveView:
    """The 67-vs-71 regression, asserted at the surface an operator uses."""

    def test_same_spec_refuses_against_the_layer_only_home(self, tmp_path: Path):
        # Arrange
        env = _env(tmp_path, home=layer_only_home(tmp_path), floor=_FLOOR)
        # Act
        result = _invoke(env)
        # Assert
        assert result.exit_code == 1

    def test_same_spec_passes_against_the_effective_home(self, tmp_path: Path):
        # Arrange — identical spec, identical command; only the resolved $HOME
        # differs. If the CLI ever reads a single layer again, this goes red.
        env = _env(tmp_path, home=effective_home(tmp_path), floor=_FLOOR)
        # Act
        result = _invoke(env)
        # Assert
        assert result.exit_code == 0

    def test_report_names_the_home_it_measured(self, tmp_path: Path):
        # Arrange — a verdict whose measurement site is invisible is a verdict
        # nobody can check.
        home = effective_home(tmp_path)
        env = _env(tmp_path, home=home, floor=_FLOOR)
        # Act
        result = _invoke(env, "--json")
        # Assert
        assert json.loads(result.stdout)["inventory"]["root"] == str(
            home / ".claude" / "hooks"
        )


class TestOffAgentReadsDoNotPretendToBeAnswers:
    def test_a_foreign_home_reports_unknown_not_satisfied(self, tmp_path: Path):
        # Arrange — hooks are right there and readable; they are just somebody
        # else's. A confident verdict here is the host-side answer this whole
        # command exists to stop.
        env = _env(tmp_path, home=effective_home(tmp_path), floor=_FLOOR)
        env["SAC_NAME"] = "some-other-agent"
        # Act
        result = _invoke(env, _AGENT, "--json")
        # Assert
        assert json.loads(result.stdout)["floor"]["satisfied"] is None

    def test_a_foreign_home_does_not_refuse(self, tmp_path: Path):
        # Arrange — refusing on "I could not check" would ground the fleet.
        env = _env(tmp_path, home=layer_only_home(tmp_path), floor=_FLOOR)
        env["SAC_NAME"] = "some-other-agent"
        # Act
        result = _invoke(env, _AGENT)
        # Assert
        assert result.exit_code == 0


class TestTheJsonIsTheStandardHealthShape:
    def test_json_carries_the_four_standard_keys(self, tmp_path: Path):
        # Arrange
        env = _env(tmp_path, home=effective_home(tmp_path), floor=_FLOOR)
        # Act
        result = _invoke(env, "--json")
        # Assert
        assert {"package", "ok", "checks", "summary"} <= set(json.loads(result.stdout))

    def test_json_stays_parseable_while_the_gate_is_refusing(self, tmp_path: Path):
        # Arrange — the refusal banner is LOUD (ERROR log + a stderr echo), and
        # the boot gate reads this same output. A diagnostic that corrupts the
        # machine-readable channel it shares is how a caller learns to stop
        # parsing it. The banner belongs on stderr; stdout stays pure JSON.
        env = _env(tmp_path, home=layer_only_home(tmp_path), floor=_FLOOR)
        # Act
        result = _invoke(env, "--json")
        # Assert
        assert json.loads(result.stdout)["floor"]["satisfied"] is False

    def test_json_lists_the_hooks_actually_armed(self, tmp_path: Path):
        # Arrange — "what does this container enforce" needs the list itself.
        env = _env(tmp_path, home=effective_home(tmp_path), floor=_FLOOR)
        # Act
        result = _invoke(env, "--json")
        # Assert
        assert (
            _MISSED in json.loads(result.stdout)["inventory"]["dirs"]["post-tool-use"]
        )
