"""Unit coverage for ``_network/_ssh_curl._post_via_ssh_curl``.

Single-assertion tests (PA-307) exercising the argv shape and bearer
handling of the generalized ssh + remote curl helper introduced by
ADR-0015 Stage 2. The helper is currently shared by:

* ``_network.peer._post_turn_via_ssh`` (``/v1/turn``, no bearer).
* ``_listen._node_channel_forwarders._forward_via_ssh_curl``
  (cross-host ``message:send``, bearer = destination host's
  peer-token).

The ``subprocess_shim`` fixture captures the production argv without
mocking ``subprocess.run``; ``env_save_restore`` pins
``SAC_SSH_CONTROL_DIR`` so the ControlPath check is deterministic.
"""

from __future__ import annotations


def test_post_via_ssh_curl_includes_target_host_in_argv(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="{}")
    from scitex_agent_container._network._ssh_curl import _post_via_ssh_curl

    # Act
    _post_via_ssh_curl(
        host="example.invalid",
        port=9999,
        path="/agents/a/message:send",
        body=b"{}",
        bearer=None,
        timeout_s=5,
    )
    argv = subprocess_shim.argv_for("ssh")
    # Assert
    assert "example.invalid" in argv


#: A FAKE, token-shaped value. Never a real credential — these tests assert
#: on its ABSENCE from argv, so it must be distinctive enough that a
#: substring match cannot be satisfied by unrelated text.
_FAKE_BEARER = "sacfakebearer0PLACEHOLDER778899deadbeef"


def test_post_via_ssh_curl_keeps_bearer_out_of_every_argv_element(
    tmp_path, env_save_restore, subprocess_shim
):
    """THE REGRESSION GUARD for the ssh/curl bearer-in-argv disclosure.

    The helper used to splice the token into the remote command as
    ``-H 'Authorization: Bearer <value>'``. That string is an argument of the
    local ``ssh`` process and of the remote shell + ``curl`` processes, and
    ``/proc/<pid>/cmdline`` is world-readable — so every cross-host
    ``message:send`` published the destination's peer-token to every local
    user on BOTH hosts. Measured before the fix: 2 live pids carried a
    token-shaped sentinel; after: 0.

    Asserting over EVERY element (not just ``argv[-1]``) is deliberate: the
    point is that no argument carries the value, wherever a future refactor
    might move it.
    """
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="{}")
    from scitex_agent_container._network._ssh_curl import _post_via_ssh_curl

    # Act
    _post_via_ssh_curl(
        host="example.invalid",
        port=9999,
        path="/agents/a/message:send",
        body=b"{}",
        bearer=_FAKE_BEARER,
        timeout_s=5,
    )
    argv = subprocess_shim.argv_for("ssh")
    leaked = [part for part in argv if _FAKE_BEARER in part]
    # Assert — count, never the matching text.
    assert len(leaked) == 0


def test_post_via_ssh_curl_takes_the_bearer_from_a_curl_config_on_stdin(
    tmp_path, env_save_restore, subprocess_shim
):
    """The remote reads the header from ``curl --config -``, the house shape.

    Matches the in-house precedent set by
    ``_hostsync._push_tokens_io.probe_peer_listen_auth`` rather than
    inventing a second way to hand a live bearer to a remote curl.
    """
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="{}")
    from scitex_agent_container._network._ssh_curl import _post_via_ssh_curl

    # Act
    _post_via_ssh_curl(
        host="example.invalid",
        port=9999,
        path="/agents/a/message:send",
        body=b"{}",
        bearer=_FAKE_BEARER,
        timeout_s=5,
    )
    remote = subprocess_shim.argv_for("ssh")[-1]
    # Assert
    assert "--config -" in remote


def test_post_via_ssh_curl_leaves_the_no_bearer_remote_command_unchanged(
    tmp_path, env_save_restore, subprocess_shim
):
    """``/v1/turn`` carries no token, so its remote command must not move.

    The fix is scoped to the authenticated path precisely so the
    ControlMaster-sharing ``/v1/turn`` transport keeps the exact command it
    has always had.
    """
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="{}")
    from scitex_agent_container._network._ssh_curl import _post_via_ssh_curl

    # Act
    _post_via_ssh_curl(
        host="example.invalid",
        port=9999,
        path="/v1/turn",
        body=b"{}",
        bearer=None,
        timeout_s=5,
    )
    remote = subprocess_shim.argv_for("ssh")[-1]
    # Assert
    assert remote == (
        "curl -sS --max-time 5 -X POST -H 'Content-Type: application/json' "
        "-d @- http://127.0.0.1:9999/v1/turn"
    )


def test_post_via_ssh_curl_omits_authorization_header_when_bearer_is_none(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="{}")
    from scitex_agent_container._network._ssh_curl import _post_via_ssh_curl

    # Act
    _post_via_ssh_curl(
        host="example.invalid",
        port=9999,
        path="/v1/turn",
        body=b"{}",
        bearer=None,
        timeout_s=5,
    )
    remote_curl = subprocess_shim.argv_for("ssh")[-1]
    # Assert
    assert "Authorization" not in remote_curl


