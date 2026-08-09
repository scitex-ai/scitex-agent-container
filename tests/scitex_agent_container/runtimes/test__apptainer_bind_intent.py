"""Declared-intent bind entries — resolution at the apptainer choke point.

A spec's ``apptainer.binds`` entry may stay a plain
``host:container[:mode]`` STRING (unchanged, unconditional, fatal when
its source is absent) or become a MAPPING that declares intent:

    - source: /mnt/c
      dest: /mnt/c
      mode: rw
      required: false          # absent source -> visible skip
    - source: ~/.scitex/cards
      dest: /home/agent/.scitex/cards
      ensure: dir              # create the source dir, then bind
    - source: /home/ywatanabe/.bun/bin/bun
      dest: /usr/local/bin/bun
      hosts: [ywata-note-win]  # applies only on the listed hosts

The load-bearing test in this file is
``test_string_form_binds_are_byte_identical_to_recorded_baseline`` — 107
live specs are plain strings and their argv must not move by one byte.
The golden list it asserts was RECORDED from the pre-change tree, so it
pins the OLD behaviour, not the new code's opinion of it.

Real ``load_config`` on real spec files, real ``build_run_argv``, real
directories under ``tmp_path`` — no mocks, no monkeypatch (PA-306 /
STX-NM002). STX-TQ002 AAA markers.

Named ``test__apptainer_bind_intent.py`` for the PS-204 §2 mirror rule
against ``src/scitex_agent_container/runtimes/_apptainer_bind_intent.py``.
The parse-side companion (``config._bind_intent.parse_bind_entries``,
reached through ``load_config``) is exercised here too because its
validation errors are only meaningful against the resolution they gate.
"""

from __future__ import annotations

import logging
import os
import re
import socket
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.config._host import resolve_hostname
from scitex_agent_container.runtimes._apptainer_build_argv import build_run_argv
from scitex_agent_container.runtimes._apptainer_jail import ENV_BIND_VARS
from scitex_agent_container.runtimes._p3a_default_binds import default_binds_for_host

_PROBE_VAR = "SAC_BIND_INTENT_PROBE_DIR"
_PROBE_VAL = "/srv/probe-var"
_BIND_ENV_VARS = ENV_BIND_VARS


@pytest.fixture
def _isolate_home(tmp_path: Path) -> Iterator[Path]:
    """Redirect ``HOME`` to a per-test tmp dir.

    ``~`` in a bind source, the fleet-default bind filter and the bearer
    -token resolver all anchor on ``$HOME``; sliding it into ``tmp_path``
    keeps every one of them inside the test.
    """
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
def no_env_binds() -> Iterator[None]:
    """Clear ``$APPTAINER_BIND`` & friends for the jail tests.

    NOT cosmetic. sac itself runs inside apptainer, so these vars are SET
    in this process, and ``enforce_jail`` refuses an env-injected bind
    too. Without this the jail tests would pass on every branch — green
    for a reason that has nothing to do with the spec entry under test.
    Clearing them leaves the spec bind as the only possible refusal, which
    the assertions then name explicitly.
    """
    saved = {v: os.environ.pop(v, None) for v in _BIND_ENV_VARS}
    try:
        yield None
    finally:
        for var, value in saved.items():
            if value is not None:
                os.environ[var] = value


@pytest.fixture
def probe_var() -> Iterator[str]:
    """Export a real env var so ``$VAR`` bind-source expansion is exercised."""
    saved = os.environ.get(_PROBE_VAR)
    os.environ[_PROBE_VAR] = _PROBE_VAL
    try:
        yield _PROBE_VAL
    finally:
        if saved is None:
            os.environ.pop(_PROBE_VAR, None)
        else:
            os.environ[_PROBE_VAR] = saved


_SPEC_TEMPLATE = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: t
    sac-builtin: "off"
