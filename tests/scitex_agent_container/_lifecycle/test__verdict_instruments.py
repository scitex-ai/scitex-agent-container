"""The INSTRUMENT taxonomy, and the meta-tests that stop it going decorative.

An enumeration nobody is forced to update is a promise of completeness that
quietly stops being true. ``may_destroy`` demanded "2 independent sources" and
counted SOURCE STRINGS — and ``process`` and ``registry`` are the SAME
``os.kill(pid, 0)`` on the SAME pid, by explicit design in both runtimes. So the
two witnesses the destruction gate required were GUARANTEED to agree, and a
single syscall could authorise ``--force --fresh`` on a healthy agent.

The collapse itself is exercised end-to-end, against a REAL reaped pid, in
``test__verdict_instrument_collapse.py``. THIS file guards the classification
that makes the collapse detectable at all:

* every instrument must be CLASSIFIED (what it reads, what it may conclude,
  when it is blind);
* every ``Signal(...)`` anywhere in src must name a DECLARED instrument —
  provably, statically, through variables and conditionals;
* every convicting instrument must be unable to corroborate ITSELF;
* every non-convicting instrument must be unable to report a death at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import scitex_agent_container
from scitex_agent_container._lifecycle import _verdict_instruments
from scitex_agent_container._lifecycle._verdict import (
    ALIVE,
    CONVICTING_INSTRUMENTS,
    DEAD,
    INSTRUMENT_HOST_TMUX,
    INSTRUMENT_INDEPENDENCE,
    INSTRUMENT_LISTEN_BROKER,
    INSTRUMENT_NO_OBSERVATION,
    INSTRUMENT_PID_NAMESPACE,
    INSTRUMENTS,
    SOURCE_DELIVERY,
    SOURCE_PROCESS,
    SOURCE_REGISTRY,
    Signal,
    decide,
)


def _declared_instrument_constants() -> dict[str, str]:
    """``{CONSTANT_NAME: value}`` for every ``INSTRUMENT_*`` string constant."""
    return {
        name: value
        for name, value in vars(_verdict_instruments).items()
        if name.startswith("INSTRUMENT_") and isinstance(value, str)
    }


# --------------------------------------------------------------------------
# Every instrument must be CLASSIFIED.
# --------------------------------------------------------------------------


def test_every_instrument_constant_is_classified_for_independence():
    """Add an INSTRUMENT_* constant without classifying it, and this FAILS.

    The original bug was an enumeration that silently claimed to be complete.
    This makes it load-bearing: a sensor with no INSTRUMENT_INDEPENDENCE entry
    has not been reasoned about, and an unreasoned sensor is exactly the thing
    that poses as an independent second witness.
    """
    # Arrange
    constants = _declared_instrument_constants()
    # Act
    unclassified = sorted(
        name
        for name, value in constants.items()
        if value not in INSTRUMENT_INDEPENDENCE
    )
    # Assert
    assert not unclassified, (
        f"instrument constants with no INSTRUMENT_INDEPENDENCE entry: "
        f"{unclassified}. Declare what the sensor physically READS, which "
        f"VERDICTS it may emit, and WHEN IT IS BLIND — the destruction gate "
        f"counts distinct instruments, so an unclassified one can corroborate a "
        f"kill it never observed."
    )


def test_every_instrument_spec_says_what_it_physically_reads():
    """A classification that says nothing is a classification in name only."""
    # Arrange
    specs = INSTRUMENT_INDEPENDENCE
    # Act
    silent = sorted(name for name, spec in specs.items() if not spec.reads.strip())
    # Assert
    assert not silent, f"instruments that do not say what they read: {silent}"


def test_every_instrument_spec_says_when_it_is_blind():
    """Blindness is the whole failure mode — an unstated blind spot is a trap."""
    # Arrange
    specs = INSTRUMENT_INDEPENDENCE
    # Act
    silent = sorted(name for name, spec in specs.items() if not spec.blind_when.strip())
    # Assert
    assert not silent, f"instruments that do not say when they are blind: {silent}"


def test_every_instrument_spec_declares_the_verdicts_it_may_emit():
    """An instrument with no declared verdicts could smuggle a DEAD through."""
    # Arrange
    specs = INSTRUMENT_INDEPENDENCE
    # Act
    silent = sorted(name for name, spec in specs.items() if not spec.verdicts)
    # Assert
    assert not silent, f"instruments declaring no verdicts: {silent}"


# --------------------------------------------------------------------------
# Every Signal in src must name a DECLARED instrument — provably.
# --------------------------------------------------------------------------


def _resolves_to_declared(
    expr: ast.expr | None, scope: ast.AST, declared: set[str]
) -> bool:
    """Does ``expr`` PROVABLY evaluate to declared instrument constants?

    Static proof, deliberately narrow. We accept exactly three shapes:

    * ``INSTRUMENT_FOO``                        — a declared constant;
    * ``A if cond else B``                      — both branches declared;
    * a local variable assigned ONLY from the above, inside the same function
      (``heartbeat_signal`` legitimately picks its instrument from the runtime,
      because for a TUI agent the beat is a re-report of the SAME tmux snapshot
      ``process_signal`` reads, while an SDK agent beats for itself).

    Everything else — a literal, an f-string, a call, a parameter we cannot see
    through — is REFUSED. That refusal is the point: the destruction gate counts
    instruments, so an instrument the suite cannot verify is an instrument that
    could pose as a second witness.
    """
    if expr is None:
        return False
    if isinstance(expr, ast.IfExp):
        return _resolves_to_declared(
            expr.body, scope, declared
        ) and _resolves_to_declared(expr.orelse, scope, declared)
    if not isinstance(expr, ast.Name):
        return False
    if expr.id in declared:
        return True

    # A local variable: every assignment to it in this scope must itself resolve.
    assignments = [
        node.value
        for node in ast.walk(scope)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == expr.id
    ]
    if not assignments:
        return False
    return all(_resolves_to_declared(value, scope, declared) for value in assignments)


def _signal_instrument_offenders(src_root: Path, declared: set[str]) -> list[str]:
    """Every ``Signal(...)`` in ``src_root`` whose instrument is not provable."""
    offenders: list[str] = []
    for py in sorted(src_root.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        scopes: list[ast.AST] = [tree]
        scopes += [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        seen: set[int] = set()
        for scope in scopes:
            for node in ast.walk(scope):
                if not isinstance(node, ast.Call) or id(node) in seen:
                    continue
                called = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None
                )
                if called != "Signal":
                    continue
                seen.add(id(node))

                instrument: ast.expr | None = None
                for kw in node.keywords:
                    if kw.arg == "instrument":
                        instrument = kw.value
                if instrument is None and len(node.args) >= 4:
                    instrument = node.args[3]

                if not _resolves_to_declared(instrument, scope, declared):
                    offenders.append(
                        f"{py.relative_to(src_root)}:{node.lineno} — instrument "
                        f"does not provably resolve to a declared INSTRUMENT_* "
                        f"constant"
                    )
    return offenders


def test_every_signal_construction_site_in_src_names_a_declared_instrument():
    """Introduce a signal with an unverifiable sensor anywhere in src, and this FAILS.

    THE guard against the enumeration going decorative again. ``source`` was a
    free string and it had ALREADY drifted — ``_status`` and ``_health_liveness``
    were emitting ``Signal("resolver", ...)``, a source that appeared in no
    enumeration anywhere. A free-string INSTRUMENT would be far worse: the
    destruction gate COUNTS instruments, so an unrecognised one is a fresh
    witness for free.
    """
    # Arrange
    src_root = Path(scitex_agent_container.__file__).parent
    declared = set(_declared_instrument_constants())
    # Act
    offenders = _signal_instrument_offenders(src_root, declared)
    # Assert
    assert not offenders, (
        "every Signal must name a CLASSIFIED instrument constant — the gate "
        "deduplicates on it, so an unverifiable instrument could pose as an "
        "independent witness and authorise killing a healthy agent:\n  "
        + "\n  ".join(offenders)
    )


def test_the_signal_scan_actually_finds_the_construction_sites():
    """Guard the guard: a scan that silently matches NOTHING proves nothing.

    A rglob that quietly stopped finding files, or a Signal call shape the walker
    no longer recognises, would make the test above pass vacuously forever — the
    same "green because we did not look" failure the whole verdict module exists
    to prevent.
    """
    # Arrange
    src_root = Path(scitex_agent_container.__file__).parent
    # Act
    sites = sum(
        1
        for py in src_root.rglob("*.py")
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
        == "Signal"
    )
    # Assert
    assert sites >= 10, f"only {sites} Signal() sites found in src — the scan is blind"


# --------------------------------------------------------------------------
# No instrument may corroborate ITSELF.
# --------------------------------------------------------------------------


def test_each_convicting_instrument_alone_is_never_enough_to_destroy():
    """Every convicting sensor, on its own, is refused — with NO exceptions.

    Iterating the enumeration is the point (cf. scitex-todo's
    ``test_every_closure_marker_is_actually_checked``): a future instrument
    marked convicting inherits this rule automatically, instead of quietly
    becoming a one-witness kill.
    """
    # Arrange
    instruments = sorted(CONVICTING_INSTRUMENTS)
    # Act
    self_corroborating = [
        instrument
        for instrument in instruments
        if decide(
            "x",
            [
                Signal(SOURCE_PROCESS, DEAD, "first look", instrument),
                Signal(SOURCE_REGISTRY, DEAD, "second look", instrument),
            ],
        ).may_destroy
    ]
    # Assert
    assert not self_corroborating, (
        f"{self_corroborating} corroborated ITSELF into a destruction — two "
        f"reports from one sensor are one witness"
    )


def test_two_distinct_convicting_instruments_do_authorise_destruction():
    """The gate must keep its teeth.

    Guards the OPPOSITE failure: a "fix" that merely makes ``may_destroy``
    unreachable is not a fix, it is a disabled feature wearing a safety badge.
    """
    # Arrange
    signals = [
        Signal(SOURCE_PROCESS, DEAD, "tmux has no such session", INSTRUMENT_HOST_TMUX),
        Signal(SOURCE_REGISTRY, DEAD, "recorded pid reaped", INSTRUMENT_PID_NAMESPACE),
    ]
    # Act
    verdict = decide("x", signals)
    # Assert
    assert verdict.may_destroy is True


def test_the_convicting_set_is_exactly_host_tmux_and_the_pid_namespace():
    """Pin WHICH sensors may kill. Widening this set must be a deliberate act."""
    # Arrange
    expected = {INSTRUMENT_HOST_TMUX, INSTRUMENT_PID_NAMESPACE}
    # Act
    actual = set(CONVICTING_INSTRUMENTS)
    # Assert
    assert actual == expected


# --------------------------------------------------------------------------
# A sensor that cannot see death may not report one.
# --------------------------------------------------------------------------


def _accepts_a_death(instrument: str) -> bool:
    """Can a DEAD Signal be constructed on this instrument at all?"""
    try:
        Signal(SOURCE_PROCESS, DEAD, "a corpse, allegedly", instrument)
    except ValueError:
        return False
    return True


def test_no_non_convicting_instrument_can_report_a_death():
    """The delivery/heartbeat doctrine, made MECHANICAL rather than aspirational.

    "0 subscribers means DEAD" is the inference that convicted a live fleet, and
    it was previously prevented only by a resolver remembering not to write it.
    Now the type refuses — for every non-convicting sensor, by enumeration.
    """
    # Arrange
    non_convicting = sorted(INSTRUMENTS - CONVICTING_INSTRUMENTS)
    # Act
    leaky = [i for i in non_convicting if _accepts_a_death(i)]
    # Assert
    assert not leaky, (
        f"{leaky} are declared unable to observe death, yet a DEAD Signal was "
        f"constructible on them — the declaration is decorative"
    )


def test_an_unclassified_instrument_is_refused_at_construction():
    """A free string cannot sneak in as a second witness."""
    # Arrange
    undeclared = "some_new_sensor"
    # Act
    # (constructing the Signal IS the act under test — it must refuse.)
    # Assert
    with pytest.raises(ValueError, match="must be one of"):
        Signal(SOURCE_PROCESS, DEAD, "no session", undeclared)


def test_an_instrument_that_observed_nothing_may_not_claim_life():
    """NO_OBSERVATION exists so "we did not look" is TYPED, not inferred."""
    # Arrange
    instrument = INSTRUMENT_NO_OBSERVATION
    # Act
    # (constructing the Signal IS the act under test — it must refuse.)
    # Assert
    with pytest.raises(ValueError, match="may not emit"):
        Signal(SOURCE_PROCESS, ALIVE, "vibes", instrument)


def test_the_listen_broker_may_not_report_a_death():
    """The confounded signal that started all of this stays incapable of killing."""
    # Arrange
    instrument = INSTRUMENT_LISTEN_BROKER
    # Act
    # (constructing the Signal IS the act under test — it must refuse.)
    # Assert
    with pytest.raises(ValueError, match="may not emit"):
        Signal(SOURCE_DELIVERY, DEAD, "0 subscribers", instrument)
