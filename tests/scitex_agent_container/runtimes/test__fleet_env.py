"""Fleet-default env layer — precedence, data-purity and the config.yaml layer.

Mirrors ``src/scitex_agent_container/runtimes/_fleet_env.py`` (PS-204 §2).

The load-bearing property is PRECEDENCE: a fleet default must reach an agent
that says nothing, and must LOSE to an agent that says something. Both
directions are asserted, and the override direction is additionally proven at
the argv level (``test_spec_env_overrides_fleet_default_in_argv``) because argv
is what actually reaches the container — a merge that is correct in a dict and
wrong in the rendered flags would still be a broken feature.

Real YAML files on ``tmp_path`` and a real ``AgentConfig`` via ``load_config``
— no mocks (PA-306).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scitex_agent_container.runtimes._fleet_env import (
    CONFIG_SECTION,
    FLEET_DEFAULT_ENV,
    HOST_PROCESS_AGENT_NAME,
    apply_fleet_defaults_to_process,
    declared_fleet_defaults,
    effective_env,
    fleet_env_flags,
    merge_fleet_env,
)
from scitex_agent_container.runtimes._pg_identity_env import (
    PG_USER_ENV,
    derive_pg_role,
)

# The host process's expected role, composed by the SAME primitive the
# production code uses. Spelling ``ywatanabe__cli`` literally here would make
# these tests pass on the operator's laptop and fail for anyone else — and
# would quietly stop testing the composition the moment it changed.
HOST_PROCESS_ROLE = derive_pg_role(HOST_PROCESS_AGENT_NAME)


def _write_config_yaml(path: Path, mapping: dict) -> Path:
    """A real ``config.yaml`` carrying ``spec.fleet_default_env``."""
    import yaml

    path.write_text(yaml.safe_dump({"spec": {CONFIG_SECTION: mapping}}))
    return path


# ----------------------------------------------------------------------
# The data layer.
# ----------------------------------------------------------------------


def test_sac_declares_the_store_dsn_and_nothing_else() -> None:
    """Replaces test_sac_declares_no_fleet_defaults_of_its_own.

    sac declared NOTHING between 2026-07-29 and 2026-08-19, after two store
    variables were retired for stating a routing policy nothing enforced.
    SCITEX_STORE_DSN is the third store variable declared here, and the
    similarity is the reason this test pins the whole mapping rather than
    just asserting the new key is present: an EXACT match is what makes a
    fourth key someone adds casually show up as a failing test instead of as
    another line in a container's environment that nobody chose.
    """
    # Arrange
    absent = Path("/nonexistent/config.yaml")
    # Act
    defaults = declared_fleet_defaults(absent)
    # Assert
    assert defaults == {
        "SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex",
    }


def _resolved_store_locator(dsn: str | None) -> str:
    """The locator scitex-dev resolves for sac, with SCITEX_STORE_DSN set or not.

    Real ``os.environ`` with save/restore, the idiom this repo already uses
    (``test__provider_common.py``, ``test_tui_session_settings_delivery.py``)
    — NOT ``monkeypatch``. PA-306 forbids mocks here, and the point of these
    two tests is that the REAL consumer reads the REAL variable, so a patched
    environment would test the patch.

    Resolution is PURE — it computes a target, it does not connect — so this
    needs no Postgres. That is deliberate: a test that required a live
    database would SKIP in CI, and a skip that reads as a pass is the defect
    fixed in #1108.
    """
    import os

    from scitex_dev.store import host_store  # HARD import; see the note below.

    key = "SCITEX_STORE_DSN"
    saved = os.environ.get(key)
    try:
        if dsn is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = dsn
        return str(host_store(pkg="scitex_agent_container", name="state").locator)
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


#: A DSN no scitex-dev version can ever resolve to on its own: RFC 2606
#: reserves ``.invalid``, so seeing this host in a locator proves the env
#: variable was read — independent of where the zero-config default lives
#: (0.57.0: per-host socket; 0.58.1: fleet primary; next version: its call).
_SENTINEL_DSN = "postgresql://sentinel.invalid:59999/sentinel_db"


def test_the_injected_store_dsn_reaches_scitex_devs_resolver() -> None:
    """Half of the guard the two RETIRED store variables never had.

    SCITEX_CARDS_READ_BACKEND was injected into every container for months
    while nothing read it. It was not merely useless: it STATED a read policy,
    so an operator diagnosing an outage read that line, concluded the read
    target was configured, and looked elsewhere. Nothing caught it, because no
    test ever asked whether the consumer honoured the variable.

    This asks. The import of ``scitex_dev.store`` is HARD rather than
    ``importorskip`` for the same reason — scitex-dev is a [dev] dependency,
    so an ImportError is a real breakage sac must see, and importorskip on a
    dotted path is exactly the bug fixed in #1108.
    """
    # Arrange
    dsn = FLEET_DEFAULT_ENV["SCITEX_STORE_DSN"]
    # Act
    locator = _resolved_store_locator(dsn)
    # Assert
    assert "55432" in locator


def test_the_injection_is_observed_a_sentinel_moves_the_target() -> None:
    """The other half: changing the variable must MOVE the target, or it is inert.

    A test that only checked the "set" arm could pass even if scitex-dev
    resolved to 55432 for its own reasons and ignored the variable entirely —
    which is precisely the inert-but-plausible state that cost the live
    diagnosis above. So this arm must prove the variable is OBSERVED.

    THE PREVIOUS PREDICATE COMPARED THE TWO ARMS and required them to differ
    — reading "the unset arm lands elsewhere" as a fact about sac's variable.
    It is a fact about SCITEX-DEV'S DEFAULT, and the default moved. Measured:

        scitex-dev 0.57.0  unset: postgres[host=~/.scitex/pg/run ... ]  <- socket
        scitex-dev 0.58.1  unset: postgres[host=scitex-primary port=55432]

    0.58.1 adopted the fleet primary as its own zero-config default, so the
    unset arm now equals the injected fleet DSN BY UPSTREAM DESIGN, and the
    two-arm inequality failed on every host that resolves the newest release
    — reproduced 2026-09-02 in a clean venv with no ambient environment at
    all, after first being mistaken for a CI-runner env leak. Same lesson as
    the 0.56.1 incident this docstring used to describe: any predicate that
    encodes WHERE the default goes breaks when upstream moves the default.

    So inject a SENTINEL instead. ``sentinel.invalid`` can never be any
    version's default (RFC 2606 reserves .invalid), so:
      * sentinel in the injected arm's locator  => the variable is read for
        behaviour — the exact property the retired variables never had;
      * sentinel absent from the unset arm      => the helper's save/restore
        works and the sentinel did not leak into the default path.
    Neither assertion knows or cares where the default resolves, so a future
    default move cannot fail this test — only ignoring the variable can.

    Resolution stays PURE (computes a target, never connects), so the
    sentinel host is never dialed. Mutation-checked: dropping the env read
    in the resolver makes the locator show the default instead, and this
    fails. The companion leak-guard is its own test below (TQ007).
    """
    # Arrange
    dsn = _SENTINEL_DSN
    # Act
    injected = _resolved_store_locator(dsn)
    # Assert
    assert "sentinel.invalid" in injected


def test_the_sentinel_never_leaks_into_the_unset_arm() -> None:
    """Leak-guard companion to the sentinel test above.

    Proves the helper's save/restore seam actually restores: after an
    injected resolution, an unset resolution must show no trace of the
    sentinel. Deliberately says NOTHING about where the unset arm lands —
    0.57.0 answers a unix socket, 0.58.1 answers the fleet primary, and both
    are upstream's business (see the docstring above for the incident that
    taught this). Mutation-checked: making ``_resolved_store_locator`` skip
    its ``finally`` restore leaves the sentinel in ``os.environ`` and this
    fails.
    """
    # Arrange
    _resolved_store_locator(_SENTINEL_DSN)
    # Act
    unset = _resolved_store_locator(None)
    # Assert
    assert "sentinel.invalid" not in unset


# ----------------------------------------------------------------------
# The defaults reach sac's OWN process, not only its containers.
#
# The gap behind ``fe_sendauth: no password supplied`` on 2026-08-28:
# ``sac agents restart`` on compute-04 opened ``node_comms_policy`` with NO
# ``SCITEX_STORE_DSN`` in its own environment — the defaults were only ever
# rendered into ``apptainer --env`` flags — so scitex-dev's resolver fell
# through to the local UNIX socket, a streaming standby whose password no
# ``.pgpass`` row could supply. A real mapping stands in for ``os.environ``
# below (the seam exists so these tests do not touch the process they run
# in); the last two go through the real ``os.environ`` with save/restore,
# the repo idiom (see ``_resolved_store_locator``).
# ----------------------------------------------------------------------


def _bare_process_env(tmp_path: Path) -> dict[str, str]:
    """A process env that says nothing about the store, defaults applied."""
    environ: dict[str, str] = {"PATH": "/usr/bin"}
    apply_fleet_defaults_to_process(
        environ, config_path=tmp_path / "no-such-config.yaml"
    )
    return environ


def test_the_sac_process_receives_the_store_dsn_it_hands_to_containers(
    tmp_path: Path,
) -> None:
    # Arrange
    environ: dict[str, str] = {"PATH": "/usr/bin"}
    # Act
    apply_fleet_defaults_to_process(
        environ, config_path=tmp_path / "no-such-config.yaml"
    )
    # Assert
    assert environ["SCITEX_STORE_DSN"] == FLEET_DEFAULT_ENV["SCITEX_STORE_DSN"]


def test_applying_defaults_reports_exactly_the_keys_it_injected(
    tmp_path: Path,
) -> None:
    # Arrange
    environ: dict[str, str] = {"PATH": "/usr/bin"}
    # Act
    injected = apply_fleet_defaults_to_process(
        environ, config_path=tmp_path / "no-such-config.yaml"
    )
    # Assert — the declared cascade PLUS the host-side half of the pg identity
    assert injected == {**FLEET_DEFAULT_ENV, PG_USER_ENV: HOST_PROCESS_ROLE}


def test_applying_defaults_leaves_unrelated_process_keys_alone(
    tmp_path: Path,
) -> None:
    # Arrange
    # Act
    environ = _bare_process_env(tmp_path)
    # Assert
    assert environ["PATH"] == "/usr/bin"


def test_an_operator_exported_store_dsn_beats_the_fleet_default(
    tmp_path: Path,
) -> None:
    """Same rule as ``spec.env`` over the fleet layer — a default exists in
    order to be overridden. A host whose Postgres lives elsewhere exports its
    own DSN, and sac must not silently redirect that host's writes to
    ``scitex-primary``.
    """
    # Arrange
    environ = {"SCITEX_STORE_DSN": "postgresql://elsewhere:5432/mine"}
    # Act
    apply_fleet_defaults_to_process(
        environ, config_path=tmp_path / "no-such-config.yaml"
    )
    # Assert
    assert environ["SCITEX_STORE_DSN"] == "postgresql://elsewhere:5432/mine"


def test_the_config_yaml_layer_reaches_the_process_beside_a_kept_override(
    tmp_path: Path,
) -> None:
    """Precedence proven against the FULL declared set, not just sac's
    constant: the operator's config.yaml key is added, the exported DSN is
    kept, and the return value names only what was actually injected.
    """
    # Arrange
    cfg = _write_config_yaml(tmp_path / "config.yaml", {"OPERATOR_KEY": "from-yaml"})
    environ = {"SCITEX_STORE_DSN": "postgresql://elsewhere:5432/mine"}
    # Act
    injected = apply_fleet_defaults_to_process(environ, config_path=cfg)
    # Assert
    assert injected == {"OPERATOR_KEY": "from-yaml", PG_USER_ENV: HOST_PROCESS_ROLE}


def _with_store_dsn_unset(fn):
    """Run ``fn`` with the injected keys absent from the REAL os.environ.

    ``PGUSER`` is saved and cleared alongside ``SCITEX_STORE_DSN`` because the
    process-level injection now sets BOTH. Clearing it makes the two tests
    below exercise the injection rather than an inherited value (inside an
    agent container ``PGUSER`` is always already set), and restoring it stops
    a test from leaving a role name behind in the live environment it borrowed.
    """
    import os

    key = "SCITEX_STORE_DSN"
    keys = (key, PG_USER_ENV)
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        return fn(key)
    finally:
        for k, previous in saved.items():
            if previous is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = previous


def test_the_default_form_writes_to_the_live_process_environment() -> None:
    """No ``environ`` argument — the way ``cli_entry_point`` calls it."""
    import os

    # Arrange
    absent = Path("/nonexistent/config.yaml")

    def act(key: str) -> str:
        apply_fleet_defaults_to_process(config_path=absent)
        return os.environ[key]

    # Act
    seen = _with_store_dsn_unset(act)
    # Assert
    assert seen == FLEET_DEFAULT_ENV["SCITEX_STORE_DSN"]


def test_a_second_application_finds_the_key_present_and_injects_nothing() -> None:
    # Arrange
    absent = Path("/nonexistent/config.yaml")

    def act(_key: str) -> dict[str, str]:
        apply_fleet_defaults_to_process(config_path=absent)
        return apply_fleet_defaults_to_process(config_path=absent)

    # Act
    second = _with_store_dsn_unset(act)
    # Assert
    assert second == {}


# ----------------------------------------------------------------------
# ``PGUSER`` — the OTHER half of the same identity.
#
# The fleet's DSN is roleless on purpose and the login travels separately, so
# a process holding only the DSN can reach the right server and still not say
# who it is. libpq then falls back to the OS user, and ``.pgpass`` matches on
# (host, port, database, USER) — compute-04's 522 rows contain no entry for
# the bare OS user, so that fallback cannot authenticate at all:
# ``fe_sendauth: no password supplied``. Containers were never exposed to this
# because ``_pg_identity_env`` gives each one ``<host_user>__<agent>``; the
# host-side process had no equivalent until it got ``<host_user>__cli``.
# ----------------------------------------------------------------------


def test_a_bare_process_env_gains_both_halves_of_the_pg_identity(
    tmp_path: Path,
) -> None:
    """One assert on the PAIR, because either half alone cannot connect."""
    # Arrange
    environ: dict[str, str] = {"PATH": "/usr/bin"}
    # Act
    apply_fleet_defaults_to_process(
        environ, config_path=tmp_path / "no-such-config.yaml"
    )
    # Assert
    assert (environ["SCITEX_STORE_DSN"], environ[PG_USER_ENV]) == (
        FLEET_DEFAULT_ENV["SCITEX_STORE_DSN"],
        HOST_PROCESS_ROLE,
    )


def test_the_injected_role_is_composed_by_the_shared_primitive(
    tmp_path: Path,
) -> None:
    """SSOT: ``<host_user>__<name>`` is built in exactly one place.

    ``derive_pg_role`` is what every container's ``PGUSER`` already comes
    from, so asserting against it — rather than against a literal — is what
    keeps a second copy of the string logic from appearing here later.
    """
    # Arrange
    # Act
    environ = _bare_process_env(tmp_path)
    # Assert
    assert environ[PG_USER_ENV] == derive_pg_role(HOST_PROCESS_AGENT_NAME)


def test_a_process_that_already_declares_a_role_keeps_it_verbatim(
    tmp_path: Path,
) -> None:
    """Declared-anywhere wins, same rule as the DSN above.

    An operator debugging as another role, or a wrapper that already resolved
    an identity, must not have it silently swapped for ``__cli`` — a
    connection made under the wrong login is worse than one that fails.
    """
    # Arrange
    environ = {PG_USER_ENV: "ywatanabe__deliberately-someone-else"}
    # Act
    apply_fleet_defaults_to_process(
        environ, config_path=tmp_path / "no-such-config.yaml"
    )
    # Assert
    assert environ[PG_USER_ENV] == "ywatanabe__deliberately-someone-else"


def test_a_config_yaml_role_beats_the_host_process_default(tmp_path: Path) -> None:
    """The third declaring layer: the operator's ``spec.fleet_default_env``.

    The injection is checked AFTER the config cascade precisely so this layer
    is covered by the same lookup, rather than by a second special case that
    could disagree with it.
    """
    # Arrange
    cfg = _write_config_yaml(
        tmp_path / "config.yaml", {PG_USER_ENV: "ywatanabe__from-yaml"}
    )
    environ: dict[str, str] = {"PATH": "/usr/bin"}
    # Act
    apply_fleet_defaults_to_process(environ, config_path=cfg)
    # Assert
    assert environ[PG_USER_ENV] == "ywatanabe__from-yaml"


def test_the_injected_dsn_names_no_role_of_its_own(tmp_path: Path) -> None:
    """The DSN must stay ROLELESS — the guard on a 132-way identity collapse.

    Putting the role in the DSN would "fix" the host process in one line, and
    :data:`FLEET_DEFAULT_ENV` is the CONTAINERS' baseline too: apptainer would
    hand that userinfo to all 132 agents, where libpq prefers it over each
    agent's own ``PGUSER``, and every distinct per-agent login would become
    one shared role. Userinfo lives before an ``@`` in the authority, so that
    is what this looks at — not the whole string, which legitimately contains
    ``:`` and ``/``.
    """
    # Arrange
    environ = _bare_process_env(tmp_path)
    # Act
    authority = environ["SCITEX_STORE_DSN"].split("://", 1)[1].split("/", 1)[0]
    # Assert
    assert "@" not in authority


def test_the_host_process_role_never_reaches_a_container(tmp_path: Path) -> None:
    """The other side of the same guard, at the layer that renders containers.

    ``cli`` is injected into the PROCESS, never into the declared defaults, so
    an agent still launches as itself. If someone ever "simplifies" this by
    adding ``PGUSER`` to :data:`FLEET_DEFAULT_ENV`, that key would win over
    ``_pg_identity_env``'s per-agent injection (declared-anywhere-wins cuts
    both ways) and this goes red.
    """
    # Arrange
    config = SimpleNamespace(name="some-agent", env={}, apptainer=None)
    # Act
    env = effective_env(config, defaults=FLEET_DEFAULT_ENV)
    # Assert
    assert env[PG_USER_ENV] == derive_pg_role("some-agent")



def test_declared_defaults_do_not_mutate_the_module_constant(tmp_path: Path) -> None:
    """A caller mutating the result must not poison the next agent's env.

    The mechanism under test is that the returned dict is a COPY. It previously
    probed that via the seeded read-backend key; with sac declaring nothing, the
    probe is an operator override instead. The coverage is unchanged — only the
    key it mutates.
    """
    # Arrange
    cfg = _write_config_yaml(tmp_path / "config.yaml", {"ADDED": "x"})
    # Act
    declared_fleet_defaults(cfg)["ADDED"] = "MUTATED"
    # Assert — the module constant is untouched by a caller's mutation
    assert "ADDED" not in FLEET_DEFAULT_ENV


def test_declared_defaults_return_a_fresh_dict_each_call(tmp_path: Path) -> None:
    """Second half of the copy guarantee: two calls must not share a dict.

    Added because the original mutation test asserted against a CONSTANT that is
    now empty, which would pass even if the function returned the constant
    itself. This asserts the property directly rather than through a key.
    """
    # Arrange
    cfg = _write_config_yaml(tmp_path / "config.yaml", {"ADDED": "x"})
    # Act
    first = declared_fleet_defaults(cfg)
    first["ADDED"] = "MUTATED"
    second = declared_fleet_defaults(cfg)
    # Assert
    assert second["ADDED"] == "x"


# ----------------------------------------------------------------------
# Layer 2 — the operator's config.yaml.
# ----------------------------------------------------------------------


def test_config_yaml_can_add_a_new_fleet_default(tmp_path: Path) -> None:
    # Arrange
    cfg = _write_config_yaml(tmp_path / "config.yaml", {"OPERATOR_KEY": "yes"})
    # Act
    defaults = declared_fleet_defaults(cfg)
    # Assert
    assert defaults["OPERATOR_KEY"] == "yes"


def test_config_yaml_overrides_a_sac_declared_default(tmp_path: Path) -> None:
    # Arrange
    cfg = _write_config_yaml(
        tmp_path / "config.yaml", {"SCITEX_CARDS_READ_BACKEND": "yaml"}
    )
    # Act
    defaults = declared_fleet_defaults(cfg)
    # Assert
    assert defaults["SCITEX_CARDS_READ_BACKEND"] == "yaml"


def test_config_yaml_values_are_coerced_to_strings(tmp_path: Path) -> None:
    """A YAML ``true`` must render as a well-formed --env value, not a repr."""
    # Arrange
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"spec:\n  {CONFIG_SECTION}:\n    FLAG: true\n    COUNT: 3\n")
    # Act
    defaults = declared_fleet_defaults(cfg)
    # Assert
    assert (defaults["FLAG"], defaults["COUNT"]) == ("True", "3")


def test_malformed_config_yaml_degrades_to_sac_defaults(tmp_path: Path) -> None:
    """An operator typo must not stop the fleet from launching.

    Previously asserted the seeded read-backend key survived the fallback. With
    sac declaring nothing that assertion would be vacuous — `{} == {}` passes
    even if parsing silently succeeded. So the malformed file now also CONTAINS
    a would-be override, and the assertion is that the override did NOT take
    effect: proof the parse actually failed and the fallback actually ran,
    rather than a shape that cannot come out the other way.
    """
    # Arrange — malformed, and carrying an override that must not be honoured.
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"spec: [this is not: a mapping\n  {CONFIG_SECTION}:\n    SHOULD_NOT_APPEAR: y\n"
    )
    # Act
    defaults = declared_fleet_defaults(cfg)
    # Assert
    assert "SHOULD_NOT_APPEAR" not in defaults


def test_non_mapping_section_is_ignored(tmp_path: Path) -> None:
    # Arrange
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"spec:\n  {CONFIG_SECTION}:\n    - not\n    - a mapping\n")
    # Act
    defaults = declared_fleet_defaults(cfg)
    # Assert
    assert defaults == dict(FLEET_DEFAULT_ENV)


# ----------------------------------------------------------------------
# THE precedence rule: spec.env wins.
# ----------------------------------------------------------------------


def test_fleet_default_reaches_an_agent_that_declares_nothing() -> None:
    # Arrange
    defaults = {"FLEET_ONLY": "from-fleet"}
    # Act
    merged = merge_fleet_env({}, defaults=defaults)
    # Assert
    assert merged["FLEET_ONLY"] == "from-fleet"


def test_spec_env_overrides_a_fleet_default_of_the_same_name() -> None:
    """THE precedence rule — per-agent beats fleet default."""
    # Arrange
    defaults = {"SHARED_KEY": "from-fleet"}
    # Act
    merged = merge_fleet_env({"SHARED_KEY": "from-spec"}, defaults=defaults)
    # Assert
    assert merged["SHARED_KEY"] == "from-spec"


def test_spec_env_can_neutralise_a_fleet_default_with_an_empty_value() -> None:
    """The documented per-agent opt-out: same key, empty value."""
    # Arrange
    defaults = {"SHARED_KEY": "from-fleet"}
    # Act
    merged = merge_fleet_env({"SHARED_KEY": ""}, defaults=defaults)
    # Assert
    assert merged["SHARED_KEY"] == ""


def test_disjoint_spec_and_fleet_keys_both_survive() -> None:
    # Arrange
    defaults = {"FLEET_KEY": "f"}
    # Act
    merged = merge_fleet_env({"SPEC_KEY": "s"}, defaults=defaults)
    # Assert
    assert (merged["FLEET_KEY"], merged["SPEC_KEY"]) == ("f", "s")


def test_a_same_key_collision_does_not_raise() -> None:
    """Unlike the to_home cascade, a default exists in order to be overridden."""
    # Arrange
    defaults = {"SHARED_KEY": "from-fleet"}
    # Act
    merged = merge_fleet_env({"SHARED_KEY": "other"}, defaults=defaults)
    # Assert
    assert merged["SHARED_KEY"] == "other"


def test_merge_does_not_mutate_the_supplied_defaults() -> None:
    # Arrange
    defaults = {"SHARED_KEY": "from-fleet"}
    # Act
    merge_fleet_env({"SHARED_KEY": "from-spec"}, defaults=defaults)
    # Assert
    assert defaults["SHARED_KEY"] == "from-fleet"


def test_merge_is_idempotent() -> None:
    # Arrange
    defaults = {"A": "1"}
    once = merge_fleet_env({"B": "2"}, defaults=defaults)
    # Act
    twice = merge_fleet_env(once, defaults=defaults)
    # Assert
    assert twice == once


def test_none_spec_env_yields_the_fleet_defaults() -> None:
    """``spec.env`` is optional; a spec without one still gets the defaults."""
    # Arrange
    defaults = {"FLEET_ONLY": "v"}
    # Act
    merged = merge_fleet_env(None, defaults=defaults)
    # Assert
    assert merged == {"FLEET_ONLY": "v"}


# ----------------------------------------------------------------------
# The build_run_argv entry-points.
# ----------------------------------------------------------------------


def test_effective_env_reads_config_env() -> None:
    # Arrange
    config = SimpleNamespace(env={"SHARED_KEY": "from-spec"})
    # Act
    merged = effective_env(config, defaults={"SHARED_KEY": "from-fleet"})
    # Assert
    assert merged["SHARED_KEY"] == "from-spec"


def test_effective_env_tolerates_a_config_without_env() -> None:
    # Arrange
    config = SimpleNamespace()
    # Act
    merged = effective_env(config, defaults={"FLEET_ONLY": "v"})
    # Assert
    assert merged == {"FLEET_ONLY": "v"}


def test_fleet_env_flags_render_apptainer_env_pairs() -> None:
    # Arrange
    config = SimpleNamespace(env={})
    # Act
    flags = fleet_env_flags(config, defaults={"K": "V"})
    # Assert
    assert flags == ["--env", "K=V"]


@pytest.mark.parametrize("value", ["with space", "a=b", ""])
def test_flag_value_is_rendered_verbatim(value: str) -> None:
    """--env values are passed as a single argv element, never re-quoted."""
    # Arrange
    config = SimpleNamespace(env={"K": value})
    # Act
    flags = fleet_env_flags(config, defaults={})
    # Assert
    assert flags[1] == f"K={value}"


# ----------------------------------------------------------------------
# Board identity — BOTH names for the transition window, and the loud
# validator that refuses an unexpanded ``${VAR}`` (INCIDENT 2026-07-19:
# seven cards stored ``created_by='${SCITEX_CARDS_AGENT_ID}'``).
# ----------------------------------------------------------------------


def test_starting_an_agent_exports_the_current_board_identity_name() -> None:
    """scitex-cards reads SCITEX_CARDS_AGENT_ID; sac injected only the old name."""
    # Arrange
    config = SimpleNamespace(env={"SCITEX_TODO_AGENT_ID": "scitex-agent-container"})
    # Act
    merged = effective_env(config, defaults={})
    # Assert
    assert merged["SCITEX_CARDS_AGENT_ID"] == "scitex-agent-container"


def test_starting_an_agent_still_exports_the_legacy_board_identity_name() -> None:
    """Both, not a swap — installed scitex-cards versions differ across the fleet."""
    # Arrange
    config = SimpleNamespace(env={"SCITEX_TODO_AGENT_ID": "scitex-agent-container"})
    # Act
    merged = effective_env(config, defaults={})
    # Assert
    assert merged["SCITEX_TODO_AGENT_ID"] == "scitex-agent-container"


def _rejection_message(config: SimpleNamespace) -> str:
    """The error ``effective_env`` raises for ``config``, or ``""`` if it did not.

    Keeps the rejection tests at ONE assertion each (STX-TQ007 counts a
    ``pytest.raises`` block as an assertion, so pairing it with an assert on
    the message would be two).
    """
    try:
        effective_env(config, defaults={})
    except ValueError as exc:
        return str(exc)
    return ""


def test_an_unexpanded_substitution_value_is_rejected_loudly() -> None:
    """A ``${VAR}`` that never expanded is a non-answer; it must never be stored."""
    # Arrange
    config = SimpleNamespace(env={"SCITEX_CARDS_AGENT_ID": "${SCITEX_CARDS_AGENT_ID}"})
    # Act
    message = _rejection_message(config)
    # Assert
    assert "SCITEX_CARDS_AGENT_ID" in message


def test_the_rejection_error_quotes_the_offending_value() -> None:
    # Arrange
    config = SimpleNamespace(env={"ANY_KEY": "${SOMETHING}"})
    # Act
    message = _rejection_message(config)
    # Assert
    assert "${SOMETHING}" in message


def test_a_normal_board_identity_value_passes_through_unchanged() -> None:
    """CONTROL — the validator must reject non-answers, not everything."""
    # Arrange
    config = SimpleNamespace(env={"SCITEX_TODO_AGENT_ID": "scitex-agent-container"})
    # Act
    merged = effective_env(config, defaults={})
    # Assert
    assert merged["SCITEX_TODO_AGENT_ID"] == "scitex-agent-container"


def test_a_normal_unrelated_value_passes_through_unchanged() -> None:
    """CONTROL — an ordinary value with no ``${`` is untouched."""
    # Arrange
    config = SimpleNamespace(env={"PLAIN": "scitex-agent-container"})
    # Act
    merged = effective_env(config, defaults={})
    # Assert
    assert merged["PLAIN"] == "scitex-agent-container"


def test_raw_args_declared_identity_is_mirrored_to_the_current_name() -> None:
    """Most specs declare the identity ONLY in raw_args, never in spec.env."""
    # Arrange
    config = SimpleNamespace(
        env={},
        apptainer=SimpleNamespace(
            raw_args=["--env", "SCITEX_TODO_AGENT_ID=scitex-dev"]
        ),
    )
    # Act
    merged = effective_env(config, defaults={})
    # Assert
    assert merged["SCITEX_CARDS_AGENT_ID"] == "scitex-dev"


def test_raw_args_identity_wins_over_the_spec_env_identity() -> None:
    """apptainer --env is last-wins and raw_args are appended AFTER spec.env."""
    # Arrange
    config = SimpleNamespace(
        env={"SCITEX_TODO_AGENT_ID": "from-spec-env"},
        apptainer=SimpleNamespace(
            raw_args=["--env", "SCITEX_TODO_AGENT_ID=from-raw-args"]
        ),
    )
    # Act
    merged = effective_env(config, defaults={})
    # Assert
    assert merged["SCITEX_CARDS_AGENT_ID"] == "from-raw-args"


# ----------------------------------------------------------------------
# Dual-write must stay GONE. The YAML tier it gated was deleted 2026-07-21;
# after that the flag routed nothing while still reaching every container,
# and scitex-cards' health FAILED single_write_target purely on its presence
# (a false alarm nobody could clear). Dropped 2026-07-28 on the store owner's
# decision. These assert the absence at BOTH layers, because a key removed
# from the dict but still rendered into argv would be the same bug.
# ----------------------------------------------------------------------

DEAD_WRITE_ROUTING_KEYS = ("SCITEX_CARDS_DUAL_WRITE", "SCITEX_TODO_DUAL_WRITE")


@pytest.mark.parametrize("key", DEAD_WRITE_ROUTING_KEYS)
def test_dead_write_routing_key_is_not_a_fleet_default(key: str) -> None:
    """sac must not declare a write-routing flag for a store tier that is gone."""
    # Arrange
    absent = Path("/nonexistent/config.yaml")
    # Act
    defaults = declared_fleet_defaults(absent)
    # Assert
    assert key not in defaults and key not in FLEET_DEFAULT_ENV


@pytest.mark.parametrize("key", DEAD_WRITE_ROUTING_KEYS)
def test_dead_write_routing_key_never_reaches_argv(key: str) -> None:
    """argv is what actually reaches the container, so assert it there too."""
    # Arrange
    config = SimpleNamespace(env={})
    # Act
    flags = fleet_env_flags(config, defaults=FLEET_DEFAULT_ENV)
    # Assert
    assert not any(flag.startswith(f"{key}=") for flag in flags)


DEAD_READ_ROUTING_KEYS = ("SCITEX_CARDS_READ_BACKEND", "SCITEX_TODO_READ_BACKEND")


@pytest.mark.parametrize("key", DEAD_READ_ROUTING_KEYS)
def test_dead_read_routing_key_is_not_a_fleet_default(key: str) -> None:
    """sac must not declare a read-routing flag that nothing reads.

    This INVERTS ``test_read_backend_default_is_retained``, which asserted the
    retired-engine read pin "stays". That test was written when the pin was
    believed to
    mean something. It does not: scitex-cards searched their read path from
    source (positive control first) and found the variable only in a comment and
    a retired-vars key — never read for behaviour. The old test pinned a policy
    statement that was never true.

    Dropped 2026-07-29 on the store owner's explicit ruling, same standard as
    DEAD_WRITE_ROUTING_KEYS above. Do NOT reintroduce without a new ruling.
    """
    # Arrange
    absent = Path("/nonexistent/config.yaml")
    # Act
    defaults = declared_fleet_defaults(absent)
    # Assert
    assert key not in defaults and key not in FLEET_DEFAULT_ENV


@pytest.mark.parametrize("key", DEAD_READ_ROUTING_KEYS)
def test_dead_read_routing_key_never_reaches_argv(key: str) -> None:
    """argv is what actually reaches the container, so assert it there too."""
    # Arrange
    config = SimpleNamespace(env={})
    # Act
    flags = fleet_env_flags(config, defaults=FLEET_DEFAULT_ENV)
    # Assert
    assert not any(flag.startswith(f"{key}=") for flag in flags)


def test_an_empty_fleet_default_env_is_a_valid_state() -> None:
    """Removing the last default must not break the mechanism.

    FLEET_DEFAULT_ENV is now empty. The cascade still exists for operator
    overrides via config.yaml's ``fleet_default_env``, so this asserts the
    empty case yields no flags rather than raising or misbehaving.
    """
    # Arrange
    config = SimpleNamespace(env={})
    # Act
    flags = fleet_env_flags(config, defaults={})
    # Assert
    assert flags == []


def test_effective_env_gives_an_unlabelled_spec_its_own_name() -> None:
    """The end-to-end path proj-scitex-hub fell through on 2026-08-20.

    The unit test above covers the alias function; this covers the WIRING —
    that ``effective_env`` actually hands the agent's name down. Drop the
    ``agent_name=`` argument at the call site and the unit tests stay green
    while this one goes red, which is the point of having both.
    """
    # Arrange
    config = SimpleNamespace(name="proj-scitex-hub", env={})
    # Act
    env = effective_env(config)
    # Assert
    assert env["SCITEX_CARDS_AGENT_ID"] == "proj-scitex-hub"