spec:
  runtime: tui
  host: ${{HOSTNAME}}
  workdir: /tmp/agt-work
  apptainer:
    image: /x.sif
{jail}    binds:
{binds}
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


def _write_spec(tmp_path: Path, binds_block: str, *, jail: bool = False) -> Path:
    """Materialise a loadable v3 spec whose ``apptainer.binds`` is given."""
    from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

    body = _SPEC_TEMPLATE.format(
        binds=binds_block, jail="    jail: true\n" if jail else ""
    )
    spec_dir = tmp_path / "agents" / "agt"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / "spec.yaml"
    spec.write_text(explicitize_yaml(body), encoding="utf-8")
    return spec


def _bind_values(argv: list[str]) -> list[str]:
    """Every ``--bind`` VALUE in ``argv``, in emission order."""
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]


def _argv_for(tmp_path: Path, binds_block: str, *, jail: bool = False) -> list[str]:
    config = load_config(str(_write_spec(tmp_path, binds_block, jail=jail)))
    return build_run_argv(
        config, state_dir=tmp_path / "state", sif_path=Path("/x.sif"), tui=True
    )


# ----------------------------------------------------------------------
# The load-bearing one: the STRING form must not move.
# ----------------------------------------------------------------------
# Recorded from the pre-change tree (origin/develop @ 4a803a34) by
# building this exact spec and dumping every `--bind` value. `{home}` is
# the isolated HOME the fixture installs. Entries cover every shape the
# 107 live specs use: whole-home rw, a source MISSING on this host
# (/mnt/c, in 101 of them), a /home/agent destination, a `~` source, a
# file (not dir) source, a `$VAR` source, and a mode-less entry.
_BASELINE_SPEC_BINDS = (
    "/home/ywatanabe:/home/ywatanabe:rw",
    "/mnt/c:/mnt/c:rw",
    "/home/ywatanabe/.ssh:/home/agent/.ssh:ro",
    "{home}/.scitex/dataset/capsule-001:/capsule:ro",
    "/home/ywatanabe/.bun/bin/bun:/usr/local/bin/bun:ro",
    "/srv/probe-var:/probe-var:rw",
    "/srv/data:/data",
)

_BASELINE_BINDS_BLOCK = f"""\
    - /home/ywatanabe:/home/ywatanabe:rw
    - /mnt/c:/mnt/c:rw
    - /home/ywatanabe/.ssh:/home/agent/.ssh:ro
    - ~/.scitex/dataset/capsule-001:/capsule:ro
    - /home/ywatanabe/.bun/bin/bun:/usr/local/bin/bun:ro
    - ${_PROBE_VAR}:/probe-var:rw
    - /srv/data:/data
"""


def test_string_form_binds_are_byte_identical_to_recorded_baseline(
    tmp_path, _isolate_home, probe_var
) -> None:
    # Arrange — the golden was recorded from the pre-change tree.
    expected = [b.format(home=_isolate_home) for b in _BASELINE_SPEC_BINDS]
    # Act
    argv = _argv_for(tmp_path, _BASELINE_BINDS_BLOCK)
    # Assert — same values, same order, same modes; not one byte moved.
    assert _bind_values(argv)[-len(expected) :] == expected


def test_string_form_binds_still_follow_the_fleet_defaults(
    tmp_path, _isolate_home, probe_var
) -> None:
    # Arrange — pins the spec block's POSITION, so the tail-slice the
    # baseline test reads cannot silently start meaning something else.
    defaults = list(default_binds_for_host())
    expected = defaults + [b.format(home=_isolate_home) for b in _BASELINE_SPEC_BINDS]
    # Act
    argv = _argv_for(tmp_path, _BASELINE_BINDS_BLOCK)
    # Assert
    assert _bind_values(argv)[-len(expected) :] == expected


