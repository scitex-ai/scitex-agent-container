"""``apptainer.binds`` entries that DECLARE their intent (parse side).

WHY THIS EXISTS — a bind entry used to be a bare string, so it said
WHERE to mount and nothing about WHEN. Every entry was therefore
unconditional and mandatory, and a spec that was valid on the operator's
laptop killed the same agent elsewhere: apptainer refuses to start a
container whose bind source is missing ("container creation failed ...
mount source doesn't exist"). Across the fleet's 107 specs the worst
offender is ``/mnt/c`` — present in 101 of them, and WSL-only, so absent
on every Linux host. The fleet's workaround was to COMMENT THE LINE OUT
per host. That hand-hack is what this module replaces.

An entry may now be EITHER shape:

  * a plain ``host:container[:mode]`` STRING — unchanged in every
    respect. It parses to ``BindIntent(spec=..., required=True)``, i.e.
    unconditional and mandatory, and produces byte-identical argv. The
    107 live specs are all this shape and none of them move.

  * a MAPPING that states intent::

        - source: /mnt/c          # host side ( ~ / $VAR expanded)
          dest: /mnt/c            # container side (must be absolute)
          mode: rw                # optional; omitted = no :mode suffix
          required: false         # absent source -> visible skip
          ensure: dir             # create the source dir first
          hosts: [ywata-note-win] # applies only on these hosts

``required: true`` is the DEFAULT and is never implied away: a mapping
that says nothing about it behaves exactly like the string form.

The legacy ``{src, dst, mode}`` dict spelling keeps working — ``src`` and
``dst`` are accepted as aliases of ``source`` and ``dest`` — so no
existing spec has to be rewritten to keep parsing.

WHERE THE LOUD VALIDATION APPLIES, and where it deliberately does NOT
--------------------------------------------------------------------
A malformed DECLARED-INTENT entry is a hard error naming the file, the
entry, the valid keys and a paste-ready correction. A typo such as
``requred: false`` MUST be caught: silently ignoring it would leave
``required: true`` in force, so the operator would read one behaviour off
the spec and get the other.

But entries that declare NO intent keep their existing defensive
tolerance, byte for byte: a non-list ``binds:`` still yields no binds, an
empty-string entry is still skipped, and a legacy dict missing ``src`` or
``dst`` is still dropped (see ``_parsers/test__apptainer.py``, which pins
all three). That is not an oversight — the governing rule for this change
is "no declared intent ⇒ behaves EXACTLY as today", and tightening those
paths would break it in the one direction nobody asked for. Cases where
the intent surface is not involved therefore stay exactly as they were;
the moment an entry uses ``source`` / ``dest`` / ``required`` / ``ensure``
/ ``hosts``, or carries a key from no vocabulary at all, it is held to
the strict contract.

This module owns SHAPE and VALIDATION only. The runtime decisions the
declarations gate (does the source exist? is this that host? create the
directory) belong to launch time, not load time — a ``sac agents list``
on a coordinator must not mkdir on behalf of an agent that runs on
another machine — and live in the sibling
``runtimes/_apptainer_bind_intent``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = [
    "BIND_ENTRY_KEYS",
    "BindIntent",
    "ENSURE_VALUES",
    "parse_bind_entries",
]

# Canonical keys, in the order the paste-ready hint renders them.
_CANONICAL_KEYS: tuple[str, ...] = (
    "source",
    "dest",
    "mode",
    "required",
    "ensure",
    "hosts",
)
# Pre-v3 spelling, still accepted so no live spec has to be rewritten.
_ALIASES: dict[str, str] = {"src": "source", "dst": "dest"}
BIND_ENTRY_KEYS = frozenset(_CANONICAL_KEYS) | frozenset(_ALIASES)

# ``ensure`` values sac knows how to satisfy. ``file`` is deliberately
# absent: accepting it would promise a creation that never happens.
ENSURE_VALUES: tuple[str, ...] = ("", "dir")


@dataclass(frozen=True)
class BindIntent:
    """One declared bind: WHERE it mounts plus WHEN it applies.

    ``spec`` is the fully-resolved ``host:container[:mode]`` string that
    reaches apptainer's ``--bind`` — the source side already expanded, the
    destination already validated. It is the ONLY thing the jail guardrail
    and the fleet-default merge ever need, which is why
    ``ApptainerSpec.binds`` keeps carrying exactly these strings.
    """

    spec: str
    required: bool = True
    ensure: str = ""
    hosts: tuple[str, ...] = ()


def _expand_source(bind_str: str) -> str:
    """Expand ``~`` / ``$VAR`` on the SOURCE side of a bind string.

    apptainer runs no shell, so it does not expand either — a literal
    ``~/x`` reaches ``--bind`` as a relative directory name and the mount
    fails. Only the source side is touched; the destination (and any
    ``:mode`` suffix) stays verbatim, because apptainer rejects a ``~`` or
    ``$VAR`` target outright and we must not paper over that.
    """
    if ":" not in bind_str:
        return os.path.expanduser(os.path.expandvars(bind_str))
    src, _, rest = bind_str.partition(":")
    return f"{os.path.expanduser(os.path.expandvars(src))}:{rest}"


def _where(source_path: str) -> str:
    return f"{source_path}: " if source_path else ""


def _paste_block(entry: dict) -> str:
    """Render ``entry``'s VALID keys as a paste-ready YAML list item."""
    kept = [(k, entry[k]) for k in _CANONICAL_KEYS if k in entry]
    for alias, canon in _ALIASES.items():
        if alias in entry and canon not in entry:
            kept.append((canon, entry[alias]))
    if not kept:
        kept = [("source", "/host/path"), ("dest", "/container/path")]
    lines = [f"  - {kept[0][0]}: {kept[0][1]}"]
    lines += [f"    {k}: {v}" for k, v in kept[1:]]
    return "\n".join(lines)


