"""``sac whoami`` — in-container self-orientation (where / how / role / how-to).

Every sac agent must be able to answer, from INSIDE its own container:
(1) WHERE am I, (2) HOW am I being run, (3) WHAT is my role, and
(4) HOW do I drive sac. This verb answers from the only two sources a
real agent container actually has — the sac-injected environment and
(when resolvable through the injected spec search path) its own
``spec.yaml``. It never assumes the host registry is readable: the
in-container ``$HOME`` is ``/home/agent`` and shadows the host home, so
any fact that cannot be derived renders an honest ``UNKNOWN``
(``null`` under ``--json``) instead of a guess.

Environment facts surveyed (injection sites, for the record):

* ``SAC_NAME`` — agent self-name (``runtimes/_apptainer_listen_env.py``);
  ``SCITEX_AGENT_CONTAINER_AGENT`` / ``CLAUDE_AGENT_ID`` — the same name
  via the spec auto-env (``config/_loaders.py``).
* ``SCITEX_TODO_AGENT_ID`` — board identity (authored in ``spec.env``).
* ``SCITEX_AGENT_CONTAINER_MODEL`` — display model (spec auto-env).
* ``SAC_LISTEN_BASE_URL`` / ``SAC_LISTEN_BEARER`` — host control-plane
  URL + bearer (``_apptainer_listen_env.py``). The bearer's VALUE is
  never echoed — presence only.
* ``SCITEX_AGENT_CONTAINER_YAML_DIRS`` — the spec search path that makes
  the HOST-side ``spec.yaml`` resolvable in-container (the host home is
  bind-visible even though ``$HOME`` differs).
* ``SCITEX_AGENT_CONTAINER_STATE_DB`` — per-agent state DB
  (``runtimes/_apptainer_build_argv.py``).
* ``APPTAINER_CONTAINER`` / ``SINGULARITY_CONTAINER`` — image path, set
  by apptainer itself.

Secret hygiene: no credential value is ever printed. The only
secret-bearing variable this module touches (``SAC_LISTEN_BEARER``) is
reported as ``set`` / ``unset``.
"""

from __future__ import annotations

import json as json_mod
import os
import socket
from pathlib import Path

import click

#: Text rendering for a fact we cannot derive in-container. JSON uses null.
UNKNOWN = "UNKNOWN"

#: Mount-point prefixes worth showing (kept to a few lines by design).
_MOUNT_PREFIXES = ("/home", "/work", "/uvwork", "/state")
_MOUNT_MAX_ROWS = 6


# ---------------------------------------------------------------------------
# env facts
# ---------------------------------------------------------------------------


def _env_pair(suffix: str) -> str | None:
    """Read a sac-owned env var (both prefixes honoured), never raising.

    A short/long conflict is itself a diagnostic fact: render it as an
    explicit ``CONFLICT(...)`` marker naming the two variables — names
    only, never their values.
    """
    from .._env import SacEnvConflict, aliases, getenv

    try:
        value = getenv(suffix)
    except SacEnvConflict:
        short_name, long_name = aliases(suffix)
        return f"CONFLICT({short_name} != {long_name})"
    return value or None


def _agent_name() -> str | None:
    """Effective agent name from the injected env (None = underivable)."""
    return (
        _env_pair("NAME")
        or _env_pair("AGENT")
        or (os.environ.get("CLAUDE_AGENT_ID") or None)
    )


def _canonical_host() -> str | None:
    """Canonical host label via the shared resolver; None when unknowable."""
    # stx-allow: fallback (reason: resolve_hostname pulls scitex_config +
    # reads config.yaml; a degraded container without them must still get
    # a whoami answer — the raw hostname is reported separately.)
    try:
        from ..config import resolve_hostname

        return resolve_hostname() or None
    except Exception:  # stx-allow: fallback (reason: see above)
        return None


def _image_path() -> str | None:
    """Container image path from apptainer's own env (None off-container)."""
    return (
        os.environ.get("APPTAINER_CONTAINER")
        or os.environ.get("SINGULARITY_CONTAINER")
        or None
    )


def _key_mounts() -> list[str] | None:
    """Few-line ``/proc/mounts`` digest for the key destinations.

    Returns ``None`` when ``/proc/mounts`` is unreadable (never raises).
    Each row is ``<mountpoint> (<fstype>, ro|rw)``; capped at
    ``_MOUNT_MAX_ROWS`` with a ``(+N more)`` tail.
    """
    # stx-allow: fallback (reason: /proc may be absent or masked in exotic
    # sandboxes; mounts are a nice-to-have PLACEMENT hint, not a hard fact.)
    try:
        raw = Path("/proc/mounts").read_text()
    except OSError:
        return None
    rows: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        _dev, mountpoint, fstype, options = parts[0], parts[1], parts[2], parts[3]
        if not mountpoint.startswith(_MOUNT_PREFIXES):
            continue
        if mountpoint in seen:
            continue
        seen.add(mountpoint)
        mode = "ro" if "ro" in options.split(",") else "rw"
        rows.append(f"{mountpoint} ({fstype}, {mode})")
    if len(rows) > _MOUNT_MAX_ROWS:
        extra = len(rows) - _MOUNT_MAX_ROWS
        rows = rows[:_MOUNT_MAX_ROWS] + [f"(+{extra} more)"]
    return rows


