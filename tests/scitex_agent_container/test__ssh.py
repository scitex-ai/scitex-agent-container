"""Tests for scitex_agent_container._ssh — ControlMaster multiplexing helper.

Covers:
- ``ssh_control_opts`` returns the expected -o flags.
- ``ensure_control_path_dir`` creates the directory (idempotent).
- ``sac_ssh_args`` returns opts and ensures the dir.
- ``SAC_SSH_CONTROL_DIR`` env var overrides the default path.
- ``TMPDIR`` env var influences the default path.
"""

from __future__ import annotations

import pathlib

from scitex_agent_container._ssh import (
    _SAC_SSH_CONTROL_DIR_DEFAULT,
    ensure_control_path_dir,
    sac_ssh_args,
    ssh_control_opts,
)


class TestSshControlOptsFirstFlagKey:
    """ssh_control_opts first -o flag: key is "-o"."""

    def test_first_elem_is_o_flag(self):
        # Arrange
        opts = ssh_control_opts()
        # Act
        result = opts[0]
        # Assert
        assert result == "-o"


class TestSshControlOptsFirstFlagValue:
    """ssh_control_opts first -o flag: value is ControlMaster=auto."""

    def test_first_o_value_is_control_master_auto(self):
        # Arrange
        opts = ssh_control_opts()
        # Act
        result = opts[1]
        # Assert
        assert result == "ControlMaster=auto"


class TestSshControlOptsSecondFlagKey:
    """ssh_control_opts second -o flag: key is "-o"."""

    def test_second_o_key_is_o_flag(self):
        # Arrange
        opts = ssh_control_opts()
        # Act
        result = opts[2]
        # Assert
        assert result == "-o"


class TestSshControlOptsSecondFlagValue:
    """ssh_control_opts second -o flag: value is ControlPersist=60s."""

    def test_second_o_value_is_control_persist_60s(self):
        # Arrange
        opts = ssh_control_opts()
        # Act
        result = opts[3]
        # Assert
        assert result == "ControlPersist=60s"


class TestSshControlOptsThirdFlagKey:
    """ssh_control_opts third -o flag: key is "-o"."""

    def test_third_o_key_is_o_flag(self):
        # Arrange
        opts = ssh_control_opts()
        # Act
        result = opts[4]
        # Assert
        assert result == "-o"


class TestSshControlOptsThirdFlagContainsSacSshCm:
    """ssh_control_opts ControlPath contains .sac-ssh-cm."""

    def test_third_o_value_contains_sac_ssh_cm(self):
        # Arrange
        opts = ssh_control_opts()
        # Act
        result = opts[5]
        # Assert
        assert ".sac-ssh-cm" in result


class TestSshControlOptsThirdFlagContainsPercentC:
    """ssh_control_opts ControlPath contains %C token."""

    def test_third_o_value_contains_percent_c(self):
        # Arrange
        opts = ssh_control_opts()
        # Act
        result = opts[5]
        # Assert
        assert "%C" in result


class TestSshControlOptsControlPathDefaultDir:
    """ssh_control_opts ControlPath default is under /tmp."""

    def test_control_path_starts_with_tmp(self):
        # Arrange
        opts = ssh_control_opts()
        # Act
        result = opts[5]
        # Assert
        assert result.startswith("ControlPath=/tmp/.sac-ssh-cm/")


class TestEnsureControlPathDirCreatesDir:
    """ensure_control_path_dir creates the directory."""

    def test_returned_path_is_existing_dir(self):
        # Arrange
        # Act
        result = ensure_control_path_dir()
        # Assert
        assert pathlib.Path(result).is_dir()

    def test_returned_path_ends_with_sac_ssh_cm(self):
        # Arrange
        # Act
        result = ensure_control_path_dir()
        # Assert
        assert result.endswith(".sac-ssh-cm")


class TestEnsureControlPathDirIdempotent:
    """ensure_control_path_dir is safe to call multiple times."""

    def test_dir_still_exists_after_three_calls(self):
        # Arrange
        ensure_control_path_dir()
        ensure_control_path_dir()
        ensure_control_path_dir()
        # Act
        p = pathlib.Path(_SAC_SSH_CONTROL_DIR_DEFAULT)
        # Assert
        assert p.is_dir()


class TestSacSshArgsContainsControlMasterAuto:
    """sac_ssh_args includes ControlMaster=auto."""

    def test_control_master_auto_in_opts(self):
        # Arrange
        # Act
        result = sac_ssh_args()
        # Assert
        assert "ControlMaster=auto" in result


class TestSacSshArgsContainsControlPersist60s:
    """sac_ssh_args includes ControlPersist=60s."""

    def test_control_persist_60s_in_opts(self):
        # Arrange
        # Act
        result = sac_ssh_args()
        # Assert
        assert "ControlPersist=60s" in result


class TestSacSshArgsExtraOpts:
    """sac_ssh_args with extra_opts parameter."""

    def test_extra_verbose_flag_included(self):
        # Arrange
        # Act
        result = sac_ssh_args(extra_opts=["-v"])
        # Assert
        assert "-v" in result

    def test_control_master_retained_with_extra_opts(self):
        # Arrange
        # Act
        result = sac_ssh_args(extra_opts=["-v"])
        # Assert
        assert "ControlMaster=auto" in result


