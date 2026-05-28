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


def test_post_via_ssh_curl_embeds_bearer_in_remote_curl_when_set(
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
        bearer="secret-token-zz",
        timeout_s=5,
    )
    argv = subprocess_shim.argv_for("ssh")
    remote_curl = argv[-1]
    # Assert
    assert "Authorization: Bearer secret-token-zz" in remote_curl


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


def test_post_via_ssh_curl_rejects_single_quote_in_bearer(
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
            path="/v1/turn",
            body=b"{}",
            bearer="quoted'token",
            timeout_s=5,
        )


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
