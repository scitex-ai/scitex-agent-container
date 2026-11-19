"""``spec.sandbox`` — the sealed-run contract (parse + validate).

One optional top-level block. When present, every launch of the agent is a
*sealed run*:

* ``egress.allow`` — the ONLY ``host:port`` pairs the agent can reach (HTTP
  CONNECT through a host-side proxy); everything else is refused and logged.
* ``env`` — the container environment is reduced to ``env.allow`` + the
  keys of ``spec.apptainer.env`` + ONE model key (``env.model_key``) + the
  small declared set of SAC launch wiring (:data:`._env_policy.SAC_WIRING`).
  Fleet-default env, fleet-default binds and the host environment never
  reach the container.
* ``run`` — each run gets a fresh writable layer under ``run.root`` (a
  ``run.size``-capped overlay image, a fresh ``$HOME``, ``/tmp`` and
  ``/uvwork``), removed or archived afterwards (``run.after``), and a
  machine-readable ``outcome.json``.

Every sub-key is REQUIRED once the block is present (the explicit-spec
ruling); unknown keys are refused so a typo can never silently weaken the
seal.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

__all__ = [
    "AFTER_MODES",
    "SandboxSpec",
    "parse_allow_entry",
    "parse_run_root",
    "parse_sandbox",
    "parse_size_mb",
]

AFTER_MODES = ("remove", "archive")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
_KEYS = {
    "": {"egress", "env", "run"},
    "egress": {"allow"},
    "env": {"allow", "model_key"},
    "run": {"root", "size", "after"},
}
#: A run root must be durable, host-private disk — never a RAM/tmp mount.
_FORBIDDEN_ROOTS = ("/tmp", "/var/tmp", "/dev/shm", "/run")


@dataclass(frozen=True)
class SandboxSpec:
    """Parsed ``spec.sandbox`` (immutable).

    ``model_key`` is ``None`` only for a plain-command run (``sac sandbox
    exec``, :mod:`._exec`): no harness, so no model key. ``parse_sandbox``
    (an agent spec) always requires it.
    """

    egress_allow: tuple[tuple[str, int], ...]
    env_allow: tuple[str, ...]
    model_key: str | None
    run_root: str
    run_size_mb: int
    run_after: str

    @property
    def egress_allow_strings(self) -> list[str]:
        return [f"{h}:{p}" for h, p in self.egress_allow]


def _fail(path: str, msg: str) -> ValueError:
    return ValueError(f"spec.sandbox{('.' + path) if path else ''}: {msg}")


def _mapping(raw, path: str) -> dict:
    if not isinstance(raw, dict):
        raise _fail(path, f"must be a mapping, got {type(raw).__name__}")
    allowed = _KEYS[path]
    unknown = sorted(set(map(str, raw)) - allowed)
    missing = sorted(allowed - set(map(str, raw)))
    if unknown:
        raise _fail(path, f"unknown keys {unknown}; valid keys: {sorted(allowed)}")
    if missing:
        raise _fail(path, f"missing required keys {missing} (every key is explicit)")
    return raw


def parse_allow_entry(entry) -> tuple[str, int]:
    """``"host:port"`` -> ``(host, port)``. No scheme, path, wildcard or IP range."""
    if not isinstance(entry, str) or not entry.strip():
        raise _fail("egress.allow", f"entry {entry!r} must be a 'host:port' string")
    text = entry.strip().lower()
    host, sep, port = text.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise _fail(
            "egress.allow", f"entry {entry!r} must be 'host:port' (e.g. pypi.org:443)"
        )
    if not _HOST.match(host) or host.replace(".", "").isdigit():
        raise _fail(
            "egress.allow",
            f"entry {entry!r}: host {host!r} must be a plain DNS name "
            "(no scheme, path, wildcard or IP literal)",
        )
    n = int(port)
    if not 1 <= n <= 65535:
        raise _fail("egress.allow", f"entry {entry!r}: port out of range")
    return host, n


def parse_size_mb(size) -> int:
    """``"4G"`` / ``"512M"`` / ``"2048MB"`` -> integer MB (> 0)."""
    m = re.fullmatch(r"\s*(\d+)\s*(M|MB|G|GB)\s*", str(size or ""), re.IGNORECASE)
    if not m:
        raise _fail("run.size", f"{size!r} must be <int>M|MB|G|GB (e.g. '4G')")
    n = int(m.group(1)) * (1024 if m.group(2).upper().startswith("G") else 1)
    if n <= 0:
        raise _fail("run.size", "must be > 0")
    return n


def _names(raw, path: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise _fail(path, f"must be a list of env var names, got {type(raw).__name__}")
    out = []
    for item in raw:
        if not isinstance(item, str) or not _ENV_NAME.match(item):
            raise _fail(path, f"{item!r} is not an environment variable name")
        out.append(item)
    return tuple(dict.fromkeys(out))


def parse_run_root(raw) -> str:
    """An absolute run root on durable disk; tmp/RAM mounts are refused."""
    if not isinstance(raw, str) or not raw.strip():
        raise _fail("run.root", "must be an absolute path string")
    root = os.path.expanduser(os.path.expandvars(raw.strip()))
    if not os.path.isabs(root):
        raise _fail("run.root", f"{raw!r} must be absolute")
    real = os.path.realpath(root)
    for bad in _FORBIDDEN_ROOTS:
        if real == bad or real.startswith(bad + "/"):
            raise _fail(
                "run.root",
                f"{raw!r} resolves to {real!r} under {bad} — per-run layers must "
                "live on durable host disk (e.g. /scratch/$USER/sac-runs), never tmp",
            )
    return os.path.normpath(root)


def parse_sandbox(spec: dict) -> SandboxSpec | None:
    """Parse ``spec.sandbox``; ``None`` when the block is absent or null."""
    raw = spec.get("sandbox")
    if raw is None:
        return None
    top = _mapping(raw, "")
    egress = _mapping(top["egress"], "egress")
    env = _mapping(top["env"], "env")
    run = _mapping(top["run"], "run")

    allow_raw = egress["allow"]
    if not isinstance(allow_raw, list) or not allow_raw:
        raise _fail(
            "egress.allow",
            "must be a NON-EMPTY list of 'host:port' (the model endpoint at least)",
        )
    allow = tuple(dict.fromkeys(parse_allow_entry(e) for e in allow_raw))

    model_key = env["model_key"]
    if not isinstance(model_key, str) or not _ENV_NAME.match(model_key):
        raise _fail(
            "env.model_key", f"{model_key!r} is not an environment variable name"
        )

    after = run["after"]
    if after not in AFTER_MODES:
        raise _fail("run.after", f"{after!r} must be one of {list(AFTER_MODES)}")

    return SandboxSpec(
        egress_allow=allow,
        env_allow=_names(env["allow"], "env.allow"),
        model_key=model_key,
        run_root=parse_run_root(run["root"]),
        run_size_mb=parse_size_mb(run["size"]),
        run_after=after,
    )