# ---------------------------------------------------------------------------
# spec facts (resolvable in-container thanks to the injected YAML_DIRS)
# ---------------------------------------------------------------------------


def _spec_name_candidates(name: str, host: str | None) -> list[str]:
    """Spec-dir names to try for an effective agent name.

    Multi-host specs compose the effective name as ``<dir>-<HOST>``
    (``config/_loaders.compose_effective_name``), so the bare dir name is
    retried when the effective name carries the canonical-host suffix.
    """
    candidates = [name]
    if host and name.endswith(f"-{host}") and name != f"-{host}":
        candidates.append(name[: -len(f"-{host}")])
    return candidates


def _resolve_spec(name: str | None, host: str | None):
    """Resolve ``(spec_path, raw_v3_dict)`` for this agent, never raising.

    Uses the same search chain as ``sac agents start`` — which works
    in-container because the launcher injects
    ``SCITEX_AGENT_CONTAINER_YAML_DIRS`` pointing back at the host
    registry (bind-visible). Returns ``(None, None)`` when the name is
    unknown or nothing resolves; ``(path, None)`` when the file resolved
    but did not parse.
    """
    if not name:
        return None, None
    from ..config._resolve import resolve_config

    path: str | None = None
    for candidate in _spec_name_candidates(name, host):
        # stx-allow: fallback (reason: whoami is a diagnostic — a missing /
        # ambiguous registry must degrade to UNKNOWN, never crash the verb.)
        try:
            path = resolve_config(candidate)
            break
        except Exception:  # stx-allow: fallback (reason: see above)
            continue
    if path is None:
        return None, None
    # stx-allow: fallback (reason: an unparseable spec still has a useful
    # path to report; role facts then degrade to UNKNOWN.)
    try:
        import yaml

        raw = yaml.safe_load(Path(path).read_text())
    except Exception:  # stx-allow: fallback (reason: see above)
        return path, None
    return path, raw if isinstance(raw, dict) else None


def _role_from_spec(raw: dict) -> dict:
    """Project role facts from the raw v3 dict via the shared identity seam."""
    # stx-allow: fallback (reason: keep whoami alive even if the a2a package
    # import chain is broken in a degraded container; role then reads the
    # labels directly — same fields, narrower projection.)
    try:
        from ..a2a._card_identity import spec_identity

        return spec_identity(raw)
    except Exception:  # stx-allow: fallback (reason: see above)
        labels = (raw.get("metadata") or {}).get("labels") or {}
        if not isinstance(labels, dict):
            return {}
        out = {}
        for key in ("role", "purpose"):
            value = labels.get(key)
            if isinstance(value, str) and value.strip():
                out[key] = value.strip()
        return out


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------


def collect_whoami() -> dict:
    """Gather every fact into the ``--json`` shape (None = unknown)."""
    name = _agent_name()
    canonical = _canonical_host()
    spec_path, raw = _resolve_spec(name, canonical)
    spec = (raw.get("spec") or {}) if isinstance(raw, dict) else {}
    if not isinstance(spec, dict):
        spec = {}
    claude_spec = spec.get("claude") if isinstance(spec.get("claude"), dict) else {}

    model = _env_pair("MODEL")
    model_source = "env" if model else None
    if not model and claude_spec.get("model"):
        model = str(claude_spec["model"])
        model_source = "spec"

    a2a_raw = spec.get("a2a") if isinstance(spec.get("a2a"), dict) else {}
    a2a_port = a2a_raw.get("port", "auto") if raw is not None else None

    board_id = os.environ.get("SCITEX_TODO_AGENT_ID") or None
    listen_url = _env_pair("LISTEN_BASE_URL")

    role = _role_from_spec(raw) if raw else {}
    role_source = "spec" if role else None
    if not role:
        env_role = os.environ.get("CLAUDE_AGENT_ROLE") or _env_pair("ROLE")
        if env_role:
            role = {"role": env_role}
            role_source = "env"

    return {
        "identity": {
            "agent": name,
            "board_id": board_id,
            "hostname": socket.gethostname(),
            "canonical_host": canonical,
        },
        "placement": {
            "image": _image_path(),
            "workdir": str(Path.cwd()),
            "mounts": _key_mounts(),
        },
        "execution": {
            "runtime": (str(spec["runtime"]) if spec.get("runtime") else None),
            "model": model,
            "model_source": model_source,
            "spec_path": spec_path,
            "listen_url": listen_url,
            "listen_bearer": "set" if _env_pair("LISTEN_BEARER") else "unset",
            "a2a_port": a2a_port,
            "state_db": _env_pair("STATE_DB")
            or os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
            or None,
        },
        "role": {**role, "source": role_source},
        "howto": _howto_lines(board_id),
    }