def test_post_via_ssh_curl_returns_curl_stdout_bytes_verbatim(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout='{"ok": true}')
    from scitex_agent_container._network._ssh_curl import _post_via_ssh_curl

    # Act
    rc, stdout, _stderr = _post_via_ssh_curl(
        host="example.invalid",
        port=9999,
        path="/v1/turn",
        body=b"{}",
        bearer=None,
        timeout_s=5,
    )
    # Assert
    assert (rc, stdout) == (0, b'{"ok": true}')


def test_post_via_ssh_curl_rejects_a_newline_in_bearer(
    tmp_path, env_save_restore, subprocess_shim
):
    """A newline would spill the token into the request body.

    The bearer is framed as the FIRST LINE of ssh stdin, with the body as the
    remainder, so a value containing a newline would be split across the two
    — sending a truncated header and a corrupted body. Refuse loudly instead.

    (The old single-quote refusal this replaces guarded a shell-quoting
    hazard that no longer exists: the value is not spliced into a shell
    command any more, which is the whole point of the fix.)
    """
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="{}")
    import pytest

    from scitex_agent_container._network._ssh_curl import _post_via_ssh_curl

    # Act
    # Assert
    with pytest.raises(ValueError):
        _post_via_ssh_curl(
            host="example.invalid",
            port=9999,
            path="/v1/turn",
            body=b"{}",
            bearer="line-one\nline-two",
            timeout_s=5,
        )


def test_post_via_ssh_curl_rejects_a_double_quote_in_bearer(
    tmp_path, env_save_restore, subprocess_shim
):
    """``"`` is special to curl's config parser inside a quoted value."""
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="{}")
    import pytest

    from scitex_agent_container._network._ssh_curl import _post_via_ssh_curl

    # Act
    # Assert
    with pytest.raises(ValueError):
        _post_via_ssh_curl(
            host="example.invalid",
            port=9999,
            path="/v1/turn",
            body=b"{}",
            bearer='quoted"token',
            timeout_s=5,
        )


def test_post_via_ssh_curl_refusal_message_withholds_the_bearer_value(
    tmp_path, env_save_restore, subprocess_shim
):
    """The refusal must not print the credential it is refusing.

    A message complaining about an exposed secret, which quotes the secret,
    is its own disclosure — and this one reaches logs.
    """
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="{}")
    from scitex_agent_container._network._ssh_curl import _post_via_ssh_curl

    # Act — catch by hand so the single assertion is the one that matters.
    message = ""
    try:
        _post_via_ssh_curl(
            host="example.invalid",
            port=9999,
            path="/v1/turn",
            body=b"{}",
            bearer=f'{_FAKE_BEARER}"',
            timeout_s=5,
        )
    except ValueError as exc:
        message = str(exc)
    # Assert
    assert _FAKE_BEARER not in message


def test_post_via_ssh_curl_rejects_empty_host_argument(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="{}")
    import pytest

    from scitex_agent_container._network._ssh_curl import _post_via_ssh_curl

    # Act
    # Assert
    with pytest.raises(ValueError):
        _post_via_ssh_curl(
            host="",
            port=9999,
            path="/v1/turn",
            body=b"{}",
            bearer=None,
            timeout_s=5,
        )


def test_post_via_ssh_curl_rejects_non_positive_port_value(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="{}")
    import pytest

    from scitex_agent_container._network._ssh_curl import _post_via_ssh_curl

    # Act
    # Assert
    with pytest.raises(ValueError):
        _post_via_ssh_curl(
            host="example.invalid",
            port=0,
            path="/v1/turn",
            body=b"{}",
            bearer=None,
            timeout_s=5,
        )


def test_post_via_ssh_curl_rejects_path_without_leading_slash(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="{}")
    import pytest

    from scitex_agent_container._network._ssh_curl import _post_via_ssh_curl

    # Act
    # Assert
    with pytest.raises(ValueError):
        _post_via_ssh_curl(
            host="example.invalid",
            port=9999,
            path="agents/no-slash",
            body=b"{}",
            bearer=None,
            timeout_s=5,
        )


def test_post_via_ssh_curl_returns_nonzero_rc_when_shim_exits_one(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=1, stdout="", stderr="ssh: connect refused")
    from scitex_agent_container._network._ssh_curl import _post_via_ssh_curl

    # Act
    rc, _stdout, _stderr = _post_via_ssh_curl(
        host="example.invalid",
        port=9999,
        path="/agents/a/message:send",
        body=b"{}",
        bearer="abc",
        timeout_s=5,
    )
    # Assert
    assert rc == 1


def test_post_via_ssh_curl_returns_stderr_bytes_from_shim(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=255, stdout="", stderr="ssh: handshake failed")
    from scitex_agent_container._network._ssh_curl import _post_via_ssh_curl

    # Act
    _rc, _stdout, stderr = _post_via_ssh_curl(
        host="example.invalid",
        port=9999,
        path="/agents/a/message:send",
        body=b"{}",
        bearer="abc",
        timeout_s=5,
    )
    # Assert
    assert b"handshake failed" in stderr