def test_binds_without_recorded_intents_still_all_mount(
    tmp_path, _isolate_home
) -> None:
    # Arrange — an ApptainerSpec built in CODE carries binds but no
    # intents (plenty of call sites do this). The resolver must fall back
    # to "every bind required and unconditional" — the pre-intent
    # behaviour — rather than resolve an empty intent list into no mounts.
    config = load_config(str(_write_spec(tmp_path, "    - /srv/a:/a:rw\n")))
    config.apptainer.bind_intents = []
    # Act
    argv = build_run_argv(
        config, state_dir=tmp_path / "state", sif_path=Path("/x.sif"), tui=True
    )
    # Assert
    assert "/srv/a:/a:rw" in argv


def test_tilde_bind_source_is_expanded_before_reaching_argv(
    tmp_path, _isolate_home
) -> None:
    # Arrange — apptainer runs no shell, so a literal `~` would mount
    # nothing anywhere. Six live specs (clew-a-001..006) rely on this.
    block = "    - ~/.scitex/dataset/capsule-001:/capsule:ro\n"
    # Act
    argv = _argv_for(tmp_path, block)
    # Assert
    assert f"{_isolate_home}/.scitex/dataset/capsule-001:/capsule:ro" in argv


def test_tilde_bind_source_is_expanded_in_the_mapping_form_too(
    tmp_path, _isolate_home
) -> None:
    # Arrange — the new form must not reintroduce the unexpanded `~`.
    block = "    - {source: ~/.scitex/cards, dest: /cards, mode: rw}\n"
    # Act
    argv = _argv_for(tmp_path, block)
    # Assert
    assert f"{_isolate_home}/.scitex/cards:/cards:rw" in argv


def test_legacy_src_dst_dict_form_still_normalises_to_a_string(
    tmp_path, _isolate_home
) -> None:
    # Arrange — the pre-existing `{src, dst, mode}` dict form.
    block = "    - {src: /srv/legacy, dst: /legacy, mode: ro}\n"
    # Act
    argv = _argv_for(tmp_path, block)
    # Assert
    assert "/srv/legacy:/legacy:ro" in argv


# ----------------------------------------------------------------------
# required: — default TRUE, nothing weakens
# ----------------------------------------------------------------------
def test_plain_string_with_absent_source_still_reaches_argv(
    tmp_path, _isolate_home
) -> None:
    # Arrange — no declared intent, source absent on this host. sac must
    # keep handing the bind to apptainer, which FATALs at container
    # creation. Downgrading that to a skip is the regression this guards.
    block = "    - /mnt/c:/mnt/c:rw\n"
    # Act
    argv = _argv_for(tmp_path, block)
    # Assert
    assert "/mnt/c:/mnt/c:rw" in argv


def test_mapping_without_required_key_defaults_to_required_true(
    tmp_path, _isolate_home
) -> None:
    # Arrange — a MAPPING that declares no `required:` is required.
    block = "    - {source: /mnt/c, dest: /mnt/c, mode: rw}\n"
    # Act
    argv = _argv_for(tmp_path, block)
    # Assert — emitted despite the absent source, exactly like the string.
    assert "/mnt/c:/mnt/c:rw" in argv


def test_explicit_required_true_with_absent_source_still_reaches_argv(
    tmp_path, _isolate_home
) -> None:
    # Arrange
    block = "    - {source: /mnt/c, dest: /mnt/c, mode: rw, required: true}\n"
    # Act
    argv = _argv_for(tmp_path, block)
    # Assert
    assert "/mnt/c:/mnt/c:rw" in argv


def test_required_false_skips_the_bind_when_source_is_absent(
    tmp_path, _isolate_home
) -> None:
    # Arrange — the WSL-only mount, on a host that has no /mnt/c.
    block = "    - {source: /mnt/c, dest: /mnt/c, mode: rw, required: false}\n"
    # Act
    argv = _argv_for(tmp_path, block)
    # Assert
    assert "/mnt/c:/mnt/c:rw" not in argv