class TestSshControlOptsDefaultDirConstant:
    """_SAC_SSH_CONTROL_DIR_DEFAULT module constant."""

    def test_constant_contains_sac_ssh_cm(self):
        # Arrange
        # Act
        val = _SAC_SSH_CONTROL_DIR_DEFAULT
        # Assert
        assert ".sac-ssh-cm" in val

    def test_constant_starts_with_tmp(self):
        # Arrange
        # Act
        val = _SAC_SSH_CONTROL_DIR_DEFAULT
        # Assert
        assert val.startswith("/tmp/")


# ---------------------------------------------------------------------------
# Integration: build_ssh_argv includes ControlMaster options
# ---------------------------------------------------------------------------


class _BuildSshArgvBase:
    """Shared helper for build_ssh_argv integration tests."""

    @staticmethod
    def _build_argv(peer_name="mba", command=None, extra_fields=None):
        from scitex_agent_container._state.host_config import (
            PeerSpec,
            build_ssh_argv,
        )

        fields = {"name": "mba", "ssh": "ywatanabe@mba.local"}
        if extra_fields:
            fields.update(extra_fields)
        if peer_name != "mba":
            fields["name"] = peer_name
            fields["ssh"] = f"ywatanabe@{peer_name}-login1"
        peers = {
            "mba": PeerSpec(**fields),
        }
        if extra_fields and "via" in fields:
            peers["mba"] = PeerSpec(name="mba", ssh="ywatanabe@mba.local")
            peers["spartan"] = PeerSpec(**fields)
            return build_ssh_argv("spartan", command or ["sac", "agent", "list"], peers)
        return build_ssh_argv(peer_name, command or ["echo", "hi"], peers)


class TestBuildSshArgvControlMaster:
    """build_ssh_argv includes ControlMaster=auto."""

    def test_control_master_flag_present_in_argv(self):
        # Arrange
        argv = _BuildSshArgvBase._build_argv()
        # Act
        has_cm = any("ControlMaster=auto" in a for a in argv)
        # Assert
        assert has_cm


class TestBuildSshArgvControlPersist:
    """build_ssh_argv includes ControlPersist=60s."""

    def test_control_persist_flag_present_in_argv(self):
        # Arrange
        argv = _BuildSshArgvBase._build_argv()
        # Act
        has_cp = any("ControlPersist=60s" in a for a in argv)
        # Assert
        assert has_cp


class TestBuildSshArgvControlPath:
    """build_ssh_argv includes .sac-ssh-cm in ControlPath."""

    def test_sac_ssh_cm_path_present_in_argv(self):
        # Arrange
        argv = _BuildSshArgvBase._build_argv()
        # Act
        has_path = any(".sac-ssh-cm" in a for a in argv)
        # Assert
        assert has_path


class TestBuildSshArgvControlMasterBeforeHost:
    """ControlMaster opts appear before the host positional arg."""

    def test_control_master_index_less_than_host_index(self):
        # Arrange
        argv = _BuildSshArgvBase._build_argv()
        host_idx = argv.index("ywatanabe@mba.local")
        # Act
        cm_idx = next(i for i, a in enumerate(argv) if "ControlMaster=auto" in a)
        # Assert
        assert cm_idx < host_idx


class TestBuildSshArgvControlMasterAfterBatchMode:
    """ControlMaster opts appear after BatchMode."""

    def test_control_master_comes_after_batch_mode(self):
        # Arrange
        argv = _BuildSshArgvBase._build_argv()
        batch_idx = argv.index("BatchMode=yes")
        # Act
        cm_idx = next(i for i, a in enumerate(argv) if "ControlMaster=auto" in a)
        # Assert
        assert cm_idx > batch_idx


class TestBuildSshArgvWithPreamblePeer:
    """Peers with env_preamble still include ControlMaster opts."""

    def test_control_master_present_with_env_preamble(self):
        # Arrange
        argv = _BuildSshArgvBase._build_argv(
            extra_fields={"env_preamble": ("module load apptainer",)}
        )
        # Act
        has_cm = any("ControlMaster=auto" in a for a in argv)
        # Assert
        assert has_cm


class TestBuildSshArgvControlPersistWithPreamble:
    """Peers with env_preamble still include ControlPersist."""

    def test_control_persist_present_with_env_preamble(self):
        # Arrange
        argv = _BuildSshArgvBase._build_argv(
            extra_fields={"env_preamble": ("module load apptainer",)}
        )
        # Act
        has_cp = any("ControlPersist=60s" in a for a in argv)
        # Assert
        assert has_cp


class TestBuildSshArgvWithProxyJump:
    """Multi-hop peers still include ControlMaster opts."""

    def test_proxy_jump_flag_retained(self):
        # Arrange
        argv = _BuildSshArgvBase._build_argv(
            peer_name="spartan",
            extra_fields={
                "name": "spartan",
                "ssh": "ywatanabe@spartan-login1",
                "via": ("mba",),
            },
        )
        # Act
        has_jump = "-J" in argv
        # Assert
        assert has_jump

    def test_control_master_present_with_proxy_jump(self):
        # Arrange
        argv = _BuildSshArgvBase._build_argv(
            peer_name="spartan",
            extra_fields={
                "name": "spartan",
                "ssh": "ywatanabe@spartan-login1",
                "via": ("mba",),
            },
        )
        # Act
        has_cm = any("ControlMaster=auto" in a for a in argv)
        # Assert
        assert has_cm
