"""One ``--env KEY`` per key, and no scitex DSN on the banned port 5432.

Mirrors ``src/scitex_agent_container/runtimes/_apptainer_env_dedup.py``
(PS-204 §2).

Two properties, both measured as FAULTS on scitex-compute-04 (2026-08-11)
before they were properties at all:

* the built argv declared ``SCITEX_CARDS_DB`` TWICE — ``:5432`` from the
  operator's ``config.yaml`` ``spec.fleet_default_env`` and ``:55432``
  from ``spec.apptainer.raw_args`` — and reached the right database only
  because apptainer's ``--env`` is last-wins; and
* ``:5432`` is a port ADR-0022 rules out for every scitex service on
  every host, which until now was written down but never checked.

Both are asserted at the ARGV level as well as the unit level, because
argv is what actually reaches the container — a rule that holds in a
helper and not in the rendered flags would not be a rule.

No mocks / no monkeypatch (PA-306/307). Real ``load_config`` on a tmp
spec for the end-to-end cases; direct argv lists for the unit cases.
AAA blocks, markers on their own line, one assert per test.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._apptainer_build_argv import build_run_argv
from scitex_agent_container.runtimes._apptainer_env_dedup import (
    CANONICAL_SCITEX_PG_PORT,
    FORBIDDEN_SCITEX_PG_PORT,
    ForbiddenScitexDsnError,
    assert_no_forbidden_scitex_dsn,
    collapse_duplicate_env,
    env_pair_at,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

CANONICAL_DSN = (
    f"postgresql://scitex_cards@127.0.0.1:{CANONICAL_SCITEX_PG_PORT}/scitex_cards"
)
BANNED_DSN = (
    f"postgresql://scitex_cards@127.0.0.1:{FORBIDDEN_SCITEX_PG_PORT}/scitex_cards"
)

# ---------------------------------------------------------------------------
# Fixtures + helpers (mirror test__apptainer_argv_guard.py).
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolate_home(tmp_path: Path) -> Iterator[Path]:
    # Arrange-side fixture: keep the bearer-token resolver off the real ~.
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


@pytest.fixture
def listen_bearer_token(_isolate_home: Path) -> Path:
    # Materialise a real listen bearer so build_run_argv's listen-env
    # guard doesn't turn an argv-shape test into a RuntimeError.
    token_dir = _isolate_home / ".scitex" / "agent-container" / "tokens"
    token_dir.mkdir(parents=True, exist_ok=True)
    token_path = token_dir / f"listen-{socket.gethostname()}.token"
    token_path.write_text("test-bearer-token-not-a-secret\n", encoding="utf-8")
    return token_path


def _write_spec(tmp_path: Path, *, spec_env: str = "", raw_args: str = "") -> Path:
    """A real v3 spec whose env layer and ``raw_args`` may both name a key.

    ``spec_env`` is the ``spec.apptainer.env`` block (v3 realignment §3
    moved it under ``apptainer``); ``raw_args`` the escape hatch appended
    verbatim after every curated flag. Both are rendered at their real
    indent so the two layers collide exactly as they do in the fleet.
    """
    spec_dir = tmp_path / "agents" / "agt"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / "spec.yaml"
    spec.write_text(
        explicitize_yaml(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "metadata:\n"
            "  labels:\n"
            "    project: t\n"
            '    sac-builtin: "off"\n'
            "spec:\n"
            "  runtime: tui\n"
            "  host: ${HOSTNAME}\n"
            "  workdir: /tmp/agt-work\n"
            "  apptainer:\n"
            "    image: /x.sif\n"
            "    fakeroot: true\n"
            "    binds: []\n"
            f"{spec_env}"
            f"{raw_args}"
            "  health:\n"
            "    enabled: true\n"
            "    interval: 60\n"
            "  restart:\n"
            "    policy: on-failure\n"
            "    max_retries: 3\n"
            "  claude:\n"
            "    model: claude-opus-4-8[1m]\n"
        ),
        encoding="utf-8",
    )
    return spec


def _build(tmp_path: Path, spec: Path) -> list[str]:
    return build_run_argv(
        load_config(str(spec)),
        state_dir=tmp_path / "st",
        sif_path=tmp_path / "x.sif",
        tui=True,
    )


def env_values(argv: list[str], key: str) -> list[str]:
    """Every value ``argv`` declares for ``--env KEY``, in argv order.

    Reads both spellings that occur in real specs — split
    (``--env K=V``) and glued (``--env=K=V``) — so a duplicate hidden
    behind the other spelling still shows up here.
    """
    found: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--env" and index + 1 < len(argv):
            pair, width = argv[index + 1], 2
        elif token.startswith("--env="):
            pair, width = token[len("--env=") :], 1
        else:
            index += 1
            continue
        name, sep, value = pair.partition("=")
        if sep and name == key:
            found.append(value)
        index += width
    return found


def env_keys(argv: list[str]) -> list[str]:
    """Every ``--env`` key the argv declares, with duplicates preserved."""
    keys: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--env" and index + 1 < len(argv):
            pair, width = argv[index + 1], 2
        elif token.startswith("--env="):
            pair, width = token[len("--env=") :], 1
        else:
            index += 1
            continue
        name, sep, _value = pair.partition("=")
        if sep and name:
            keys.append(name)
        index += width
    return keys


# ---------------------------------------------------------------------------
# collapse_duplicate_env — unit cases (direct argv lists).
# ---------------------------------------------------------------------------


def test_a_key_declared_twice_survives_exactly_once() -> None:
    # Arrange — the shape the fleet actually ran on.
    argv = ["--env", "SCITEX_CARDS_DB=a", "--env", "SCITEX_CARDS_DB=b"]
    # Act
    collapsed = collapse_duplicate_env(argv)
    # Assert
    assert env_values(collapsed, "SCITEX_CARDS_DB") == ["b"]


def test_the_surviving_value_is_the_one_apptainer_would_have_resolved() -> None:
    # Arrange — last-wins is preserved deliberately: collapsing to the
    # FIRST would repoint every agent's store, which is the accident this
    # module exists to prevent, not to cause.
    argv = ["--env", "K=first", "--env", "K=middle", "--env", "K=last"]
    # Act
    collapsed = collapse_duplicate_env(argv)
    # Assert
    assert env_values(collapsed, "K") == ["last"]


def test_the_glued_spelling_is_the_same_key_as_the_split_one() -> None:
    # Arrange — two specs in this fleet use ``--env=K=V``.
    argv = ["--env", "K=split", "--env=K=glued"]
    # Act
    collapsed = collapse_duplicate_env(argv)
    # Assert
    assert env_values(collapsed, "K") == ["glued"]


def test_distinct_keys_are_all_kept() -> None:
    # Arrange — dedup is per-key, not a general thinning pass.
    argv = ["--env", "A=1", "--env", "B=2", "--env", "C=3"]
    # Act
    collapsed = collapse_duplicate_env(argv)
    # Assert
    assert collapsed == argv


def test_non_env_tokens_keep_their_positions() -> None:
    # Arrange — binds and overlays carry their own ordering invariants.
    argv = ["--bind", "/a:/a", "--env", "K=1", "--overlay", "/o", "--env", "K=2"]
    # Act
    collapsed = collapse_duplicate_env(argv)
    # Assert
    assert collapsed == ["--bind", "/a:/a", "--overlay", "/o", "--env", "K=2"]


def test_env_file_is_not_an_env_pair() -> None:
    # Arrange — ``--env-file`` shares a prefix with ``--env``.
    argv = ["--env-file", "/home/agent/.env", "--env", "K=1"]
    # Act
    collapsed = collapse_duplicate_env(argv)
    # Assert
    assert collapsed == argv


def test_the_input_list_is_not_mutated() -> None:
    # Arrange — callers reuse the list they passed in.
    argv = ["--env", "K=1", "--env", "K=2"]
    # Act
    collapse_duplicate_env(argv)
    # Assert
    assert argv == ["--env", "K=1", "--env", "K=2"]


def test_a_malformed_orphan_env_is_left_for_the_flag_guard() -> None:
    # Arrange — a trailing bare ``--env`` is _apptainer_argv_guard's to
    # report; swallowing it here would delete its evidence.
    argv = ["--env", "K=1", "--env"]
    # Act
    collapsed = collapse_duplicate_env(argv)
    # Assert
    assert collapsed[-1] == "--env"


# ---------------------------------------------------------------------------
# assert_no_forbidden_scitex_dsn — the operator's port rule.
# ---------------------------------------------------------------------------


def test_a_scitex_dsn_on_the_banned_port_is_refused() -> None:
    # Arrange — ADR-0022: port 5432 is never used for scitex, on any host.
    argv = ["--env", f"SCITEX_CARDS_DB={BANNED_DSN}"]
    # Act
    check = lambda: assert_no_forbidden_scitex_dsn(argv)
    # Assert
    with pytest.raises(ForbiddenScitexDsnError):
        check()


def test_the_refusal_names_the_offending_variable() -> None:
    # Arrange — a reader must learn WHICH variable to open.
    argv = ["--env", f"SCITEX_CARDS_DB={BANNED_DSN}"]
    message = ""
    try:
        assert_no_forbidden_scitex_dsn(argv)
    except ForbiddenScitexDsnError as exc:
        message = str(exc)
    # Act
    names_variable = "SCITEX_CARDS_DB" in message
    # Assert
    assert names_variable is True


def test_the_refusal_names_the_canonical_port() -> None:
    # Arrange — naming the fault without naming the fix wastes the reader.
    argv = ["--env", f"SCITEX_CARDS_DB={BANNED_DSN}"]
    message = ""
    try:
        assert_no_forbidden_scitex_dsn(argv)
    except ForbiddenScitexDsnError as exc:
        message = str(exc)
    # Act
    names_canonical = str(CANONICAL_SCITEX_PG_PORT) in message
    # Assert
    assert names_canonical is True


def test_the_refusal_never_prints_the_dsn_itself() -> None:
    # Arrange — a DSN can carry a password and this message reaches logs.
    secret = f"postgresql://scitex_cards:hunter2@127.0.0.1:{FORBIDDEN_SCITEX_PG_PORT}/scitex_cards"
    argv = ["--env", f"SCITEX_CARDS_DB={secret}"]
    message = ""
    try:
        assert_no_forbidden_scitex_dsn(argv)
    except ForbiddenScitexDsnError as exc:
        message = str(exc)
    # Act
    leaked = "hunter2" in message
    # Assert
    assert leaked is False


def test_a_scitex_dsn_with_no_port_is_refused_too() -> None:
    # Arrange — an omitted port IS 5432, the most invisible way to hit it.
    argv = ["--env", "SCITEX_CARDS_DB=postgresql://scitex_cards@127.0.0.1/scitex_cards"]
    # Act
    check = lambda: assert_no_forbidden_scitex_dsn(argv)
    # Assert
    with pytest.raises(ForbiddenScitexDsnError):
        check()


def test_the_canonical_port_is_accepted() -> None:
    # Arrange — the whole fleet runs on 55432.
    argv = ["--env", f"SCITEX_CARDS_DB={CANONICAL_DSN}"]
    # Act
    result = assert_no_forbidden_scitex_dsn(argv)
    # Assert — a no-op returns None (did not raise).
    assert result is None


def test_a_sqlite_store_path_is_not_a_dsn() -> None:
    # Arrange — ``SCITEX_CARDS_DB`` is a filesystem path on SQLite.
    argv = ["--env", "SCITEX_CARDS_DB=/home/agent/.scitex/cards/cards.db"]
    # Act
    result = assert_no_forbidden_scitex_dsn(argv)
    # Assert
    assert result is None


def test_a_non_scitex_postgres_on_5432_is_none_of_our_business() -> None:
    # Arrange — the rule is about scitex services, not about the port.
    argv = ["--env", "OTHER_DB=postgresql://someone@127.0.0.1:5432/otherapp"]
    # Act
    result = assert_no_forbidden_scitex_dsn(argv)
    # Assert
    assert result is None


def test_a_scitex_database_name_is_enough_to_claim_the_dsn() -> None:
    # Arrange — the rule follows the SERVICE, not the variable's spelling.
    argv = ["--env", "BOARD_URL=postgresql://someone@127.0.0.1:5432/scitex_cards"]
    # Act
    check = lambda: assert_no_forbidden_scitex_dsn(argv)
    # Assert
    with pytest.raises(ForbiddenScitexDsnError):
        check()


# ---------------------------------------------------------------------------
# build_run_argv — end-to-end (what actually reaches the container).
# ---------------------------------------------------------------------------


def test_the_built_argv_declares_the_cards_dsn_exactly_once(
    tmp_path: Path, listen_bearer_token: Path
) -> None:
    # Arrange — spec.env AND raw_args both name SCITEX_CARDS_DB, the exact
    # shape measured on scitex-compute-04.
    spec = _write_spec(
        tmp_path,
        spec_env=f"    env:\n      SCITEX_CARDS_DB: {CANONICAL_DSN}\n",
        raw_args=f"    raw_args:\n      - --env\n      - SCITEX_CARDS_DB={CANONICAL_DSN}\n",
    )
    # Act
    argv = _build(tmp_path, spec)
    # Assert
    assert len(env_values(argv, "SCITEX_CARDS_DB")) == 1


def test_no_key_at_all_is_declared_twice_in_a_built_argv(
    tmp_path: Path, listen_bearer_token: Path
) -> None:
    # Arrange — the fix is for the CLASS of bug, not for one key.
    spec = _write_spec(
        tmp_path,
        spec_env=(
            "    env:\n"
            f"      SCITEX_CARDS_DB: {CANONICAL_DSN}\n"
            "      SAC_PROBE: from-spec\n"
        ),
        raw_args=(
            "    raw_args:\n"
            "      - --env\n"
            f"      - SCITEX_CARDS_DB={CANONICAL_DSN}\n"
            "      - --env\n"
            "      - SAC_PROBE=from-raw\n"
        ),
    )
    argv = _build(tmp_path, spec)
    # Act
    keys = env_keys(argv)
    # Assert
    assert len(keys) == len(set(keys))


def test_the_raw_args_value_is_the_one_that_survives(
    tmp_path: Path, listen_bearer_token: Path
) -> None:
    # Arrange — behaviour preservation: raw_args already won via last-wins.
    spec = _write_spec(
        tmp_path,
        spec_env="    env:\n      SCITEX_CARDS_DB: /home/agent/from-spec-env.db\n",
        raw_args=f"    raw_args:\n      - --env\n      - SCITEX_CARDS_DB={CANONICAL_DSN}\n",
    )
    # Act
    argv = _build(tmp_path, spec)
    # Assert
    assert env_values(argv, "SCITEX_CARDS_DB") == [CANONICAL_DSN]


def test_a_built_argv_never_carries_a_scitex_dsn_on_5432(
    tmp_path: Path, listen_bearer_token: Path
) -> None:
    # Arrange — ``:5432`` reached the fleet through a layer nobody re-read.
    # Rendering it must fail the launch, not hand a container an address
    # no peer reads.
    spec = _write_spec(
        tmp_path,
        spec_env=f"    env:\n      SCITEX_CARDS_DB: {BANNED_DSN}\n",
    )
    # Act
    build = lambda: _build(tmp_path, spec)
    # Assert
    with pytest.raises(ForbiddenScitexDsnError):
        build()


def test_a_banned_dsn_overridden_by_raw_args_still_launches(
    tmp_path: Path, listen_bearer_token: Path
) -> None:
    # Arrange — the live fleet's shape: a stale ``:5432`` default that
    # every healthy agent already overrides. The bad value never reaches
    # the container, so refusing here would ground 90 working agents.
    spec = _write_spec(
        tmp_path,
        spec_env=f"    env:\n      SCITEX_CARDS_DB: {BANNED_DSN}\n",
        raw_args=f"    raw_args:\n      - --env\n      - SCITEX_CARDS_DB={CANONICAL_DSN}\n",
    )
    # Act
    argv = _build(tmp_path, spec)
    # Assert
    assert env_values(argv, "SCITEX_CARDS_DB") == [CANONICAL_DSN]


# ---------------------------------------------------------------------------
# ``env_pair_at`` is THE shared recogniser — public for that reason
#
# WHY THESE EXIST. This module and ``_apptainer_secret_env`` both walk the
# same argv asking "is this an --env pair?". They used to answer it
# separately, and answered it DIFFERENTLY: the secret sweep matched only the
# SPLIT ``["--env", "K=V"]`` form, this module matched the GLUED
# ``--env=K=V`` too. So a spec written in the glued spelling — the form live
# across this fleet's ``spec.apptainer.raw_args`` — was deduplicated
# correctly and then swept NOT AT ALL, putting its value into the
# world-readable launcher argv with nothing reporting a problem.
#
# The sweep now calls this function. These tests pin the contract it depends
# on, so the two can never re-acquire separate opinions.
# ---------------------------------------------------------------------------


def test_env_pair_at_recognises_the_split_spelling() -> None:
    # Arrange
    argv = ["apptainer", "--env", "K=v", "img.sif"]
    # Act
    found = env_pair_at(argv, 1)
    # Assert
    assert found == ("K", "v", 2)


def test_env_pair_at_recognises_the_glued_spelling() -> None:
    # Arrange
    argv = ["apptainer", "--env=K=v", "img.sif"]
    # Act
    found = env_pair_at(argv, 1)
    # Assert
    assert found == ("K", "v", 1)


def test_env_pair_at_reports_width_so_callers_can_drop_the_whole_pair() -> None:
    """The width is what lets a caller remove a pair without knowing which
    spelling it was — the detail the secret sweep needs to lift a glued flag
    out as ONE token rather than leaving half of it behind."""
    # Arrange
    argv = ["--env=K=v", "--env", "J=w"]
    # Act
    widths = [env_pair_at(argv, 0)[2], env_pair_at(argv, 1)[2]]
    # Assert
    assert widths == [1, 2]


def test_env_pair_at_does_not_match_env_file() -> None:
    """``--env-file`` carries a PATH, not a pair, and must never be swept."""
    # Arrange
    argv = ["--env-file", "/x/secret.env"]
    # Act
    found = env_pair_at(argv, 0)
    # Assert
    assert found is None


def test_env_pair_at_declines_a_value_without_an_equals() -> None:
    """Left where it is for the malformed-flag guard to report."""
    # Arrange
    argv = ["--env", "NOEQUALS"]
    # Act
    found = env_pair_at(argv, 0)
    # Assert
    assert found is None