def test_required_false_skip_is_logged_not_silent(
    tmp_path, _isolate_home, caplog
) -> None:
    # Arrange — a silently dropped mount is how an agent comes up looking
    # healthy but wrong; the skip must be readable in the launch log.
    block = "    - {source: /mnt/c, dest: /mnt/c, mode: rw, required: false}\n"
    caplog.set_level(logging.WARNING)
    # Act
    _argv_for(tmp_path, block)
    # Assert — one scannable line naming the bind that went.
    assert "/mnt/c:/mnt/c:rw" in caplog.text


def test_required_false_skip_log_names_the_agent(
    tmp_path, _isolate_home, caplog
) -> None:
    # Arrange — 101 specs skip /mnt/c; the line must say whose launch it is.
    block = "    - {source: /mnt/c, dest: /mnt/c, mode: rw, required: false}\n"
    caplog.set_level(logging.WARNING)
    # Act
    _argv_for(tmp_path, block)
    # Assert
    assert "agt" in caplog.text


def test_required_false_still_binds_when_the_source_exists(
    tmp_path, _isolate_home
) -> None:
    # Arrange — optional is about ABSENCE; a present source still mounts.
    src = tmp_path / "present"
    src.mkdir()
    block = f"    - {{source: {src}, dest: /present, mode: rw, required: false}}\n"
    # Act
    argv = _argv_for(tmp_path, block)
    # Assert
    assert f"{src}:/present:rw" in argv


def test_required_false_accepts_a_file_source(tmp_path, _isolate_home) -> None:
    # Arrange — `/home/ywatanabe/.bun/bin/bun` is a FILE bind; existence,
    # not dir-ness, is the predicate.
    src = tmp_path / "bun"
    src.write_text("#!/bin/sh\n", encoding="utf-8")
    block = f"    - {{source: {src}, dest: /usr/local/bin/bun, required: false}}\n"
    # Act
    argv = _argv_for(tmp_path, block)
    # Assert
    assert f"{src}:/usr/local/bin/bun" in argv


# ----------------------------------------------------------------------
# ensure: dir
# ----------------------------------------------------------------------
def test_ensure_dir_creates_the_missing_source(tmp_path, _isolate_home) -> None:
    # Arrange — the ~/.scitex/cards shape: sac owns creating the dir.
    src = tmp_path / "cards-root" / "cards"
    block = (
        f"    - {{source: {src}, dest: /home/agent/.scitex/cards, "
        "mode: rw, ensure: dir}\n"
    )
    # Act
    _argv_for(tmp_path, block)
    # Assert — parents created too.
    assert src.is_dir()


def test_ensure_dir_then_emits_the_bind(tmp_path, _isolate_home) -> None:
    # Arrange
    src = tmp_path / "cards-root" / "cards"
    block = (
        f"    - {{source: {src}, dest: /home/agent/.scitex/cards, "
        "mode: rw, ensure: dir}\n"
    )
    # Act
    argv = _argv_for(tmp_path, block)
    # Assert
    assert f"{src}:/home/agent/.scitex/cards:rw" in argv


def test_ensure_dir_is_idempotent_on_an_existing_source(
    tmp_path, _isolate_home
) -> None:
    # Arrange — an already-present source keeps its content.
    src = tmp_path / "cards"
    src.mkdir()
    (src / "keep.db").write_text("x", encoding="utf-8")
    block = f"    - {{source: {src}, dest: /cards, mode: rw, ensure: dir}}\n"
    # Act
    _argv_for(tmp_path, block)
    # Assert
    assert (src / "keep.db").read_text(encoding="utf-8") == "x"


def test_ensure_dir_creation_failure_is_loud(tmp_path, _isolate_home) -> None:
    # Arrange — a FILE where the source's parent must be, so mkdir cannot
    # succeed. Degrading this to a skip would hide a broken mount.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    block = f"    - {{source: {blocker / 'cards'}, dest: /cards, ensure: dir}}\n"
    # Act
    # Assert
    with pytest.raises(RuntimeError, match=r"ensure: dir"):
        _argv_for(tmp_path, block)


