"""Fixed host + container layout of a sealed run (pure path arithmetic).

Host::

    <state_dir>/sandbox/run        -> <run.root>/<agent>/<run_id>   (symlink, current run)
    <state_dir>/home               -> <run_dir>/home                (symlink while a run is live)
    <run_dir>/home/                fresh $HOME (/home/agent)
    <run_dir>/overlay/             directory overlay (the writable rootfs layer)
    <run_dir>/workdir/             apptainer --workdir (/tmp, /var/tmp)
    <run_dir>/uvwork/              /uvwork
    <run_dir>/rt/bin/netgw.py      the in-container gateway (bound read-only)
    <run_dir>/rt/sock/             proxy.sock (host-served) + ingress.sock (container-served)
    <run_dir>/secrets/             filtered 0600 env files (NOT mounted; deleted once consumed)
    <run_dir>/out/                 outcome.json, exit_code, egress_*.jsonl (NOT mounted)

``build_run_argv`` (pure) names run paths through the stable
``<state_dir>/sandbox/run`` link; the supervisor rewrites them to the real
run dir before exec, so the container's mount table shows real paths.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "C_BIN",
    "C_INGRESS",
    "C_NETGW",
    "C_PROXY_SOCK",
    "C_SOCK",
    "OUTCOME",
    "RunPaths",
    "run_link",
]

C_ROOT = "/run/sac-sandbox"
C_BIN = f"{C_ROOT}/bin"
C_SOCK = f"{C_ROOT}/sock"
C_NETGW = f"{C_BIN}/netgw.py"
C_PROXY_SOCK = f"{C_SOCK}/proxy.sock"
C_INGRESS = f"{C_SOCK}/ingress.sock"
OUTCOME = "outcome.json"


def run_link(state_dir: Path) -> Path:
    return Path(state_dir).expanduser() / "sandbox" / "run"


class RunPaths:
    """Every path of one run, rooted at ``root`` (real dir or the stable link)."""

    def __init__(self, root: Path):
        self.root = Path(root)

    home = property(lambda s: s.root / "home")
    overlay = property(lambda s: s.root / "overlay")
    workdir = property(lambda s: s.root / "workdir")
    uvwork = property(lambda s: s.root / "uvwork")
    bin = property(lambda s: s.root / "rt" / "bin")
    sock = property(lambda s: s.root / "rt" / "sock")
    secrets = property(lambda s: s.root / "secrets")
    out = property(lambda s: s.root / "out")
    outcome = property(lambda s: s.root / "out" / OUTCOME)
    exit_code = property(lambda s: s.root / "out" / "exit_code")
    proxy_sock = property(lambda s: s.root / "rt" / "sock" / "proxy.sock")
    ingress_sock = property(lambda s: s.root / "rt" / "sock" / "ingress.sock")
