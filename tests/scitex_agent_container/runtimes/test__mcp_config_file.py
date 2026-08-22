"""Secret material must never reach the ``claude`` child's command line.

Card ``sac-bot-token-plaintext-in-process-argv-20260814``: a plain
``ps -eo args`` on scitex-compute-04 printed a live Telegram bot token,
because ``claude-agent-sdk`` serialises an ``mcp_servers`` DICT into
``--mcp-config <json>`` — env blocks and all — and ``/proc/<pid>/cmdline`` is
world-readable while ``/proc/<pid>/environ`` is uid-restricted.

THE GUARD THAT ACTUALLY CATCHES IT
==================================
The exposure is a property of the LAUNCHED PROCESS, not of sac's own data
structures, so asserting on ``options.mcp_servers`` alone would keep passing
if the SDK changed how it serialises. :func:`_sdk_argv` therefore drives the
SDK's REAL argv builder (``SubprocessCLITransport._build_command``) — the
exact code that produced the leaked command line — and the assertions search
every argv element for a token-shaped sentinel.

``test_dict_form_puts_the_secret_on_the_child_argv`` pins the OLD behaviour
as still-true of the SDK, so the pair reads as cause and cure: the dict form
leaks, the externalised form does not. Deleting the
``externalize_mcp_servers`` call from ``build_sdk_options`` turns
``test_build_sdk_options_child_argv_carries_no_secret`` red.

The sentinel is a FAKE, deliberately shaped like a Telegram bot token
(``<digits>:<base64ish>``) so it matches the same pattern a real one would.
No real secret appears in this file.
"""

from __future__ import annotations

import json
import os
import resource
import signal
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scitex_agent_container.runtimes import _sdk_common
from scitex_agent_container.runtimes._mcp_config_file import (
    MCP_CONFIG_DIR_MODE,
    MCP_CONFIG_FILE_MODE,
    McpConfigWriteError,
    externalize_mcp_servers,
    has_inprocess_servers,
    read_mcp_servers,
    write_mcp_config_file,
)
from scitex_agent_container.runtimes._mcp_spec_env import SPEC_ENV_KEYS_VAR

#: A fake, token-SHAPED value. Never a real credential — the point is that it
#: matches what a scanner looking for a bot token would match.
SENTINEL = "1234567890:FAKE-not-a-real-token-000000000000000"

#: The env key the live leak travelled under (the telegrammer bot token).
SENTINEL_KEY = "CCT_BOT_TOKEN"


def _server_with_secret() -> dict[str, Any]:
    """One stdio MCP entry whose env block carries the sentinel."""
    return {
        "claude-code-telegrammer": {
            "type": "stdio",
            "command": "/usr/bin/true",
            "args": [],
            "env": {SENTINEL_KEY: SENTINEL},
        }
    }


class _Env:
    """Records env / attribute mutations and reverses them (PA-306, no mocks)."""

    def __init__(self) -> None:
        self._env: dict[str, str | None] = {}
        self._attrs: list[tuple[Any, str, Any]] = []

    def setenv(self, key: str, value: str) -> None:
        self._env.setdefault(key, os.environ.get(key))
        os.environ[key] = value

    def delenv(self, key: str) -> None:
        self._env.setdefault(key, os.environ.get(key))
        os.environ.pop(key, None)

    def setattr_module(self, obj: Any, name: str, value: Any) -> None:
        if not any(o is obj and n == name for o, n, _ in self._attrs):
            self._attrs.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self) -> None:
        for key, prev in self._env.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        for obj, name, prev in self._attrs:
            setattr(obj, name, prev)


@pytest.fixture
def env():
    helper = _Env()
    try:
        yield helper
    finally:
        helper.restore()


# ---------------------------------------------------------------------------
# The argv guard — driven through the SDK's own command builder
# ---------------------------------------------------------------------------


def _sdk_argv(mcp_servers: Any) -> list[str]:
    """Return the real ``claude`` child argv the SDK would exec.

    Uses the SDK's own ``SubprocessCLITransport._build_command`` so the
    assertion covers the actual serialisation that leaked, not a
    reimplementation of it. ``cli_path`` short-circuits binary discovery, so
    nothing is spawned and no ``claude`` install is needed.
    """
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    options = ClaudeAgentOptions(cli_path="/usr/bin/true", mcp_servers=mcp_servers)
    return SubprocessCLITransport(prompt="", options=options)._build_command()


