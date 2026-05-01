"""SSH remote execution helper for deploying agents to remote machines."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import AgentConfig

logger = logging.getLogger(__name__)


class SSHPreflightError(RuntimeError):
    """Raised when SSH preflight checks fail with actionable guidance."""


class SSHRemote:
    """Helper for executing commands on a remote machine via SSH."""

    SSH_OPTS = [
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
    ]

    @staticmethod
    def _ssh_target(config: AgentConfig) -> str:
        """Return user@host string for display and commands."""
        return f"{config.remote.user}@{config.remote.host}"

    @staticmethod
    def _ssh_base(config: AgentConfig) -> list[str]:
        """Build the base SSH command with options."""
        if config.remote.hops:
            from ._ssh_chain import render_ssh_chain, skip_local_hops

            remaining = skip_local_hops(config.remote.hops)
            cmd = ["ssh"] + SSHRemote.SSH_OPTS
            cmd.extend(render_ssh_chain(remaining))
            return cmd
        # Legacy dict-format path
        cmd = ["ssh"] + SSHRemote.SSH_OPTS
        if config.remote.key:
            cmd += ["-i", config.remote.key]
        if config.remote.port != 22:
            cmd += ["-p", str(config.remote.port)]
        cmd.append(SSHRemote._ssh_target(config))
        return cmd

    @staticmethod
    def _scp_base(config: AgentConfig) -> list[str]:
        """Build the base SCP command with options."""
        cmd = ["scp"] + SSHRemote.SSH_OPTS
        if config.remote.key:
            cmd += ["-i", config.remote.key]
        if config.remote.port != 22:
            cmd += ["-P", str(config.remote.port)]
        return cmd

    @staticmethod
    def _wrap_login_shell(remote_cmd: str) -> str:
        """Wrap a command to run inside a login shell on the remote host.

        This ensures ~/.bashrc, ~/.profile, and PATH modifications are loaded
        so that tools installed via pip --user or pyenv are discoverable.
        """
        escaped = remote_cmd.replace("'", "'\\''")
        return f"bash -l -c '{escaped}'"

    @staticmethod
    def preflight(config: AgentConfig) -> list[tuple[str, bool, str]]:
        """Run preflight checks and return a list of (name, passed, detail).

        Checks: SSH connectivity, screen binary, scitex-agent-container binary,
        python availability, and disk space.

        Uses a single SSH call with batched commands to minimize round-trips,
        which is critical for hosts with slow login shells (e.g., module loads
        in .bashrc taking 30-60s per connection).
        """
        target = SSHRemote._ssh_target(config)
        results: list[tuple[str, bool, str]] = []
        timeout = getattr(config.remote, "timeout", 60)
        host = config.remote.host

        batched_script = (
            "echo '===CHECK_SSH_OK===';"
            "echo '===CHECK_SCREEN_START==='; which screen 2>/dev/null; echo '===CHECK_SCREEN_END===';"
            "echo '===CHECK_SAC_START==='; which scitex-agent-container 2>/dev/null && scitex-agent-container --version 2>/dev/null; echo '===CHECK_SAC_END===';"
            "echo '===CHECK_PYTHON_START==='; python3 --version 2>&1; echo '===CHECK_PYTHON_END===';"
            "echo '===CHECK_DISK_START==='; df -h / 2>/dev/null | awk 'NR==2 {print $5}'; echo '===CHECK_DISK_END==='"
        )

        use_login = getattr(config.remote, "login_shell", True)

        try:
            ssh_cmd = SSHRemote._ssh_base(config)
            if use_login:
                ssh_cmd.append(SSHRemote._wrap_login_shell(batched_script))
            else:
                ssh_cmd.append(batched_script)

            proc = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:  # stx-allow: fallback (reason: file may not exist on first use)
            results.append(
                (
                    "SSH connection",
                    False,
                    f"Cannot SSH to {target}\n"
                    f"  Check: ssh {target} 'echo ok'\n"
                    f"  Fix:   ssh-keygen && ssh-copy-id {target}\n"
                    f"  Error: {exc}",
                )
            )
            return results

        output = proc.stdout or ""

        if "===CHECK_SSH_OK===" in output:
            results.append(("SSH connection", True, "OK"))
        else:
            results.append(
                (
                    "SSH connection",
                    False,
                    f"Cannot SSH to {target}\n"
                    f"  Check: ssh {target} 'echo ok'\n"
                    f"  Fix:   ssh-keygen && ssh-copy-id {target}",
                )
            )
            return results

        def _extract(start_marker: str, end_marker: str) -> str:
            s = output.find(start_marker)
            e = output.find(end_marker)
            if s == -1 or e == -1:
                return ""
            return output[s + len(start_marker) : e].strip()

        # screen binary
        screen_out = _extract("===CHECK_SCREEN_START===", "===CHECK_SCREEN_END===")
        if screen_out and "/" in screen_out:
            results.append(("screen", True, "OK"))
        else:
            results.append(
                (
                    "screen",
                    False,
                    f"GNU screen not installed on {host}\n"
                    f'  Fix: ssh {host} "sudo apt install screen"',
                )
            )

        # scitex-agent-container binary + version
        sac_out = _extract("===CHECK_SAC_START===", "===CHECK_SAC_END===")
        if sac_out and ("scitex-agent-container" in sac_out or "/" in sac_out):
            version = "unknown"
            for line in sac_out.split("\n"):
                line = line.strip()
                if "version" in line.lower() or line.startswith("scitex"):
                    version = line
                    break
                elif "/" in line:
                    continue
            results.append(("scitex-agent-container", True, version))
        else:
            results.append(
                (
                    "scitex-agent-container",
                    False,
                    f"scitex-agent-container not installed on {host}\n"
                    f'  Fix: ssh {host} "pip install scitex-agent-container"',
                )
            )

        # python
        python_out = _extract("===CHECK_PYTHON_START===", "===CHECK_PYTHON_END===")
        if python_out and "Python" in python_out:
            for line in python_out.split("\n"):
                if "Python" in line:
                    results.append(("python", True, line.strip()))
                    break
            else:
                results.append(("python", True, python_out.split("\n")[0].strip()))
        else:
            results.append(("python", False, "python3 not found on remote"))

        # disk space
        disk_out = _extract("===CHECK_DISK_START===", "===CHECK_DISK_END===")
        if disk_out:
            usage = disk_out.split("\n")[0].strip()
            results.append(("disk space", True, f"{usage} used"))
        else:
            results.append(("disk space", True, "unknown"))

        return results

    @staticmethod
    def check_or_raise(config: AgentConfig) -> None:
        """Run preflight checks and raise SSHPreflightError if any fail."""
        results = SSHRemote.preflight(config)
        failures = [(name, detail) for name, passed, detail in results if not passed]
        if failures:
            lines = [f"Preflight check failed for {config.remote.host}:"]
            for name, detail in failures:
                lines.append(f"\nERROR: {name}")
                for line in detail.split("\n"):
                    lines.append(f"  {line}")
            raise SSHPreflightError("\n".join(lines))

    @staticmethod
    def copy_config(config: AgentConfig) -> str:
        """SCP the YAML config to the remote machine. Returns remote path.

        The ``remote`` section is stripped from the copied config so that the
        remote-side ``scitex-agent-container start`` runs locally instead of
        attempting to SSH back to itself (which would cause infinite recursion).
        """
        import yaml as _yaml

        # Per-agent namespaced remote dir to prevent src_CLAUDE.md /
        # src_mcp.json from leaking between agents that share /tmp/.
        # Without namespacing, the most-recent agent's src_* files
        # would be picked up by the next agent's deploy, injecting the
        # wrong identity into its workspace CLAUDE.md (todo#221).
        remote_dir = f"~/.scitex/agent-container/runtime/{config.name}"
        remote_path = f"{remote_dir}/{config.name}.yaml"
        local_path = config.config_path
        if not local_path:
            raise RuntimeError(
                f"Cannot deploy '{config.name}' remotely: config_path is not set"
            )

        logger.info(
            "Copying config to remote: %s -> %s:%s",
            local_path,
            config.remote.host,
            remote_path,
        )
        try:
            with open(local_path) as f:
                raw = _yaml.safe_load(f)
            if isinstance(raw, dict) and "spec" in raw and "remote" in raw["spec"]:
                del raw["spec"]["remote"]
            content = _yaml.dump(raw, default_flow_style=False, sort_keys=False)
            mkdir_cmd = SSHRemote._ssh_base(config) + [
                f"mkdir -p {remote_dir} && rm -f {remote_dir}/src_CLAUDE.md {remote_dir}/src_mcp.json"
            ]
            subprocess.run(mkdir_cmd, capture_output=True, text=True, timeout=30)
            ssh_cmd = SSHRemote._ssh_base(config) + [f"cat > {remote_path}"]
            result = subprocess.run(
                ssh_cmd, input=content, capture_output=True, text=True, timeout=30
            )
        except subprocess.TimeoutExpired:  # stx-allow: fallback (reason: subprocess execution failure)
            t = SSHRemote._ssh_target(config)
            raise RuntimeError(
                f"ERROR: Timed out copying config to {config.remote.host}\n"
                f"  Check: ssh {t} 'echo ok'\n"
                f"  Fix:   ssh-keygen && ssh-copy-id {t}"
            )
        if result.returncode != 0:
            t = SSHRemote._ssh_target(config)
            raise RuntimeError(
                f"ERROR: Failed to copy config to {config.remote.host}\n"
                f"  SSH stderr: {result.stderr.strip()}\n"
                f"  Check: ssh {t} 'echo ok'\n"
                f"  Fix:   ssh-keygen && ssh-copy-id {t}"
            )

        # Copy sibling src_* files (src_CLAUDE.md, src_mcp.json) for v2
        defdir = Path(local_path).parent
        remote_dir = str(Path(remote_path).parent)
        for src_file in ("src_CLAUDE.md", "src_mcp.json"):
            local_src = defdir / src_file
            if local_src.exists():
                remote_src = f"{remote_dir}/{src_file}"
                # stx-allow: fallback (reason: src_* files are optional v2 enhancements; an SSH/IO error copying them must not abort the remote deploy of the primary config)
                try:
                    content_src = local_src.read_text()
                    ssh_cmd_src = SSHRemote._ssh_base(config) + [f"cat > {remote_src}"]
                    subprocess.run(
                        ssh_cmd_src,
                        input=content_src,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    logger.info(
                        "Copied %s to %s:%s",
                        src_file,
                        config.remote.host,
                        remote_src,
                    )
                except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                    logger.warning("Failed to copy %s to remote", src_file)

        return remote_path

    @staticmethod
    def run(
        config: AgentConfig,
        remote_cmd: str,
        timeout: int = 0,
        login_shell: bool | None = None,
    ) -> subprocess.CompletedProcess:
        """Execute a command on the remote host via SSH."""
        if timeout <= 0:
            timeout = getattr(config.remote, "timeout", 60)
        if login_shell is None:
            login_shell = getattr(config.remote, "login_shell", True)
        ssh_cmd = SSHRemote._ssh_base(config)
        if login_shell:
            ssh_cmd.append(SSHRemote._wrap_login_shell(remote_cmd))
        else:
            ssh_cmd.append(remote_cmd)

        logger.info("SSH [%s]: %s", config.remote.host, remote_cmd)
        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:  # stx-allow: fallback (reason: subprocess execution failure)
            t = SSHRemote._ssh_target(config)
            raise RuntimeError(
                f"ERROR: SSH command timed out on {config.remote.host}\n"
                f"  Command: {remote_cmd}\n"
                f"  Check: ssh {t} 'echo ok'"
            )
        if result.returncode != 0:
            logger.warning(
                "SSH command failed on %s (exit %d): %s",
                config.remote.host,
                result.returncode,
                result.stderr.strip(),
            )
        return result

    @staticmethod
    def start(
        config: AgentConfig, no_preflight: bool = False, force: bool = False
    ) -> bool:
        """Deploy and start agent on remote machine.

        If ``force=True``, the remote ``scitex-agent-container start``
        call receives ``--force`` so it stops any existing instance
        before starting fresh. Without this passthrough, ``--force`` on
        the dispatcher side was silently lost at the SSH boundary and
        the remote CLI would reject the start with "already running".
        """
        if not no_preflight:
            SSHRemote.check_or_raise(config)

        remote_path = SSHRemote.copy_config(config)
        start_timeout = getattr(config.remote, "timeout", 120)
        force_flag = " --force" if force else ""
        result = SSHRemote.run(
            config,
            f"scitex-agent-container start{force_flag} {remote_path}",
            timeout=start_timeout,
        )
        if result.returncode != 0:
            screen_name = config.screen_name or f"cld-{config.name}"
            screen_output = ""
            # stx-allow: fallback (reason: diagnostic SSH call to capture screen state can fail independently of the original start failure; the RuntimeError is raised regardless with whatever output was captured)
            try:
                diag = SSHRemote.run(
                    config,
                    f"screen -ls {screen_name} 2>&1; "
                    f"screen -S {screen_name} -X hardcopy /tmp/{screen_name}-diag.txt 2>/dev/null; "
                    f"cat /tmp/{screen_name}-diag.txt 2>/dev/null | tail -30",
                    timeout=30,
                )
                screen_output = diag.stdout.strip()
            except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                screen_output = "(could not capture screen output)"
            raise RuntimeError(
                f"Failed to start agent '{config.name}' on {config.remote.host}\n"
                f"  stderr: {result.stderr.strip()}\n"
                f"  screen output:\n{screen_output}"
            )
        logger.info(
            "Agent '%s' started on remote host %s", config.name, config.remote.host
        )
        return True

    @staticmethod
    def stop(config: AgentConfig) -> bool:
        """Stop agent on remote machine."""
        result = SSHRemote.run(
            config,
            f"scitex-agent-container stop {config.name}",
            timeout=30,
        )
        if result.returncode != 0:
            logger.error(
                "Failed to stop agent '%s' on %s: %s",
                config.name,
                config.remote.host,
                result.stderr.strip(),
            )
            return False
        logger.info(
            "Agent '%s' stopped on remote host %s", config.name, config.remote.host
        )
        return True

    @staticmethod
    def is_running(config: AgentConfig) -> bool:
        """Check if agent is running on remote machine via screen -ls."""
        screen_name = config.screen_name or f"cld-{config.name}"
        # stx-allow: fallback (reason: SSH connectivity may be unavailable when checking status; returning False is correct because an unreachable host means the agent cannot be confirmed running)
        try:
            result = SSHRemote.run(
                config,
                f"screen -ls {screen_name}",
                timeout=30,
            )
            return screen_name in (result.stdout or "")
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            return False

    @staticmethod
    def logs(config: AgentConfig, lines: int = 50) -> str:
        """Get logs from remote agent."""
        result = SSHRemote.run(
            config,
            f"scitex-agent-container logs {config.name} -n {lines}",
            timeout=60,
        )
        if result.returncode != 0:
            return f"[SSH error] {result.stderr.strip()}"
        return result.stdout