def test_ensure_dir_creation_failure_is_loud_even_when_not_required(
    tmp_path, _isolate_home
) -> None:
    # Arrange — `required: false` excuses an ABSENT source, never a
    # FAILED creation: the operator asked for the dir to exist.
    blocker = tmp_path / "blocker2"
    blocker.write_text("not a directory", encoding="utf-8")
    block = (
        f"    - {{source: {blocker / 'cards'}, dest: /cards, "
        "ensure: dir, required: false}\n"
    )
    # Act
    # Assert
    with pytest.raises(RuntimeError, match=r"ensure: dir"):
        _argv_for(tmp_path, block)


# ----------------------------------------------------------------------
# hosts:
# ----------------------------------------------------------------------
def test_hosts_listing_this_host_emits_the_bind(tmp_path, _isolate_home) -> None:
    # Arrange — resolved through the codebase's own hostname authority.
    src = tmp_path / "bun"
    src.write_text("#!/bin/sh\n", encoding="utf-8")
    block = (
        f"    - {{source: {src}, dest: /usr/local/bin/bun, "
        f"hosts: [{resolve_hostname()}]}}\n"
    )
    # Act
    argv = _argv_for(tmp_path, block)
    # Assert
    assert f"{src}:/usr/local/bin/bun" in argv


def test_hosts_accepts_the_bare_short_hostname(tmp_path, _isolate_home) -> None:
    # Arrange — `_local_host_names` unions every spelling of THIS machine,
    # so the bare short hostname matches even if a config alias renames it.
    src = tmp_path / "bun"
    src.write_text("#!/bin/sh\n", encoding="utf-8")
    short = socket.gethostname().split(".")[0]
    block = f"    - {{source: {src}, dest: /usr/local/bin/bun, hosts: [{short}]}}\n"
    # Act
    argv = _argv_for(tmp_path, block)
    # Assert
    assert f"{src}:/usr/local/bin/bun" in argv


def test_hosts_excluding_this_host_skips_the_bind(tmp_path, _isolate_home) -> None:
    # Arrange — source EXISTS, so only the host gate can drop it.
    src = tmp_path / "bun"
    src.write_text("#!/bin/sh\n", encoding="utf-8")
    block = (
        f"    - {{source: {src}, dest: /usr/local/bin/bun, hosts: [ywata-note-win]}}\n"
    )
    # Act
    argv = _argv_for(tmp_path, block)
    # Assert
    assert f"{src}:/usr/local/bin/bun" not in argv


def test_hosts_skip_is_logged_not_silent(tmp_path, _isolate_home, caplog) -> None:
    # Arrange
    src = tmp_path / "bun"
    src.write_text("#!/bin/sh\n", encoding="utf-8")
    block = (
        f"    - {{source: {src}, dest: /usr/local/bin/bun, hosts: [ywata-note-win]}}\n"
    )
    caplog.set_level(logging.WARNING)
    # Act
    _argv_for(tmp_path, block)
    # Assert
    assert "ywata-note-win" in caplog.text


# --- the documented interaction rule (brief item 5) --------------------
def test_hosts_gate_wins_over_required_true_and_skips(tmp_path, _isolate_home) -> None:
    # Arrange — RULE: `hosts:` is a GATE evaluated FIRST. On a host the
    # list excludes, the entry is NOT DECLARED here, so `required:` (which
    # is only about source EXISTENCE within an applicable entry) is never
    # consulted. Source absent AND required: true AND wrong host -> SKIP,
    # never fatal. The opposite choice would make `hosts:` unusable: /mnt/c
    # is in 101 of 107 live specs and absent on every non-WSL host.
    block = (
        "    - {source: /mnt/c, dest: /mnt/c, mode: rw, "
        "required: true, hosts: [ywata-note-win]}\n"
    )
    # Act
    argv = _argv_for(tmp_path, block)
    # Assert
    assert "/mnt/c:/mnt/c:rw" not in argv