def test_dict_form_puts_the_secret_on_the_child_argv() -> None:
    """The defect, pinned: a dict ``mcp_servers`` leaks env values into argv.

    Not a wish — a statement about the installed SDK. If this ever goes red,
    the SDK stopped inlining the config and the fix below may be relaxed;
    until then it is the reason the fix exists.
    """
    # Arrange
    servers = _server_with_secret()
    # Act
    argv = _sdk_argv(servers)
    # Assert
    assert any(SENTINEL in arg for arg in argv)


def test_externalized_form_keeps_the_secret_off_the_child_argv(
    tmp_path: Path,
) -> None:
    # Arrange
    kwargs: dict[str, Any] = {"mcp_servers": _server_with_secret()}
    externalize_mcp_servers(kwargs, "telegrammer-agent")
    # Act
    argv = _sdk_argv(kwargs["mcp_servers"])
    # Assert
    assert not any(SENTINEL in arg for arg in argv)


def test_externalized_form_still_passes_an_mcp_config_flag() -> None:
    """Removing the leak must not remove the config — claude still loads it."""
    # Arrange
    kwargs: dict[str, Any] = {"mcp_servers": _server_with_secret()}
    externalize_mcp_servers(kwargs, "telegrammer-agent")
    # Act
    argv = _sdk_argv(kwargs["mcp_servers"])
    # Assert
    assert "--mcp-config" in argv


def test_externalized_argv_value_is_the_config_path() -> None:
    # Arrange
    kwargs: dict[str, Any] = {"mcp_servers": _server_with_secret()}
    externalize_mcp_servers(kwargs, "telegrammer-agent")
    argv = _sdk_argv(kwargs["mcp_servers"])
    # Act
    value = argv[argv.index("--mcp-config") + 1]
    # Assert
    assert value == kwargs["mcp_servers"]


def test_file_named_in_argv_still_carries_the_server_entry() -> None:
    """The config claude reads is unchanged — only its transport moved."""
    # Arrange
    kwargs: dict[str, Any] = {"mcp_servers": _server_with_secret()}
    externalize_mcp_servers(kwargs, "telegrammer-agent")
    # Act
    servers = read_mcp_servers(kwargs["mcp_servers"])
    # Assert
    assert servers["claude-code-telegrammer"]["env"][SENTINEL_KEY] == SENTINEL


# ---------------------------------------------------------------------------
# File / directory permissions — argv is public, this file must not be
# ---------------------------------------------------------------------------


def test_config_file_is_readable_by_owner_only(tmp_path: Path) -> None:
    # Arrange
    directory = tmp_path / "d"
    # Act
    path = write_mcp_config_file("a", _server_with_secret(), dirs=[directory])
    # Assert
    assert stat.S_IMODE(os.stat(path).st_mode) == MCP_CONFIG_FILE_MODE


def test_config_dir_is_traversable_by_owner_only(tmp_path: Path) -> None:
    # Arrange
    directory = tmp_path / "d"
    # Act
    write_mcp_config_file("a", _server_with_secret(), dirs=[directory])
    # Assert
    assert stat.S_IMODE(os.stat(directory).st_mode) == MCP_CONFIG_DIR_MODE


def test_preexisting_world_readable_file_is_tightened(tmp_path: Path) -> None:
    """An 0644 file left by an older sac must not be reused as-is."""
    # Arrange
    directory = tmp_path / "d"
    directory.mkdir()
    stale = directory / "a.mcp.json"
    stale.write_text("{}")
    os.chmod(stale, 0o644)
    # Act
    path = write_mcp_config_file("a", _server_with_secret(), dirs=[directory])
    # Assert
    assert stat.S_IMODE(os.stat(path).st_mode) == MCP_CONFIG_FILE_MODE


def test_written_payload_uses_the_mcp_json_shape(tmp_path: Path) -> None:
    # Arrange
    directory = tmp_path / "d"
    # Act
    path = write_mcp_config_file("a", _server_with_secret(), dirs=[directory])
    # Assert
    assert "mcpServers" in json.loads(Path(path).read_text())


def test_unwritable_location_raises_instead_of_falling_back(tmp_path: Path) -> None:
    """No silent fallback: the dict form would re-publish every secret."""
    # Arrange: a FILE where the code wants a directory → mkdir raises OSError.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    servers = _server_with_secret()
    # Act
    def write():
        write_mcp_config_file("a", servers, dirs=[blocker / "sub"])

    # Assert
    with pytest.raises(McpConfigWriteError):
        write()


