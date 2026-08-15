"""The birth certificate — the COMPILED spec, recorded at launch.

Operator requirement, verbatim (2026-08-14, card sac-v4-layering-
refactor-harness-runtime-inference-20260813): 「起動した後にコンパイル
された最終的なスペックをエージェントが持つようにしてください、この
エージェントはこうして生まれました、という情報です。状態なのでdb に
入れるのがよさそうですよね」.

At the moment :func:`._instances.record_local_instance` mints the
incarnation id, the fully-resolved :class:`AgentConfig` — post-
inheritance, post-defaults, post-conversion, i.e. WHAT ACTUALLY RUNS —
is serialized and written to the ``incarnations`` table keyed by that
id, joining the three settled identities in one row: this INCARNATION
was born of this AGENT from this SPEC at this git commit. This
structurally retires "the spec I read is not the spec sac loads": drift
between a running agent and its on-disk spec becomes a diff of
birth-record vs current compile — a lookup, not an investigation.

SECRETS: credentials are referenced by SLOT/SOURCE NAME, never by
value. Key-pattern redaction scrubs any secret-shaped mapping key
(token / secret / password / credential / bearer / api-key /
private-key) while deliberately KEEPING keys that merely NAME a source
(``*_file`` / ``*_files`` / ``*_path`` / ``*_env`` / ``*_dir`` — an
account slug path or an env-var NAME is the reference the operator
approved recording: 「シークレット配下なのにというのは大丈夫です」).

GIT SHA: the spec's repo HEAD when resolvable; the agents dir may not
be a git repo on this host, and then ``"unresolvable"`` is recorded
honestly rather than faked (the v4 spec-history-in-git migration makes
it resolvable fleet-wide later).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "SPEC_SHA_UNRESOLVABLE",
    "compiled_spec_snapshot",
    "spec_git_sha",
    "write_birth_certificate",
]

#: Recorded when the spec's git commit cannot be resolved — honest
#: absence, never a fabricated sha.
SPEC_SHA_UNRESOLVABLE = "unresolvable"

#: Secret-shaped mapping keys whose VALUES are scrubbed. Matches the
#: key anywhere, case-insensitively.
_SECRET_KEY_RE = re.compile(
    r"(?i)(token|secret|passwd|password|credential|bearer|api[-_]?key|private[-_]?key)"
)

#: Keys that merely NAME a credential source (a path, a file list, an
#: env-var name, a directory) — these ARE the slot/source references the
#: birth record is supposed to carry, so they are exempt from scrubbing.
_SOURCE_REF_KEY_RE = re.compile(r"(?i)(_file|_files|_path|_paths|_env|_dir)s?$")


def _redact(obj: Any) -> Any:
    """Recursively scrub secret-shaped values out of a serialized config.

    A mapping entry whose KEY looks secret-shaped (and does not merely
    name a source) has its value replaced by ``"<redacted:<key>>"`` —
    the key itself survives, so the record still says WHICH slot was
    wired, just not what was in it. Non-mapping containers recurse.
    """
    if isinstance(obj, dict):
        out: dict = {}
        for key, value in obj.items():
            k = str(key)
            if _SECRET_KEY_RE.search(k) and not _SOURCE_REF_KEY_RE.search(k):
                out[key] = f"<redacted:{k}>" if value not in (None, "", [], {}) else value
            else:
                out[key] = _redact(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    return obj


def compiled_spec_snapshot(config: Any) -> dict:
    """The fully-resolved config as a JSON-safe, secret-redacted dict.

    ``dataclasses.asdict`` walks the whole :class:`AgentConfig` tree
    (every nested spec is itself a dataclass); anything non-JSON-native
    left over (Paths, exotic extension values) is stringified by the
    ``default=str`` in the JSON dump downstream. Redaction runs on the
    full tree — including ``spec.env`` values and any ``extensions``
    payload — before anything leaves this function.
    """
    raw = dataclasses.asdict(config)
    return _redact(raw)


def spec_git_sha(config_path: str | None, *, timeout_s: float = 5.0) -> str:
    """The spec repo's HEAD commit, or ``"unresolvable"`` — never a guess.

    Asks git itself (``git -C <spec-dir> rev-parse HEAD``). Every
    failure mode — no config path, no git binary, not a repo, an empty
    repo, a timeout — resolves to :data:`SPEC_SHA_UNRESOLVABLE`;
    recording that honestly beats blocking a launch or faking a sha.
    """
    if not config_path:
        return SPEC_SHA_UNRESOLVABLE
    spec_dir = Path(config_path).expanduser().resolve().parent
    if not spec_dir.is_dir():
        return SPEC_SHA_UNRESOLVABLE
    try:
        proc = subprocess.run(
            ["git", "-C", str(spec_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        return SPEC_SHA_UNRESOLVABLE
    if proc.returncode != 0:
        return SPEC_SHA_UNRESOLVABLE
    sha = proc.stdout.strip()
    return sha if sha else SPEC_SHA_UNRESOLVABLE


def write_birth_certificate(
    config: Any,
    incarnation_id: str,
    *,
    db_path: Path | None = None,
) -> bool:
    """Record the birth certificate for ``incarnation_id``. Best-effort.

    Returns True iff the row was written. A failure is LOGGED with its
    origin and swallowed — the certificate is bookkeeping about a launch
    that already succeeded, and failing the launch over it would destroy
    the very run it documents (same contract as the sibling
    ``record_local_instance`` side-writes).
    """
    try:
        spec_id = (
            getattr(config, "config_path", None)
            or getattr(config, "spec_path", None)
            or None
        )
        snapshot = compiled_spec_snapshot(config)
        payload = json.dumps(snapshot, ensure_ascii=False, default=str)
        from .._state.state_db_incarnations import record_incarnation_birth

        record_incarnation_birth(
            incarnation_id,
            agent_id=str(getattr(config, "name", "") or ""),
            spec_id=str(spec_id) if spec_id else None,
            spec_git_sha=spec_git_sha(spec_id),
            host=None,
            compiled_spec_json=payload,
            db_path=db_path,
        )
        return True
    except Exception as exc:  # stx-allow: fallback (reason: the certificate documents a launch that already succeeded; failing the launch over bookkeeping would destroy the run it documents — logged with origin, never silent)
        logger.error(
            "birth certificate NOT recorded for incarnation %s (agent %s): %s "
            "(origin: _lifecycle/_birth_certificate.write_birth_certificate)",
            incarnation_id,
            getattr(config, "name", "?"),
            exc,
        )
        return False