def test_hosts_gate_skip_names_required_true_in_the_log(
    tmp_path, _isolate_home, caplog
) -> None:
    # Arrange — the rule is surprising enough that the log must say the
    # entry was required and still skipped, not just "skipped".
    block = (
        "    - {source: /mnt/c, dest: /mnt/c, mode: rw, "
        "required: true, hosts: [ywata-note-win]}\n"
    )
    caplog.set_level(logging.WARNING)
    # Act
    _argv_for(tmp_path, block)
    # Assert
    assert "required: true" in caplog.text


def test_hosts_gate_prevents_ensure_dir_from_creating_anything(
    tmp_path, _isolate_home
) -> None:
    # Arrange — an entry that does not apply here must not touch this
    # host's filesystem either.
    src = tmp_path / "not-for-this-host"
    block = (
        f"    - {{source: {src}, dest: /x, mode: rw, "
        "ensure: dir, hosts: [ywata-note-win]}\n"
    )
    # Act
    _argv_for(tmp_path, block)
    # Assert
    assert not src.exists()


# ----------------------------------------------------------------------
# Validation — malformed entries fail loud and useful
# ----------------------------------------------------------------------
def test_unknown_key_in_a_bind_mapping_is_rejected(tmp_path, _isolate_home) -> None:
    # Arrange — a typo must not be silently ignored into an unconditional
    # bind ("requred" would have left required: true in force).
    spec = _write_spec(tmp_path, "    - {source: /mnt/c, dest: /mnt/c, requred: 0}\n")
    # Act
    # Assert
    with pytest.raises(ValueError, match=r"requred"):
        load_config(str(spec))


def test_unknown_key_error_lists_the_valid_keys(tmp_path, _isolate_home) -> None:
    # Arrange
    spec = _write_spec(tmp_path, "    - {source: /mnt/c, dest: /mnt/c, requred: 0}\n")
    # Act
    # Assert — every writable key, so the fix needs no doc lookup.
    with pytest.raises(ValueError, match=r"'required'"):
        load_config(str(spec))


def test_unknown_key_error_names_the_offending_spec_file(
    tmp_path, _isolate_home
) -> None:
    # Arrange — 107 specs; an error that does not say WHICH file is a
    # scavenger hunt.
    spec = _write_spec(tmp_path, "    - {source: /mnt/c, dest: /mnt/c, nope: 1}\n")
    # Act
    # Assert
    with pytest.raises(ValueError, match=re.escape(str(spec))):
        load_config(str(spec))


def test_unknown_key_error_carries_a_paste_ready_fix(tmp_path, _isolate_home) -> None:
    # Arrange
    spec = _write_spec(tmp_path, "    - {source: /mnt/c, dest: /mnt/c, nope: 1}\n")
    # Act
    # Assert — the corrected entry, ready to paste back.
    with pytest.raises(ValueError, match=re.escape("- source: /mnt/c")):
        load_config(str(spec))


def test_mapping_without_source_is_rejected(tmp_path, _isolate_home) -> None:
    # Arrange
    spec = _write_spec(tmp_path, "    - {dest: /mnt/c, mode: rw}\n")
    # Act
    # Assert
    with pytest.raises(ValueError, match=r"source"):
        load_config(str(spec))


def test_mapping_without_dest_is_rejected(tmp_path, _isolate_home) -> None:
    # Arrange
    spec = _write_spec(tmp_path, "    - {source: /mnt/c, mode: rw}\n")
    # Act
    # Assert
    with pytest.raises(ValueError, match=r"dest"):
        load_config(str(spec))


