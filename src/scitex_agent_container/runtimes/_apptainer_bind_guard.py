"""Fail-loud guard for spec-declared binds that carry a CAPABILITY.

The bug (measured 2026-08-09 on scitex-compute-04)
--------------------------------------------------
Every agent spec on that host carries, verbatim::

    - /home/ywatanabe/.config/gh:/home/agent/.config/gh:ro

The host directory EXISTED — and held only ``config.yml``. ``hosts.yml``,
the file that actually carries ``oauth_token``, was absent. So the bind
SUCCEEDED: apptainer mounted a real directory containing no credential.
Inside the container ``gh`` answers "not logged in", which is
indistinguishable from "this agent was never given a token", and neither
``GH_TOKEN`` nor ``GITHUB_TOKEN`` was set as a fallback (verified via
``/proc/<pid>/environ``). All 12 agents on the host concluded no GitHub
token existed; one told the operator it could not merge its own PR and
asked a peer to do it for it. The mount was wrong for hours and said
nothing at any point.

Why every existing check missed it
----------------------------------
Because they all ask the wrong question. ``_p3a_default_binds`` filters
fleet defaults on ``expanded.is_dir()``; ``_inline_spec_preflight`` and
``_relocate_preflight`` stat the source with ``Path(...).exists()``.
Every one of those passes here — the directory *was* there. **Source
exists is not the same question as capability delivered.** Nothing in
the tree had ever looked INSIDE a bind source, so a credential mount
could be empty and still read as green.

Operator ruling (2026-08-09, stated twice)
------------------------------------------
    "when binding fails, it must fail loudly"
    "bind 失敗 -> うるさく失敗です; spec.yaml を直すかディスクを確認して
     ください … logging.error で言葉出すだけでは?"

So a message here must name, in one breath: WHICH agent, WHICH spec
file, WHICH host path is empty or missing, WHICH capability is therefore
unavailable, and the CONCRETE remedy. An operator reading it should know
what to fix without asking anyone.

Refuse the start, or only log loudly? — both, split deliberately
----------------------------------------------------------------
The two cases are not equally dangerous, so they do not get the same
verdict. The split follows the precedent already in this package
(``_apptainer_listen_env`` refuses when the spec explicitly requested
``server:sac`` and warns when it did not):

* A bind whose destination is in :data:`CAPABILITY_BINDS` — a
  deliberately TINY, named set of credential destinations — **REFUSES
  the start** when its source is missing, or present but lacking the one
  file that proves the credential is there. Justification: the spec
  named a credential destination, so the capability was explicitly
  requested; and this is the single case that CANNOT be diagnosed from
  inside the container, because an empty credential mount and an
  ungranted credential look identical to ``gh`` / ``claude``. A loud log
  would still leave a running agent asserting, wrongly and for hours,
  that it has no token. The escape hatch needs no new config: delete the
  bind line from the spec if this agent genuinely does not need the
  capability (same escape ``_sdk_channels`` offers for channels).

* EVERY OTHER spec-declared bind whose source is absent gets a
  ``logging.error`` and the start CONTINUES. Justification: data /
  scratch / host-specific mounts (``/mnt/c``, ``/data/gpfs/...``) go
  missing legitimately when a spec travels between hosts, and grounding
  a fleet over an optional mount would be worse than the bug this module
  exists to fix. Degraded is not the same as falsified.

* Fleet-default binds are untouched. They are filtered by existence in
  ``_p3a_default_binds.default_binds_for_host`` and their silent skip is
  documented there as deliberate — they are sac's suggestion, not the
  operator's declaration.

The named set is kept small on purpose: a general "this bind must
contain X" spec schema is a field nobody would fill in, and an unfilled
field would have prevented exactly zero of this incident. The two
entries below are the two credentials sac actually hands an agent, and
they are the same two ``cli_pkg/_explain._annotate`` already names.

Consequence for ``sac agents explain`` — accepted, not overlooked
----------------------------------------------------------------
The gate lives in ``build_run_argv``, which ``sac agents explain`` also
calls, so ``explain`` on an agent with a broken credential bind prints
this refusal instead of a mount table. That is the intended answer:
``explain``'s contract is "what will ``start`` do", and what ``start``
will do is refuse. The message it prints IS the diagnosis, and it names
the one command that makes both work again.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, NamedTuple

logger = logging.getLogger(__name__)


class BindCapabilityError(RuntimeError):
    """A spec-declared credential bind cannot deliver its capability.

    Raised BEFORE any container is launched, so nothing has to be
    unwound: ``build_run_argv`` is a pure function.
    """


class CapabilityBind(NamedTuple):
    """One named credential destination and the file that proves it.

    ``dst_suffix``  matched against the CONTAINER-side path of the bind
                    (suffix match, so ``/home/agent/.config/gh`` and any
                    other home root both hit).
    ``proof``       path RELATIVE to the bind source that must exist for
                    the capability to actually work. Empty string means
                    the bind source ITSELF is the proof (a file bind).
    ``capability``  what the agent loses, in the operator's terms.
    ``remedy``      the concrete fix, runnable or editable.
    """

    dst_suffix: str
    proof: str
    capability: str
    remedy: str


CAPABILITY_BINDS: tuple[CapabilityBind, ...] = (
    CapabilityBind(
        dst_suffix="/.config/gh",
        proof="hosts.yml",
        capability=(
            "GitHub CLI authentication — `gh auth`, `gh pr`, `gh api`, and "
            "therefore this agent's ability to open/review/merge its own PRs"
        ),
        remedy=(
            "run `gh auth login` on THIS host so ~/.config/gh/hosts.yml is "
            "written — config.yml alone carries NO oauth_token and is what "
            "made this look configured. Or declare the token explicitly as "
            "`spec.env.GH_TOKEN` (a bare host export does NOT reach a "
            "--containall container, so that is not a workaround). Or delete "
            "this bind line if the agent genuinely does not need gh"
        ),
    ),
    CapabilityBind(
        dst_suffix="/.credentials.json",
        proof="",
        capability="the Claude account credential this agent runs on",
        remedy=(
            "run `claude /login` for that account on this host, then "
            "`sac accounts save <account>`, or re-point this bind line at an "
            "account file that exists"
        ),
    ),
)


def _split_bind(bind_str: str) -> tuple[str, str] | None:
    """Return ``(host_source, container_destination)`` for a bind string.

    Covers all THREE shapes apptainer's ``--bind`` consumes, because the
    spec parser accepts all three (``config/_parsers/_apptainer.py`` —
    ``_validate_dst`` returns early on a colonless entry rather than
    rejecting it): ``host``, ``host:container`` and
    ``host:container:mode``. The bare form means src == dst, and it must
    be checked like any other — a spec line reading simply
    ``- /home/ywatanabe/.config/gh`` is the SAME credential bind as the
    two-sided form, so skipping it would reopen this module's own bug.
    ``None`` only for an entry with no usable source or destination.
    """
    bind_str = bind_str.strip()
    if not bind_str:
        return None
    if ":" not in bind_str:
        return bind_str, bind_str
    src, _, rest = bind_str.partition(":")
    dst = rest.split(":", 1)[0]
    if not src or not dst:
        return None
    return src, dst


def _rule_for(dst: str) -> CapabilityBind | None:
    """Return the capability rule matching this destination, if any.

    A trailing slash is stripped first: ``/home/agent/.config/gh/`` and
    ``/home/agent/.config/gh`` are the same mount, and a rule that missed
    on the cosmetic difference would fail silently — the exact failure
    mode this module exists to remove.
    """
    dst = dst.rstrip("/") or "/"
    for rule in CAPABILITY_BINDS:
        if dst == rule.dst_suffix or dst.endswith(rule.dst_suffix):
            return rule
    return None


def _render_capability_failure(
    *,
    agent: str,
    spec_path: str,
    bind_str: str,
    source: Path,
    required: Path,
    rule: CapabilityBind,
) -> str:
    if not source.exists():
        state = (
            f"the host path {source} DOES NOT EXIST, so the bind cannot "
            "deliver anything"
        )
    else:
        state = (
            f"the host path {source} exists but does NOT contain "
            f"{rule.proof!r} — the one file that carries the credential"
        )
    return (
        f"bind {bind_str!r} declared for agent {agent!r} cannot deliver "
        f"{rule.capability}: {state}.\n"
        f"  spec file:  {spec_path}\n"
        f"  host path:  {required}\n"
        "  WHY THIS IS FATAL AND NOT A WARNING: the mount would SUCCEED — "
        "apptainer binds the source whatever is in it — so inside the "
        "container the capability reports 'not configured', which is "
        "indistinguishable from 'this agent was never granted it'. That is "
        "exactly the 2026-08-09 incident: 12 agents spent hours believing no "
        "GitHub token existed. Refusing to launch an agent whose declared "
        "credential bind carries no credential.\n"
        f"  FIX: {rule.remedy}."
    )


def validate_capability_binds(config: object, spec_binds: Iterable[str]) -> None:
    """Refuse (or loudly report) spec binds that cannot deliver.

    ``spec_binds`` is ``spec.apptainer.binds`` — the OPERATOR-declared
    entries only. Fleet defaults are excluded by design; see the module
    docstring for the refuse-vs-log split and why each half is what it
    is.

    Raises :class:`BindCapabilityError` for a credential bind that
    cannot deliver. Returns ``None`` otherwise, having emitted one
    ``logging.error`` per non-credential bind whose host source is
    absent.
    """
    agent = str(getattr(config, "name", "") or "<unnamed agent>")
    spec_path = str(getattr(config, "config_path", "") or "<spec path unknown>")
    for bind_str in spec_binds:
        parts = _split_bind(str(bind_str))
        if parts is None:
            continue
        src_raw, dst = parts
        source = Path(src_raw).expanduser()
        rule = _rule_for(dst)
        if rule is None:
            if not source.exists():
                # Loud, not fatal — a host-specific data/scratch mount.
                logger.error(
                    "bind source MISSING for agent %r: the spec %s declares "
                    "'%s' but the host path %s does not exist. The container "
                    "will see an empty %s, so anything the agent expects "
                    "there is silently absent. NOT fatal (this bind carries "
                    "no credential, and host-specific mounts legitimately "
                    "differ per host) — the start continues. If the agent "
                    "needs it: create the path on this host, or fix/remove "
                    "the bind line in %s.",
                    agent,
                    spec_path,
                    bind_str,
                    source,
                    dst,
                    spec_path,
                )
            continue
        required = source / rule.proof if rule.proof else source
        if required.exists():
            continue
        message = _render_capability_failure(
            agent=agent,
            spec_path=spec_path,
            bind_str=str(bind_str),
            source=source,
            required=required,
            rule=rule,
        )
        # Log AND raise: the raise reaches whoever called `sac agents
        # start`, the log reaches the agent's start log for whoever reads
        # it later. The operator asked for words, not a bare traceback.
        logger.error("%s", message)
        raise BindCapabilityError(message)


def spec_binds_checked(config: object) -> list[str]:
    """Read ``spec.apptainer.binds`` and gate them in one call.

    ``build_run_argv`` calls THIS rather than reading the field and
    validating separately, so the gate cannot be skipped by a future
    caller that assembles the list itself — the only way to obtain the
    spec binds is to obtain them checked.
    """
    ap = getattr(config, "apptainer", None)
    binds = [str(b) for b in getattr(ap, "binds", None) or []]
    validate_capability_binds(config, binds)
    return binds


__all__ = [
    "CAPABILITY_BINDS",
    "BindCapabilityError",
    "CapabilityBind",
    "spec_binds_checked",
    "validate_capability_binds",
]
