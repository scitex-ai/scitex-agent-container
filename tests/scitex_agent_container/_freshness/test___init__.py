#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: tests/scitex_agent_container/_freshness/test___init__.py

"""Does sac ACT on the primitive's verdict, or merely call it?

That distinction is the whole point of this file. Wiring a checker in and
then rendering its output through a path that always exits 0 is
indistinguishable, from the outside, from not wiring it in at all — and it
is the more dangerous of the two, because it looks done. So the tests here
assert on things a wired-but-ignoring implementation could not produce: the
tri-state EXIT CODE, the remedy text the primitive chose, and the loudness
of the surface.

NO MOCKS. The logic under test is the real
``scitex_dev.versioning.build_report``; the only substitution is
``StaticSources``, a genuine implementation of the ``Sources`` protocol
whose backing store is a dict rather than a network, fed the shape of
evidence the real systems return. Nothing is monkeypatched. A mocked
``check_currency`` would let this suite pass against a sac that ignores
every verdict — exactly the failure being tested for.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

#: "a git command that pulls" — the shape an editable remedy must have. Not
#: the literal string `git pull`: the primitive composes
#: `git -C <repo> pull --ff-only` so the command works from any CWD and can
#: never rewrite unpushed commits, and an older primitive emits a bare
#: `git pull`. Both are the same promise; a `pip install -U` is not.
#: Unanchored on purpose: `.match()` pins it to the start of a REMEDY
#: string, while `.search()` finds the `fix:` line inside rendered output.
_GIT_PULL = re.compile(r"git\b(?:\s+\S+)*?\s+pull\b")

from scitex_agent_container._freshness import (
    DIST_NAME,
    LISTEN_UNIT,
    RELEASE_WORKFLOW,
    check_currency,
    is_stale,
    sac_versioning_config,
    stale_findings,
    warn_once,
)
from scitex_agent_container._freshness._expectations import EXPECTATIONS
from scitex_agent_container.cli_pkg.freshness_cmds import (
    EXIT_FRESH,
    EXIT_STALE,
    EXIT_UNKNOWN,
    _render,
)

# The primitive is a `[dev]` extra, and its ABSENCE is a supported state that
# MUST stay tested — a sac without scitex-dev has to report UNKNOWN, never
# FRESH and never a crash.
#
# So the skip is scoped to the classes that need real verdicts, NOT applied to
# the module. A module-level `importorskip` would skip TestDegradesToUnknown
# too — the one class whose entire purpose is the primitive being missing —
# and it would do so precisely when the primitive is missing. That is not a
# hypothetical: PyPI's newest scitex-dev is 0.31.1, and `git ls-tree v0.31.1
# -- src/scitex_dev/versioning` is EMPTY, so CI installing `scitex-dev>=0.31.1`
# gets a build with no primitive in it. Under a module-level skip this whole
# file would have reported green in CI while executing nothing at all.
#
# FOLLOW-UP: raise the `[dev]` floor to the first scitex-dev release that
# ships `scitex_dev.versioning` once it is published. Until then the
# real-verdict classes below genuinely cannot run in CI, and they say so out
# loud instead of vanishing.
try:
    from scitex_dev import versioning
except (
    ImportError
):  # stx-allow: fallback (reason: absence is a tested state, see above)
    versioning = None

requires_primitive = pytest.mark.skipif(
    versioning is None,
    reason=(
        "scitex_dev.versioning is not installed — real-verdict tests need it. "
        "The degradation tests in TestDegradesToUnknown still run."
    ),
)


def _report(**evidence):
    """A REAL report from the REAL check logic, over recorded evidence."""
    return versioning.build_report(
        sac_versioning_config(), versioning.StaticSources(**evidence)
    )


def _upstream_evidence(behind: int) -> dict:
    """The second editable axis, when the installed primitive has it.

    scitex-dev grew a distinction this fixture predates. Distance from the
    latest release tag says only that the tag is not in HEAD's history, and
    release tags are cut on `main` — so a perfectly healthy `develop` is
    permanently "behind" one, and reporting STALE on that alone prints a
    `git pull` that can never close the gap. Distance from the TRACKING
    REMOTE is the one gap a pull does close, so it is now the only fact that
    may be judged STALE; tag distance on its own reads as UNKNOWN.

    Supplied CONDITIONALLY because sac's `[dev]` floor still permits a
    primitive that predates these keys, and ``StaticSources`` rejects an
    unknown kwarg. FOLLOW-UP: inline them once the floor moves past the
    release that ships them — the same floor bump the module docstring
    already asks for.
    """
    if versioning is None:
        return {}
    import inspect

    params = inspect.signature(versioning.StaticSources).parameters
    if "editable_behind_upstream" not in params:
        return {}
    return {
        "editable_behind_upstream": behind,
        "editable_repo": "/home/ywatanabe/proj/scitex-agent-container",
    }


# An editable checkout behind its own release tag — the operator's actual
# situation: his `.venv` advertised 0.21.21 from a frozen .dist-info while
# executing current develop. The remote genuinely carries those 7 commits,
# which is what makes this both STALE and fixable by a pull.
EDITABLE_BEHIND = dict(
    install_kind="editable",
    effective_version="0.21.24",
    metadata_version="0.21.21",
    module_origin="/home/ywatanabe/proj/scitex-agent-container/src",
    executable="/home/ywatanabe/proj/scitex-agent-container/.venv/bin/python",
    editable_ahead_behind=(0, 7),
    pypi_latest="0.21.24",
    **_upstream_evidence(7),
)

# A wheel genuinely behind what shipped — the one case where a version
# compare is honest and `pip install -U` is the correct remedy.
WHEEL_BEHIND = dict(
    install_kind="wheel",
    effective_version="0.21.11",
    metadata_version="0.21.11",
    module_origin="/opt/venv-sac/lib/python3.12/site-packages/scitex_agent_container",
    executable="/opt/venv-sac/bin/python",
    pypi_latest="0.21.24",
)


@pytest.fixture
def stale_editable():
    """A STALE report from an editable checkout behind its tag."""
    report = _report(**EDITABLE_BEHIND)
    if report.state is not versioning.Currency.STALE:
        pytest.fail(
            "precondition: the primitive must judge this evidence STALE, "
            "otherwise the tests below prove nothing about sac"
        )
    return report


@pytest.fixture
def stale_wheel():
    """A STALE report from a wheel behind PyPI."""
    return _report(**WHEEL_BEHIND)


@pytest.fixture
def fresh_report():
    """A positively-FRESH report: every check has evidence and it is clean."""
    return _report(
        install_kind="editable",
        effective_version="0.21.24",
        metadata_version="0.21.11",
        module_origin="/w/src",
        executable="/w/.venv/bin/python",
        editable_ahead_behind=(3, 0),
        pypi_latest="0.21.24",
        pypi_versions={"0.21.24"},
        git_tags=["v0.21.24"],
        release_runs=[{"conclusion": "success", "status": "completed"}],
        daemon_started_at=2_000.0,
        installed_at=1_000.0,
    )


@pytest.fixture
def blind_report():
    """Every source unavailable — the offline / cannot-see case."""
    return _report(module_origin="/w/src", executable="/w/.venv/bin/python")


@requires_primitive
class TestActsOnTheVerdict:
    """The mutation-prove: a STALE verdict must make the surface go loud.

    MEASURED, not asserted. Replacing the body of
    ``freshness_cmds._exit_for`` with a bare ``return EXIT_FRESH`` — the
    exact wired-but-ignored implementation, which still calls the primitive
    and still renders every finding — turns four tests RED:

        test_a_stale_verdict_produces_the_stale_exit_code   assert 0 == 1
        test_a_wheel_behind_pypi_is_still_stale             assert 0 == 1
        test_an_empty_report_is_unknown                     assert 0 == 2
        test_blind_evidence_renders_unknown                 assert 0 == 2

    Note what the last two catch: under that mutation UNKNOWN renders as
    FRESH. A suite that only checked "does a stale report print the word
    STALE" would have stayed green through all of it, because the mutation
    leaves the text untouched and only destroys the verdict.
    """

    def test_a_stale_verdict_produces_the_stale_exit_code(self, stale_editable):
        # Arrange
        report = stale_editable
        # Act
        _text, code = _render(report, as_json=False)
        # Assert — an implementation that called check_currency() and then
        # ignored the answer exits 0 here. This is the assertion that goes
        # RED against wired-but-ignored.
        assert code == EXIT_STALE

    def test_a_stale_verdict_is_announced_in_the_output(self, stale_editable):
        # Arrange
        report = stale_editable
        # Act
        text, _code = _render(report, as_json=False)
        # Assert
        assert "STALE" in text

    def test_a_stale_verdict_carries_the_remedy_to_the_user(self, stale_editable):
        # Arrange
        report = stale_editable
        # Act
        text, _code = _render(report, as_json=False)
        # Assert — an alarm that does not say what to DO gets ignored. Matched
        # as "a git command that pulls" rather than the literal `git pull`,
        # because the primitive now composes `git -C <repo> pull --ff-only`:
        # CWD-independent so it cannot hit the wrong repo, and never
        # `--rebase`, which would rewrite the operator's unpushed work.
        assert _GIT_PULL.search(text), text

    def test_a_fresh_verdict_exits_zero(self, fresh_report):
        # Arrange
        report = fresh_report
        # Act
        _text, code = _render(report, as_json=False)
        # Assert — the negative control. An implementation that shouted on
        # everything would also pass the stale tests above.
        assert code == EXIT_FRESH

    def test_the_fresh_fixture_really_is_fresh(self, fresh_report):
        # Arrange
        report = fresh_report
        # Act
        state = report.state
        # Assert
        assert state is versioning.Currency.FRESH

    def test_is_stale_is_true_for_a_stale_report(self, stale_editable):
        # Arrange
        report = stale_editable
        # Act
        verdict = is_stale(report)
        # Assert
        assert verdict is True

    def test_is_stale_is_false_for_unknown(self):
        # Arrange — `None` is UNKNOWN at this seam.
        report = None
        # Act
        verdict = is_stale(report)
        # Assert — UNKNOWN must not be folded into the alarm.
        assert verdict is False

    def test_stale_findings_is_empty_for_unknown(self):
        # Arrange
        report = None
        # Act
        findings = stale_findings(report)
        # Assert
        assert findings == ()


@requires_primitive
class TestNeverClobbersAnEditableCheckout:
    """The dangerous case, guarded on our side too.

    scitex-dev guards this structurally — its editable branch never runs a
    version-string compare and never composes an upgrade command. This is
    sac's belt-and-braces: the closed PR #677 would have told the operator
    to `pip install -U` over his editable checkout on his primary machine,
    replacing a healthy working tree with a wheel. A false RED whose remedy
    destroys the thing it complains about is worse than silence, so it gets
    a dedicated negative test at the display layer — where the command
    would actually be printed.
    """

    @pytest.mark.parametrize(
        "forbidden", ["pip install -u", "pip install --upgrade", "--force-reinstall"]
    )
    def test_no_editable_remedy_is_a_clobbering_command(
        self, stale_editable, forbidden
    ):
        # Arrange — metadata (0.21.21) is far behind PyPI (0.21.24); a naive
        # version compare fires STALE here and reaches for `pip install -U`.
        #
        # The assertion is scoped to REMEDY fields on purpose. A blanket
        # substring scan of the whole payload cannot tell a command from
        # prose, and upstream's `detail` legitimately contains the sentence
        # "never `pip install -U`, which would clobber the editable
        # checkout" — an explanation of the danger. Matching that would make
        # this test fail on the very text that documents the guard, which is
        # the kind of false red that gets a test deleted rather than fixed.
        # What must never happen is a COMMAND the operator could run.
        report = stale_editable
        # Act
        remedies = " ".join(f.remedy for f in report.findings).lower()
        # Assert
        assert forbidden not in remedies

    @pytest.mark.parametrize(
        "forbidden", ["pip install -u", "pip install --upgrade", "--force-reinstall"]
    )
    def test_no_fix_line_in_the_human_output_is_a_clobbering_command(
        self, stale_editable, forbidden
    ):
        # Arrange — the same property at the surface the operator actually
        # reads. `fix:` lines are the ones written to be copy-pasted.
        report = stale_editable
        text, _code = _render(report, as_json=False)
        # Act
        fix_lines = " ".join(
            line for line in text.lower().splitlines() if "fix:" in line
        )
        # Assert
        assert forbidden not in fix_lines

    def test_an_editable_remedy_is_a_git_pull(self, stale_editable):
        # Arrange
        report = stale_editable
        # Act
        remedies = [f.remedy for f in report.stale if f.remedy]
        # Assert — stated positively, not merely as the absence of pip. The
        # shape is "a git command that pulls", which admits the primitive's
        # `git -C <repo> pull --ff-only` without admitting a wheel clobber.
        assert all(_GIT_PULL.match(r) for r in remedies), remedies

    def test_no_editable_remedy_rewrites_unpushed_work(self, stale_editable):
        # Arrange — `--rebase` rewrites commits the developer has not pushed.
        # A WARNING's remedy must not be able to cost anybody their work.
        report = stale_editable
        # Act
        remedies = " ".join(f.remedy for f in report.stale)
        # Assert
        assert "--rebase" not in remedies

    def test_an_editable_stale_report_has_a_remedy_at_all(self, stale_editable):
        # Arrange
        report = stale_editable
        # Act
        remedies = [f.remedy for f in report.stale if f.remedy]
        # Assert
        assert remedies

    def test_a_wheel_install_still_gets_its_upgrade_command(self, stale_wheel):
        # Arrange — the negative control. If the guard above were built by
        # suppressing `pip install -U` everywhere, this would fail, and the
        # suppression would have broken the one case where upgrading is right.
        report = stale_wheel
        # Act
        text, _code = _render(report, as_json=False)
        # Assert
        assert "pip install -U" in text

    def test_a_wheel_behind_pypi_is_still_stale(self, stale_wheel):
        # Arrange
        report = stale_wheel
        # Act
        _text, code = _render(report, as_json=False)
        # Assert
        assert code == EXIT_STALE


class TestUnknownIsNeverFresh:
    def test_no_verdict_at_all_exits_unknown(self):
        # Arrange — what check_currency returns when it could not obtain a
        # verdict (primitive absent or broken).
        report = None
        # Act
        _text, code = _render(report, as_json=False)
        # Assert
        assert code == EXIT_UNKNOWN

    def test_no_verdict_at_all_does_not_exit_fresh(self):
        # Arrange
        report = None
        # Act
        _text, code = _render(report, as_json=False)
        # Assert
        assert code != EXIT_FRESH

    def test_the_unknown_surface_never_says_fresh(self):
        # Arrange
        report = None
        # Act
        text, _code = _render(report, as_json=False)
        # Assert
        assert "FRESH" not in text

    def test_the_unknown_message_refuses_to_be_read_as_health(self):
        # Arrange — silence would be misread as health, so the text has to
        # actively refuse that reading.
        report = None
        # Act
        text, _code = _render(report, as_json=False)
        # Assert
        assert "NOT a clean bill of health" in text

    @requires_primitive
    def test_an_empty_report_is_unknown(self):
        # Arrange — the emptiest possible report. Nothing was checked.
        report = versioning.Report(findings=(), generated_at=1.0)
        # Act
        _text, code = _render(report, as_json=False)
        # Assert
        assert code == EXIT_UNKNOWN

    @requires_primitive
    def test_blind_evidence_renders_unknown(self, blind_report):
        # Arrange — every source offline. A check that cannot reach its
        # evidence must not report FRESH.
        report = blind_report
        # Act
        _text, code = _render(report, as_json=False)
        # Assert
        assert code == EXIT_UNKNOWN

    @requires_primitive
    def test_blind_evidence_is_unknown_upstream_too(self, blind_report):
        # Arrange
        report = blind_report
        # Act
        state = report.state
        # Assert
        assert state is versioning.Currency.UNKNOWN


@requires_primitive
class TestNamesTheBinaryThatAnswered:
    """Property 3, at sac's own output boundary.

    "0.21.21 is behind 0.21.24" is not actionable when five installs on one
    host could be speaking. The origin + interpreter stamp is the only
    reason the five-install problem was findable, so this asserts it
    survives rendering rather than trusting that it does.
    """

    def test_the_origin_reaches_the_rendered_output(self, stale_wheel):
        # Arrange
        report = stale_wheel
        # Act
        text, _code = _render(report, as_json=False)
        # Assert
        assert WHEEL_BEHIND["module_origin"] in text

    def test_the_interpreter_reaches_the_rendered_output(self, stale_wheel):
        # Arrange
        report = stale_wheel
        # Act
        text, _code = _render(report, as_json=False)
        # Assert
        assert WHEEL_BEHIND["executable"] in text

    def test_the_origin_survives_the_json_surface(self, stale_wheel):
        # Arrange
        report = stale_wheel
        # Act
        payload = json.loads(_render(report, as_json=True)[0])
        # Assert
        assert WHEEL_BEHIND["module_origin"] in {
            f["data"].get("origin") for f in payload["findings"]
        }

    def test_the_interpreter_survives_the_json_surface(self, stale_wheel):
        # Arrange
        report = stale_wheel
        # Act
        payload = json.loads(_render(report, as_json=True)[0])
        # Assert
        assert WHEEL_BEHIND["executable"] in {
            f["data"].get("executable") for f in payload["findings"]
        }

    def test_two_installs_at_the_same_version_render_differently(self):
        # Arrange — the actual diagnostic need: same number, different
        # binary. Drop the stamp and these collapse into one indistinguishable
        # message, and the five-install problem is invisible again.
        one = _report(**{**WHEEL_BEHIND, "module_origin": "/opt/a/pkg"})
        two = _report(**{**WHEEL_BEHIND, "module_origin": "/opt/b/pkg"})
        # Act
        rendered = (_render(one, as_json=False)[0], _render(two, as_json=False)[0])
        # Assert
        assert rendered[0] != rendered[1]


@requires_primitive
class TestSacOwnedConstants:
    def test_the_release_workflow_file_exists_in_this_repo(self):
        # Arrange — a typo here does not fail loudly; it silently makes the
        # release-run check UNKNOWN forever.
        repo_root = Path(__file__).resolve().parents[3]
        # Act
        workflow = repo_root / ".github" / "workflows" / RELEASE_WORKFLOW
        # Assert
        assert workflow.is_file(), f"{workflow} does not exist"

    def test_the_config_carries_sacs_distribution_name(self):
        # Arrange
        cfg = sac_versioning_config()
        # Act
        dist = cfg.dist
        # Assert
        assert dist == DIST_NAME

    def test_the_config_carries_sacs_daemon_unit(self):
        # Arrange
        cfg = sac_versioning_config()
        # Act
        unit = cfg.systemd_unit
        # Assert
        assert unit == LISTEN_UNIT

    def test_the_config_carries_a_non_empty_symbol_registry(self):
        # Arrange
        cfg = sac_versioning_config()
        # Act
        expectations = cfg.expectations
        # Assert
        assert expectations

    def test_the_cache_path_is_not_resolved_at_import_time(self, tmp_path):
        # Arrange — $HOME differs between container (/home/agent) and host;
        # an import-time Path.home() constant cannot be redirected later.
        cfg = sac_versioning_config()
        target = tmp_path / "currency.json"
        os.environ[cfg.env_cache] = str(target)
        # Act
        try:
            resolved = versioning.cache_path(cfg)
        finally:
            del os.environ[cfg.env_cache]
        # Assert
        assert resolved == target


class TestExpectationsRegistryIsHonest:
    """An expectation naming a symbol that no longer exists is a permanent
    false STALE aimed at the operator. Catch the rename here, not there."""

    @pytest.mark.parametrize(
        ("module", "symbol"),
        [(row[0], row[1]) for row in EXPECTATIONS],
        ids=[f"{row[0].rsplit('.', 1)[-1]}.{row[1]}" for row in EXPECTATIONS],
    )
    def test_every_expected_symbol_exists_in_this_checkout(self, module, symbol):
        # Arrange
        mod = importlib.import_module(module)
        # Act
        present = hasattr(mod, symbol)
        # Assert
        assert present, (
            f"{module}.{symbol} is in the expectation registry but does not "
            "exist here — it was renamed or removed. Left alone this becomes "
            "a STALE verdict the operator cannot act on."
        )

    @pytest.mark.parametrize(
        ("symbol", "why"),
        [(row[1], row[3]) for row in EXPECTATIONS],
        ids=[row[1] for row in EXPECTATIONS],
    )
    def test_every_expectation_explains_what_breaks_without_it(self, symbol, why):
        # Arrange — the `why` lands in front of a human when something is
        # wrong; an empty one wastes the only moment anyone is paying
        # attention.
        minimum = 40
        # Act
        length = len(why)
        # Assert
        assert length > minimum, f"{symbol} has a thin `why`"


class TestDegradesToUnknown:
    """scitex-dev is a [dev] extra. Its absence must be UNKNOWN — not a
    crash, and not FRESH.

    NOTHING in this class is skipped when the primitive is missing. That is
    the entire point: these are the guarantees that hold *because* it can be
    missing, so skipping them exactly then would test the easy half of the
    contract and leave the half that ships to users unexercised.
    """

    def test_warn_once_never_raises_on_the_cli_entry_path(self):
        # Arrange — rule 2: a staleness warning that can break the CLI is
        # worse than the staleness it reports. This runs on EVERY `sac`
        # invocation, with or without scitex-dev installed.
        expected = (0, 3)
        # Act
        code = warn_once()
        # Assert
        assert code in expected

    def test_check_currency_never_raises_with_live_sources(self):
        # Arrange — no StaticSources here on purpose: this must exercise the
        # real path, which is the one that runs when scitex-dev is absent.
        # Act
        report = check_currency()
        # Assert — a verdict object or an honest None. Never an exception.
        assert report is None or hasattr(report, "state")

    def test_a_missing_primitive_is_reported_not_assumed(self):
        # Arrange — `available()` exists so callers can render "cannot tell"
        # instead of a confident wrong answer.
        from scitex_agent_container._freshness import available

        # Act
        answer = available()
        # Assert
        assert isinstance(answer, bool)

    def test_running_version_always_labels_its_provenance(self):
        # Arrange — with the primitive gone this must fall back to metadata
        # and SAY "metadata", never pass a fossil off as verified.
        from scitex_agent_container._freshness import running_version

        allowed = {"content", "metadata", "unknown"}
        # Act
        _version, source = running_version()
        # Assert
        assert source in allowed

    def test_the_hot_path_does_not_import_the_heavy_primitive(self):
        # Arrange — warn_once() runs before EVERY `sac` command. Importing
        # scitex_dev.versioning costs 201 ms (measured) against a documented
        # ~150 ms budget for sac's entire click + LazyGroup startup, so the
        # naive implementation more than doubled every invocation including
        # tab completion. That is how a staleness check earns an env var that
        # turns it off — and then there is no check at all.
        #
        # The gate is a ~1 ms stdlib read of the refresher's cache; nothing
        # heavy loads unless there is positively-STALE news to deliver. This
        # pins that, because "simplifying" the gate away would reintroduce
        # the cost silently — it breaks no behaviour, only speed.
        code = (
            "import sys;"
            "from scitex_agent_container._freshness import warn_once;"
            "warn_once();"
            "print('scitex_dev' in sys.modules)"
        )
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[3] / "src")}
        # Act
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        # Assert — a subprocess is the only honest way to ask this; in-process
        # the module is already imported by the rest of the suite.
        assert proc.stdout.strip().endswith("False"), proc.stdout + proc.stderr

    def test_an_unavailable_verdict_renders_unknown_not_fresh(self):
        # Arrange — the end-to-end degradation, at the surface the operator
        # sees. This is what a sac without scitex-dev actually prints.
        report = None
        # Act
        _text, code = _render(report, as_json=False)
        # Assert
        assert code == EXIT_UNKNOWN


# EOF