def test_relative_dest_in_a_mapping_is_rejected(tmp_path, _isolate_home) -> None:
    # Arrange — apptainer rejects relative bind targets opaquely.
    spec = _write_spec(tmp_path, "    - {source: /mnt/c, dest: mnt/c}\n")
    # Act
    # Assert
    with pytest.raises(ValueError, match=r"absolute"):
        load_config(str(spec))


def test_unknown_ensure_value_is_rejected(tmp_path, _isolate_home) -> None:
    # Arrange — `ensure: file` is not implemented; accepting it silently
    # would promise a creation that never happens.
    spec = _write_spec(tmp_path, "    - {source: /mnt/c, dest: /mnt/c, ensure: file}\n")
    # Act
    # Assert
    with pytest.raises(ValueError, match=r"ensure"):
        load_config(str(spec))


def test_non_boolean_required_is_rejected(tmp_path, _isolate_home) -> None:
    # Arrange — YAML's `required: "false"` is a truthy STRING; accepting
    # it would silently mean the opposite of what it reads as.
    spec = _write_spec(
        tmp_path, '    - {source: /mnt/c, dest: /mnt/c, required: "false"}\n'
    )
    # Act
    # Assert
    with pytest.raises(ValueError, match=r"required"):
        load_config(str(spec))


def test_non_list_hosts_is_rejected(tmp_path, _isolate_home) -> None:
    # Arrange — a bare string would match no host and skip everywhere.
    spec = _write_spec(
        tmp_path, "    - {source: /mnt/c, dest: /mnt/c, hosts: ywata-note-win}\n"
    )
    # Act
    # Assert
    with pytest.raises(ValueError, match=r"hosts"):
        load_config(str(spec))


def test_empty_hosts_list_is_rejected(tmp_path, _isolate_home) -> None:
    # Arrange — `hosts: []` reads as "no hosts", i.e. a bind that can
    # never apply anywhere. That is a mistake, not a declaration.
    spec = _write_spec(tmp_path, "    - {source: /mnt/c, dest: /mnt/c, hosts: []}\n")
    # Act
    # Assert
    with pytest.raises(ValueError, match=r"hosts"):
        load_config(str(spec))


# ----------------------------------------------------------------------
# The jail guardrail must not be weakened by any of the above
# ----------------------------------------------------------------------
def test_jailed_capsule_still_refuses_an_optional_forbidden_bind(
    tmp_path, _isolate_home, no_env_binds
) -> None:
    # Arrange — declared intent decides what MOUNTS; it must not decide
    # what the jail INSPECTS. A `required: false` /home bind is still a
    # refusal, whether or not it would have been skipped at this moment.
    # The match names the SPEC entry, so an env-injected refusal cannot
    # stand in for it.
    block = (
        "    - {source: /home/ywatanabe, dest: /home/ywatanabe, "
        "mode: rw, required: false}\n"
    )
    config = load_config(str(_write_spec(tmp_path, block, jail=True)))
    # Act
    # Assert
    with pytest.raises(RuntimeError, match=r"spec\.apptainer\.binds entry"):
        build_run_argv(
            config, state_dir=tmp_path / "state", sif_path=Path("/x.sif"), tui=True
        )


def test_jailed_capsule_still_refuses_a_host_gated_forbidden_bind(
    tmp_path, _isolate_home, no_env_binds
) -> None:
    # Arrange — same reasoning for the host gate: the jail is conservative
    # by design and inspects every DECLARED source, not the applicable set.
    block = (
        "    - {source: /home/ywatanabe, dest: /home/ywatanabe, "
        "mode: rw, hosts: [ywata-note-win]}\n"
    )
    config = load_config(str(_write_spec(tmp_path, block, jail=True)))
    # Act
    # Assert
    with pytest.raises(RuntimeError, match=r"spec\.apptainer\.binds entry"):
        build_run_argv(
            config, state_dir=tmp_path / "state", sif_path=Path("/x.sif"), tui=True
        )