def _reject(source_path: str, entry: object, problem: str, fix: str) -> ValueError:
    """The house error shape: where, what, why, and a paste-ready fix."""
    return ValueError(
        f"{_where(source_path)}apptainer.binds entry {entry!r}: {problem}\n"
        f"  Valid keys: {sorted(BIND_ENTRY_KEYS)}\n"
        f"  {fix}"
    )


def _corrected(source_path: str, entry: dict, problem: str) -> ValueError:
    return _reject(
        source_path,
        entry,
        problem,
        "Corrected entry:\n" + _paste_block(entry),
    )


def _validate_dest(source_path: str, entry: object, dest: str) -> None:
    """Absolute-path check for the container side (apptainer's rule)."""
    if dest.startswith("~") or dest.startswith("$"):
        raise ValueError(
            f"{_where(source_path)}apptainer.binds entry {entry!r}: "
            f"destination side {dest!r} must be an absolute path (apptainer "
            "does not expand ~ or $VAR on bind targets). Use /home/agent/... "
            "(D5 canonical HOME) or another absolute path."
        )
    if not dest.startswith("/"):
        raise ValueError(
            f"{_where(source_path)}apptainer.binds entry {entry!r}: "
            f"destination side {dest!r} must be absolute."
        )


def _string_entry(source_path: str, item: str) -> BindIntent:
    """A plain ``host:container[:mode]`` string — unconditional, required."""
    if ":" in item:
        _, _, rest = item.partition(":")
        dest = rest.split(":", 1)[0]
        if dest:
            _validate_dest(source_path, item, dest)
    return BindIntent(spec=_expand_source(item))


def _pick(source_path: str, entry: dict, canon: str) -> object:
    """Read ``canon`` honouring its legacy alias; refuse both at once."""
    alias = next((a for a, c in _ALIASES.items() if c == canon), None)
    if alias is not None and alias in entry and canon in entry:
        raise _corrected(
            source_path,
            entry,
            f"declares both {canon!r} and its legacy alias {alias!r} — "
            f"keep one (prefer {canon!r})",
        )
    if canon in entry:
        return entry[canon]
    if alias is not None:
        return entry.get(alias)
    return None


def _required_of(source_path: str, entry: dict) -> bool:
    value = entry.get("required")
    if value is None:
        return True
    if not isinstance(value, bool):
        raise _corrected(
            source_path,
            entry,
            f"'required' must be a YAML boolean (true / false), got "
            f"{value!r}. Quoted 'false' is a non-empty STRING and would "
            "read as the opposite of what it says",
        )
    return value


def _ensure_of(source_path: str, entry: dict) -> str:
    value = entry.get("ensure")
    if value is None:
        return ""
    ensure = str(value).strip()
    if ensure not in ENSURE_VALUES:
        raise _corrected(
            source_path,
            entry,
            f"'ensure' must be one of {list(ENSURE_VALUES)}, got {value!r}",
        )
    return ensure