# ---------------------------------------------------------------------------
# The write is ATOMIC — a concurrent reader never sees an empty file
#
# MEASURED on CI 2026-08-22, pytest-matrix py3.11, xdist worker gw4:
#   test_compose_attaches_mcp_servers
#   -> read_mcp_servers -> json.loads("")
#   -> JSONDecodeError: Expecting value: line 1 column 1 (char 0)
# The old write opened with O_TRUNC and then wrote, so the file was EMPTY on
# disk between the two syscalls. The filename is keyed on the AGENT NAME, so
# every concurrent build_sdk_options("alpha", ...) targeted one shared path.
#
# A race cannot be tested by racing -- a passing timing test proves nothing.
# These pin the PROPERTIES that make the race impossible instead, and every
# one of them OBSERVES THE REAL ARTIFACT rather than patching a seam
# (PA-306 forbids mocks, and the observations below are strictly stronger
# than the spies they replaced -- a spy only proves os.replace was CALLED):
#
#   st_ino          an in-place O_TRUNC rewrite KEEPS the target's inode; a
#                   rename installs a DIFFERENT one. One stat call separates
#                   the two implementations.
#   a held fd       an open descriptor names the INODE, not the path, so a
#                   reader that opened the config first keeps reading
#                   whatever the writer did to that inode. That reader IS
#                   the one CI tripped over.
#   RLIMIT_FSIZE    a real, kernel-imposed ceiling on the write, hit after
#                   the point where the old form had already destroyed the
#                   target. Making the DIRECTORY unwritable does NOT work as
#                   a failure injector here: write_mcp_config_file chmods it
#                   back to 0700 itself (measured 2026-08-22 -- the write
#                   simply succeeds).
# ---------------------------------------------------------------------------

#: An existing, valid config -- what a reader could be holding open when a
#: rewrite starts.
_PREVIOUS_CONFIG = json.dumps({"mcpServers": {"old": {}}})

#: A file-size ceiling far below the payload, so the write fails mid-flight.
_TINY_FILE_LIMIT = 64


def _seeded_config_dir(tmp_path: Path, body: str, mode: int) -> Path:
    """A config dir already holding ``a.mcp.json`` with ``body`` at ``mode``."""
    directory = tmp_path / "d"
    directory.mkdir()
    target = directory / "a.mcp.json"
    target.write_text(body, encoding="utf-8")
    os.chmod(target, mode)
    return directory


def _rewrite(directory: Path) -> str:
    """Write the secret-bearing config into ``directory``; return its path."""
    return write_mcp_config_file("a", _server_with_secret(), dirs=[directory])


def _what_a_holding_reader_reads(directory: Path) -> str:
    """What a reader that opened the config BEFORE a rewrite reads after it.

    The descriptor is bound to the inode the config had on entry. An atomic
    rename leaves that inode untouched and moves a different one into the
    path, so the reader still sees the old bytes; an in-place ``O_TRUNC``
    rewrite mutates the reader's own inode under it, so the reader sees the
    new payload -- or, at the wrong instant, nothing at all, which is the
    ``json.loads("")`` CI reported.
    """
    handle = (directory / "a.mcp.json").open("r", encoding="utf-8")
    try:
        _rewrite(directory)
        return handle.read()
    finally:
        handle.close()