def _howto_lines(board_id: str | None) -> list[str]:
    """The essential verbs + the emergency path (env-var NAMES, no values)."""
    scope = board_id or "<board-id>"
    return [
        "cards: scitex-todo MCP — add_task / update_task / complete_task; "
        f'card BEFORE working, scope="agent:{scope}"',
        'dm: scitex-todo MCP dm_send, or `sac peer post-turn <agent> "<text>"`',
        "fleet: `sac agents list` (specs via $SCITEX_AGENT_CONTAINER_YAML_DIRS)",
        "emergency host access: curl -sS -X POST "
        '-H "Authorization: Bearer $SAC_LISTEN_BEARER" '
        '"$SAC_LISTEN_BASE_URL/v1/host_exec" (env vars are pre-injected)',
        "skills: ~/.claude/skills/scitex/scitex-agent-container/",
    ]


# ---------------------------------------------------------------------------
# text rendering
# ---------------------------------------------------------------------------


def _kv(key: str, value, note: str = "") -> str:
    unknown = value in (None, "")
    shown = UNKNOWN if unknown else str(value)
    suffix = f"  ({note})" if (note and not unknown) else ""
    return f"  {key:<11}{shown}{suffix}"


def render_whoami_text(facts: dict) -> str:
    """Render the collected facts as the compact plain-text report."""
    identity = facts["identity"]
    placement = facts["placement"]
    execution = facts["execution"]
    role = facts["role"]

    lines: list[str] = ["IDENTITY"]
    lines.append(_kv("agent:", identity["agent"], "SAC_NAME"))
    lines.append(_kv("board-id:", identity["board_id"], "SCITEX_TODO_AGENT_ID"))
    lines.append(_kv("hostname:", identity["hostname"]))
    lines.append(_kv("host:", identity["canonical_host"], "canonical"))

    lines.append("PLACEMENT")
    lines.append(_kv("image:", placement["image"], "APPTAINER_CONTAINER"))
    lines.append(_kv("workdir:", placement["workdir"]))
    mounts = placement["mounts"]
    if not mounts:
        none_note = "(none under /home,/work,/uvwork,/state)"
        lines.append(_kv("mounts:", None if mounts is None else none_note))
    else:
        for idx, row in enumerate(mounts):
            lines.append(_kv("mounts:" if idx == 0 else "", row))

    lines.append("EXECUTION")
    lines.append(_kv("runtime:", execution["runtime"], "spec"))
    model_note = {"env": "SCITEX_AGENT_CONTAINER_MODEL", "spec": "spec"}.get(
        execution["model_source"] or "", ""
    )
    lines.append(_kv("model:", execution["model"], model_note))
    lines.append(_kv("spec:", execution["spec_path"], "host-side path"))
    lines.append(_kv("listen:", execution["listen_url"], "SAC_LISTEN_BASE_URL"))
    lines.append(
        _kv(
            "bearer:",
            execution["listen_bearer"],
            "SAC_LISTEN_BEARER — value never shown",
        )
    )
    a2a_port = execution["a2a_port"]
    if a2a_port == "auto":
        a2a_port = "auto (allocated at start)"
    elif a2a_port is None and execution["spec_path"]:
        a2a_port = "disabled"
    lines.append(_kv("a2a-port:", a2a_port))
    lines.append(_kv("state-db:", execution["state_db"]))

    lines.append("ROLE")
    if role.get("role") or role.get("purpose"):
        source = role.get("source") or ""
        role_value = role.get("role")
        if isinstance(role_value, list):
            role_value = ", ".join(role_value)
        lines.append(_kv("role:", role_value, source))
        if role.get("purpose"):
            lines.append(_kv("purpose:", role["purpose"]))
        if role.get("groups"):
            lines.append(_kv("groups:", ", ".join(role["groups"])))
        for idx, item in enumerate(role.get("responsibilities") or []):
            lines.append(_kv("resp:" if idx == 0 else "", item))
    else:
        lines.append(
            f"  role:      {UNKNOWN} — spec not resolvable in-container; "
            "see the agent block in ./.claude/CLAUDE.md"
        )

    lines.append("HOW-TO")
    for row in facts["howto"]:
        lines.append(f"  {row}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# click surface
# ---------------------------------------------------------------------------


@click.command("whoami")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the facts as JSON (null = unknown).",
)
@click.pass_context
def whoami(ctx: click.Context, as_json: bool) -> None:
    """Who am I? Answerable from INSIDE the agent container.

    \b
    Sections:
      IDENTITY   agent name, board id, hostname (+ canonical host)
      PLACEMENT  container image, workdir, key mounts
      EXECUTION  runtime, model, spec path, listen URL, bearer presence
      ROLE       labels.role / purpose / groups / responsibilities
      HOW-TO     the essential sac verbs + the emergency path

    Facts come from the sac-injected environment and, when resolvable
    through $SCITEX_AGENT_CONTAINER_YAML_DIRS, the agent's own spec.yaml.
    Underivable facts render an honest UNKNOWN (null under --json); no
    secret value (e.g. the listen bearer) is ever printed.
    """
    from ._helpers import _json_flag

    facts = collect_whoami()
    if _json_flag(ctx, as_json):
        click.echo(json_mod.dumps(facts, indent=2))
        return
    click.echo(render_whoami_text(facts))


__all__ = ["whoami", "collect_whoami", "render_whoami_text"]
