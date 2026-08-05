"""Classify ``config.yaml`` by OBSERVED state, so MISSING cannot read as VALID.

INCIDENT 2026-07-30 (reported by scitex-cards, reproduced here): ``sac host
validate`` returned ``{"source": "/home/agent/.scitex/agent-container/
config.yaml", "errors": []}`` while that file did not exist. The operator's real
config — four peers — lives under their own home. Every ``host probe`` then
failed with "peer is not defined in config.yaml" while the validator reported
the configuration clean.

The mechanism is in :func:`..host_config.load`, which maps a missing file onto
the same defaults as a present one::

    if not p.is_file():
        return Config(source_path=p)

So downstream sees a valid-looking Config with zero peers. An absent config has
no schema to violate, which means **the one condition that actually breaks
multi-host is the one condition the check could not fail on** — a gate that
cannot fail is not a gate.

WHY A SEPARATE MODULE: ``host_config.py`` is the loader and is already past the
512-line limit; the diagnostic is a distinct responsibility (classify, do not
parse-into-a-model), so it lands here rather than growing that file.

DESIGN — one branch per observed state, each naming the resolved path. This
deliberately mirrors :func:`.._account.quota_cache.diagnose_quota_cache` so the
codebase has ONE idiom for representing missing state instead of a fresh
invention at each site. States are kept distinct because they warrant different
responses: ``absent`` is an error, whereas ``empty`` (present, no ``peers:``) is
the legitimate single-host install. Collapsing those two is the original defect.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .host_config import _default_config_path

#: config.yaml is not there at all — peer resolution cannot work.
STATE_ABSENT = "absent"
#: Present but unusable (a directory, bad permissions, unreadable device).
STATE_UNREADABLE = "unreadable"
#: Present and readable but not parseable as a top-level mapping.
STATE_MALFORMED = "malformed"
#: Present and valid, but defines no peers — a legitimate single-host install.
STATE_EMPTY = "empty"
#: Present with at least one peer.
STATE_POPULATED = "populated"


def describe_config_resolution(path: Path | None = None) -> dict[str, str | None]:
    """Report WHY a config path was chosen, so an absent file is explainable.

    ``$HOME`` is included deliberately: it is the discriminator for the failure
    this module exists to make legible. User-scope resolution is HOME-derived,
    and a container's ``$HOME`` is per-container (``/home/agent``), so a config
    written under the operator's home is simply not on the path the cascade
    computes inside a container. Reporting the resolved path alone leaves the
    reader unable to tell a typo from a HOME mismatch — which is exactly the
    ambiguity that cost time on 2026-07-30.

    ``SCITEX_AGENT_CONTAINER_CONFIG`` and ``SCITEX_DIR`` are reported because
    both are honoured by the resolver and neither is injected into agent
    containers today; naming them tells a reader which lever to pull. (Note for
    anyone chasing this: ``SCITEX_AGENT_CONTAINER_HOME`` appears in prose but no
    code reads it — injecting it changes nothing.)
    """
    return {
        "resolved": str(Path(path) if path else _default_config_path()),
        "override_env": "SCITEX_AGENT_CONTAINER_CONFIG",
        "override_value": os.environ.get("SCITEX_AGENT_CONTAINER_CONFIG"),
        "scitex_dir_env": os.environ.get("SCITEX_DIR"),
        "home": os.environ.get("HOME"),
    }


#: Where per-user homes live. Injectable ONLY so the check is testable against
#: a tmp tree — a hardcoded ``/home`` would make this function unreachable from
#: a test, and an untested guard is the kind that quietly stops firing.
HOMES_ROOT = Path("/home")


def find_shadowing_configs(
    resolved: Path, homes_root: Path | None = None
) -> list[tuple[Path, int]]:
    """Other readable ``config.yaml`` files under a DIFFERENT home.

    Returns ``[(path, peer_count), ...]``, empty when there is nothing to
    warn about.

    INCIDENT 2026-08-05 (scitex-dev, reproduced from their report): they ran
    ``sac host add`` twice inside a container and then ``sac host validate``,
    which answered ``ok, valid (2 peers)``. The rows had landed in a 127-byte
    ``/home/agent/.scitex/agent-container/config.yaml`` created that same day,
    while the operator's real four-peer config under ``/home/ywatanabe`` — WHICH
    IS BIND-MOUNTED AND READABLE FROM INSIDE THE CONTAINER — was never touched.
    Both the add and the validate reported success about the wrong file, and
    the card was reported CLOSED on the strength of it, then retracted.

    This is NOT the 2026-07-30 case this module was written for. That one was
    ABSENT — no file at the resolved path — and is already caught. Here the
    file EXISTS and is well-formed, so every state check passes: the resolved
    config is a legitimate-looking config that simply is not the fleet's.
    ``STATE_POPULATED`` emitted no diagnostic at all, so the one shape that
    silently diverges from fleet state was the one shape we said nothing about.

    THE DISCRIMINATOR WAS ALREADY ON SCREEN AND BOTH OF US WALKED PAST IT: the
    resolved config had 2 peers, the operator's had 4. The COUNT named the
    split. That is why this returns the peer count per candidate rather than
    just the paths — a reader comparing "2" against "4" sees it instantly,
    whereas two paths side by side still look like a choice someone made.

    Deliberately CONSERVATIVE, because a false positive here trains people to
    ignore the warning:
      * only ``/home/*/.scitex/agent-container/config.yaml`` — the one layout
        the bind actually produces; no unbounded filesystem walk;
      * only files that PARSE and are readable;
      * never the resolved path itself, and never a path that resolves to the
        same file (a symlink or bind alias is not a shadow — measured today,
        the live and dotfiles spec paths share one inode).
    """
    try:
        resolved_real = resolved.resolve()
    except OSError:
        resolved_real = resolved

    root = homes_root if homes_root is not None else HOMES_ROOT
    out: list[tuple[Path, int]] = []

    # Only meaningful for a HOME-DERIVED resolution. If the caller pointed at an
    # explicit path outside the homes root (SCITEX_AGENT_CONTAINER_CONFIG, a
    # test fixture, a checkout-scoped config), they named the file deliberately
    # and a "did you mean another home?" warning is noise. Skipping here also
    # keeps the check hermetic: without it, every unit test that builds a config
    # in tmp_path picks up whatever the real /home happens to hold, so the
    # diagnosis would depend on the machine the suite runs on.
    try:
        resolved_real.relative_to(root.resolve())
    except (ValueError, OSError):
        return out

    try:
        candidates = sorted(root.glob("*/.scitex/agent-container/config.yaml"))
    except OSError:
        return out

    for cand in candidates:
        try:
            if cand.resolve() == resolved_real:
                continue  # same file by another name — not a shadow
            parsed = yaml.safe_load(cand.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(parsed, dict):
            continue
        peers = parsed.get("peers")
        out.append((cand, len(peers) if isinstance(peers, dict) else 0))
    return out


def diagnose_host_config(path: Path | None = None) -> tuple[str, int, Path]:
    """Return ``(state, peer_count, resolved_path)`` for config.yaml.

    ``state`` is one of the ``STATE_*`` constants in this module. ``peer_count``
    is the number of entries under ``peers:`` and is 0 for every state other
    than :data:`STATE_POPULATED`.
    """
    p = Path(path) if path else _default_config_path()
    try:
        raw_text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (STATE_ABSENT, 0, p)
    except OSError:
        return (STATE_UNREADABLE, 0, p)

    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError:
        return (STATE_MALFORMED, 0, p)

    if parsed is None:
        # Present but empty file: valid single-host, not a configuration error.
        return (STATE_EMPTY, 0, p)
    if not isinstance(parsed, dict):
        return (STATE_MALFORMED, 0, p)

    peers = parsed.get("peers")
    if peers is None:
        return (STATE_EMPTY, 0, p)
    if not isinstance(peers, dict):
        return (STATE_MALFORMED, 0, p)
    return (STATE_POPULATED if peers else STATE_EMPTY, len(peers), p)


def config_state_problems(
    path: Path | None = None,
) -> tuple[list[str], list[str], dict[str, object]]:
    """Turn a diagnosis into ``(errors, warnings, detail)`` for a CLI caller.

    Split rather than a single list because the exit code depends on it: an
    absent or unparseable config must FAIL, while a present config with no
    peers must not — a single-host install is a supported configuration, and
    failing it would just teach operators to ignore the check.
    """
    state, peer_count, resolved = diagnose_host_config(path)
    resolution = describe_config_resolution(path)
    detail: dict[str, object] = {
        "state": state,
        "peers": peer_count,
        "resolution": resolution,
    }
    errors: list[str] = []
    warnings: list[str] = []

    if state == STATE_ABSENT:
        errors.append(
            f"config.yaml NOT FOUND at {resolved} — peer resolution cannot "
            f"work, so every `sac host probe` will report its peer as "
            f"undefined. This path is HOME-derived (HOME="
            f"{resolution['home']!r}); inside a container HOME is "
            f"per-container, so a config written under another home is not on "
            f"this path. Point at it explicitly with "
            f"SCITEX_AGENT_CONTAINER_CONFIG=/path/to/config.yaml, or set "
            f"SCITEX_DIR to the state root that holds it."
        )
    elif state == STATE_UNREADABLE:
        errors.append(
            f"config.yaml at {resolved} exists but could not be read "
            f"(directory, or permissions)."
        )
    elif state == STATE_MALFORMED:
        errors.append(
            f"config.yaml at {resolved} is not a valid top-level mapping with "
            f"a mapping `peers:` block."
        )
    elif state == STATE_EMPTY:
        warnings.append(
            f"config.yaml at {resolved} defines no peers — correct for a "
            f"single-host install, but multi-host commands will report every "
            f"peer as undefined."
        )

    # A PRESENT, well-formed config can still be the WRONG ONE. Every state
    # above describes the resolved file on its own terms; none of them can see
    # that a different home holds the fleet's real config. Checked for every
    # state, including POPULATED — that is the shape that says nothing and
    # diverges silently.
    shadowing = find_shadowing_configs(resolved)
    if shadowing:
        detail["shadowing"] = [{"path": str(p), "peers": n} for p, n in shadowing]
        others = "; ".join(f"{p} ({n} peers)" for p, n in shadowing)
        warnings.append(
            f"ANOTHER config.yaml exists under a different home: {others}. "
            f"This command resolved {resolved} ({peer_count} peers) because "
            f"the path is HOME-derived (HOME={resolution['home']!r}). If you "
            f"are in a container, the fleet's real config is almost certainly "
            f"the other one, and WRITES HERE WILL NOT REACH IT — `sac host "
            f"add` will report success against this copy while fleet state is "
            f"unchanged. Compare the peer counts: they are the fastest tell. "
            f"To act on the fleet config, run on the host or point at it with "
            f"SCITEX_AGENT_CONTAINER_CONFIG=<path>."
        )

    return errors, warnings, detail