def _hosts_of(source_path: str, entry: dict) -> tuple[str, ...]:
    value = entry.get("hosts")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _corrected(
            source_path,
            entry,
            f"'hosts' must be a LIST of host names, got {type(value).__name__} "
            f"{value!r}. A bare string matches no host, so the bind would "
            "silently skip everywhere",
        )
    hosts = tuple(str(h).strip() for h in value if str(h).strip())
    if not hosts:
        raise _corrected(
            source_path,
            entry,
            "'hosts' is empty — that declares a bind which can never apply "
            "on any host. Drop the key to apply everywhere, or name the "
            "hosts it belongs to",
        )
    return hosts


def _legacy_entry(entry: dict) -> BindIntent | None:
    """The pre-intent ``{src, dst, mode}`` dict, byte-for-byte as before.

    Reached only when the mapping uses NO key outside ``{src, dst, mode}``,
    so it cannot be declaring intent. Kept verbatim — including the silent
    drop of an entry missing either side, which
    ``_parsers/test__apptainer.py`` pins as deliberate defensive coercion.
    Returns ``None`` for "dropped".
    """
    src = str(entry.get("src", "") or "")
    dst = str(entry.get("dst", "") or "")
    mode = str(entry.get("mode", "") or "")
    if not (src and dst):
        return None
    if dst.startswith("~") or dst.startswith("$") or not dst.startswith("/"):
        raise ValueError(
            f"apptainer.binds dict {entry!r}: dst {dst!r} "
            "must be an absolute path (apptainer rejects "
            "~/$VAR/relative bind targets)."
        )
    src = os.path.expanduser(os.path.expandvars(src))
    return BindIntent(spec=f"{src}:{dst}:{mode}" if mode else f"{src}:{dst}")


def _mapping_entry(source_path: str, entry: dict) -> BindIntent:
    """A declared-intent mapping. Every key is validated; typos are loud."""
    unknown = sorted(k for k in entry if k not in BIND_ENTRY_KEYS)
    if unknown:
        raise _corrected(
            source_path,
            entry,
            f"unknown key(s) {unknown}. A typo here is silent otherwise: "
            "the intent you meant to declare would never take effect",
        )
    source = _pick(source_path, entry, "source")
    dest = _pick(source_path, entry, "dest")
    if source is None or not str(source).strip():
        raise _corrected(source_path, entry, "'source' is required (the host path)")
    if dest is None or not str(dest).strip():
        raise _corrected(
            source_path, entry, "'dest' is required (the container path)"
        )
    source = str(source).strip()
    dest = str(dest).strip()
    _validate_dest(source_path, entry, dest)
    mode = str(entry.get("mode") or "").strip()
    src = os.path.expanduser(os.path.expandvars(source))
    return BindIntent(
        spec=f"{src}:{dest}:{mode}" if mode else f"{src}:{dest}",
        required=_required_of(source_path, entry),
        ensure=_ensure_of(source_path, entry),
        hosts=_hosts_of(source_path, entry),
    )


def parse_bind_entries(binds_raw: object, *, source_path: str = "") -> list[BindIntent]:
    """Parse ``spec.apptainer.binds`` into one :class:`BindIntent` per entry.

    ``source_path`` is the spec file the entries came from; it is quoted
    in every error so a fleet-wide failure names the file to edit rather
    than sending the operator through 107 of them.

    The pre-intent defensive coercions are preserved EXACTLY (see the
    module docstring): a non-list ``binds:`` yields no binds, an
    empty-string entry is skipped, a legacy ``{src, dst}`` dict missing
    either side is dropped, and a non-string non-mapping item is ignored.
    Strictness starts where the intent vocabulary does.
    """
    if not isinstance(binds_raw, list):
        return []
    out: list[BindIntent] = []
    for item in binds_raw:
        if isinstance(item, str) and item:
            out.append(_string_entry(source_path, item))
        elif isinstance(item, dict):
            # Only-legacy keys ⇒ cannot be declaring intent ⇒ old path.
            if set(item).issubset(_ALIASES.keys() | {"mode"}):
                legacy = _legacy_entry(item)
                if legacy is not None:
                    out.append(legacy)
            else:
                out.append(_mapping_entry(source_path, item))
    return out