def _rewrite_under_a_tiny_file_size_limit(directory: Path) -> None:
    """Run the write with ``RLIMIT_FSIZE`` far below the payload it must store.

    A REAL, kernel-imposed obstacle rather than an injected one. ``SIGXFSZ``
    is ignored for the duration so an over-limit write reports an error
    instead of killing the process; the limit and the handler are both
    restored before returning, so the window is confined to this one call.

    The two implementations answer it very differently, MEASURED 2026-08-22:

      * the atomic form buffers the payload and flushes it, so the flush
        raises ``EFBIG``, the temp file is unlinked, and the call ends in
        ``McpConfigWriteError`` (swallowed here — the caller asserts on the
        DIRECTORY, not on the exception);
      * the old ``os.write`` form got a SHORT COUNT back (64 of 239 bytes)
        and never checked it, so it reported SUCCESS while leaving a
        truncated stub where the config used to be.

    Nothing is asserted here on purpose. If this obstacle ever stops biting,
    the write simply succeeds and the callers' assertions go red — they can
    never pass vacuously.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    previous_handler = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (_TINY_FILE_LIMIT, hard))
        try:
            _rewrite(directory)
        except McpConfigWriteError:
            pass
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
        signal.signal(signal.SIGXFSZ, previous_handler)


def test_a_rewrite_replaces_the_config_inode(tmp_path: Path) -> None:
    """The file is REPLACED, never written into in place.

    ``O_TRUNC`` preserves ``st_ino``; ``os.replace`` installs a new inode.
    That one number is the whole difference between the racy write and the
    atomic one, and it is a property of the artifact on disk.
    """
    # Arrange
    directory = _seeded_config_dir(tmp_path, _PREVIOUS_CONFIG, MCP_CONFIG_FILE_MODE)
    before = os.stat(directory / "a.mcp.json").st_ino
    # Act
    _rewrite(directory)
    # Assert
    assert os.stat(directory / "a.mcp.json").st_ino != before


def test_a_holding_reader_still_reads_the_previous_config(tmp_path: Path) -> None:
    """The CI reader, reproduced without racing: it keeps its own inode."""
    # Arrange
    directory = _seeded_config_dir(tmp_path, _PREVIOUS_CONFIG, MCP_CONFIG_FILE_MODE)
    # Act
    seen = _what_a_holding_reader_reads(directory)
    # Assert
    assert seen == _PREVIOUS_CONFIG


def test_a_failed_write_leaves_the_previous_config_intact(tmp_path: Path) -> None:
    """Atomicity's other half: a write that cannot finish must not destroy
    what was already there.

    The old form truncated the target FIRST and wrote second, so anything
    that went wrong in between left the agent with a mangled MCP config. It
    did not even need an exception to do it: under the size limit it kept
    the short count ``os.write`` returned and reported success.
    """
    # Arrange
    directory = _seeded_config_dir(tmp_path, _PREVIOUS_CONFIG, MCP_CONFIG_FILE_MODE)
    # Act
    _rewrite_under_a_tiny_file_size_limit(directory)
    # Assert
    assert (directory / "a.mcp.json").read_text(encoding="utf-8") == _PREVIOUS_CONFIG


def test_a_failed_write_leaves_no_file_behind(tmp_path: Path) -> None:
    """No stray temp file, and no truncated stub either.

    The old form's ``O_CREAT`` created the target before it had the bytes to
    fill it, so a write that could not complete still published a partial
    config for ``claude`` to load.
    """
    # Arrange
    directory = tmp_path / "d"
    directory.mkdir()
    # Act
    _rewrite_under_a_tiny_file_size_limit(directory)
    # Assert
    assert list(directory.iterdir()) == []


def test_a_successful_write_leaves_only_the_config_behind(tmp_path: Path) -> None:
    """GUARD, not a regression test — the old form passed this too.

    It pins that the temp file the atomic write now creates is always renamed
    away and never accumulates.
    """
    # Arrange
    directory = tmp_path / "d"
    # Act
    _rewrite(directory)
    # Assert
    assert [q.name for q in directory.iterdir()] == ["a.mcp.json"]


def test_the_world_readable_file_is_replaced_not_rewritten(tmp_path: Path) -> None:
    """The 0644 inode an older sac left must not be the one that gets filled.

    ``test_preexisting_world_readable_file_is_tightened`` above pins the
    FINAL mode, and the old code reached 0600 too -- by writing the resolved
    secret literals into the pre-existing 0644 inode and chmod-ing after. A
    final-state assertion cannot tell those apart. Inode identity can: the
    payload lands on a fresh mkstemp inode, so the world-readable one is no
    longer the file at all.
    """
    # Arrange
    directory = _seeded_config_dir(tmp_path, "{}", 0o644)
    stale_inode = os.stat(directory / "a.mcp.json").st_ino
    # Act
    path = _rewrite(directory)
    # Assert
    assert os.stat(path).st_ino != stale_inode


def test_the_world_readable_inode_never_receives_the_secret(tmp_path: Path) -> None:
    """...and that inode never holds the secret, at any instant.

    Read through a descriptor opened while the file was still 0644: whatever
    that descriptor can see, every local user could have seen too.
    """
    # Arrange
    directory = _seeded_config_dir(tmp_path, "{}", 0o644)
    # Act
    seen = _what_a_holding_reader_reads(directory)
    # Assert
    assert SENTINEL not in seen


# ---------------------------------------------------------------------------
# externalize_mcp_servers — the narrow no-op cases
# ---------------------------------------------------------------------------


def test_no_servers_is_a_noop() -> None:
    # Arrange
    kwargs: dict[str, Any] = {}
    # Act
    changed = externalize_mcp_servers(kwargs, "a")
    # Assert
    assert changed is False


def test_inprocess_server_declines_the_rewrite() -> None:
    """``type: sdk`` entries hold a live object and cannot be serialised."""
    # Arrange
    kwargs: dict[str, Any] = {
        "mcp_servers": {"inproc": {"type": "sdk", "instance": object()}}
    }
    # Act
    changed = externalize_mcp_servers(kwargs, "a")
    # Assert
    assert changed is False


def test_inprocess_detector_ignores_stdio_entries() -> None:
    # Arrange
    servers = _server_with_secret()
    # Act
    detected = has_inprocess_servers(servers)
    # Assert
    assert detected is False


def test_already_a_path_is_left_alone(tmp_path: Path) -> None:
    # Arrange
    kwargs: dict[str, Any] = {"mcp_servers": str(tmp_path / "x.mcp.json")}
    # Act
    changed = externalize_mcp_servers(kwargs, "a")
    # Assert
    assert changed is False


# ---------------------------------------------------------------------------
# End-to-end: build_sdk_options is where the wiring must live
# ---------------------------------------------------------------------------


def _valid_creds_json() -> str:
    expires_at_ms = int((time.time() + 86_400) * 1_000)
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "tok",
                "refreshToken": "ref",
                "expiresAt": expires_at_ms,
                "scopes": ["user:inference"],
                "subscriptionType": "max",
            }
        }
    )


@pytest.fixture
def _built_options(env: _Env, tmp_path: Path):
    """``build_sdk_options`` for an agent whose MCP entry holds a secret."""
    # Arrange: hermetic auth (no touch of the operator's real credentials).
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / ".credentials.json").write_text(_valid_creds_json())
    env.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))
    env.delenv("ANTHROPIC_API_KEY")
    env.delenv(_sdk_common._SAC_API_KEY_ENV)
    # Arrange: drop the spec-env manifest this test process inherited from
    # its own sac container. Left in place, ``bake_spec_env_into_servers``
    # fails loud on the HOST agent's keys and the fixture errors before it
    # can say anything about argv.
    env.delenv(SPEC_ENV_KEYS_VAR)
    # Arrange: HOME redirected so the merge of $HOME/.mcp.json and the config
    # file this fix writes both stay inside tmp_path.
    home = tmp_path / "home"
    home.mkdir()
    env.setenv("HOME", str(home))
    # Arrange: a registered workspace whose .mcp.json carries the sentinel.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": _server_with_secret()}))

    import scitex_agent_container._state.registry as reg_mod
    import scitex_agent_container.config as cfg_mod

    class _FakeRegistry:
        def get(self, _name):
            return {"config": "cfg.yaml"}

    env.setattr_module(reg_mod, "Registry", _FakeRegistry)
    env.setattr_module(
        cfg_mod,
        "load_config",
        lambda _path: SimpleNamespace(expanded_workdir=str(ws)),
    )
    return _sdk_common.build_sdk_options("telegrammer-agent")


def test_build_sdk_options_child_argv_carries_no_secret(_built_options) -> None:
    """THE regression guard: remove the wiring and this goes red."""
    # Arrange
    configured = _built_options.mcp_servers
    # Act
    argv = _sdk_argv(configured)
    # Assert
    assert not any(SENTINEL in arg for arg in argv)


def test_build_sdk_options_still_delivers_the_server_entry(_built_options) -> None:
    # Arrange
    configured = _built_options.mcp_servers
    # Act
    servers = read_mcp_servers(configured)
    # Assert
    assert servers["claude-code-telegrammer"]["env"][SENTINEL_KEY] == SENTINEL


def test_build_sdk_options_config_file_is_owner_only(_built_options) -> None:
    # Arrange
    path = str(_built_options.mcp_servers)
    # Act
    mode = stat.S_IMODE(os.stat(path).st_mode)
    # Assert
    assert mode == MCP_CONFIG_FILE_MODE
