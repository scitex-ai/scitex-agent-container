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

* A bind — credential or not — whose source cannot be STATTED at all
  gets a ``logging.error`` and the start CONTINUES, including for a
  destination in :data:`CAPABILITY_BINDS`. This third case is not a
  nicety: ``Path.exists()`` swallows only ENOENT/ENOTDIR/EBADF/ELOOP
  (``pathlib._IGNORED_ERRNOS``) and RE-RAISES everything else, so an
  EACCES from another user's ``0700`` parent, or an ESTALE/ETIMEDOUT
  from an autofs/NFS/GPFS hiccup, escapes as a bare ``OSError``. Left
  unhandled it aborts ``build_run_argv`` for a bind two paragraphs above
  promise is non-fatal, and surfaces through ``_start_single``'s generic
  handler as ``[Errno 13]`` plus a traceback — no agent, no spec file,
  no remedy: precisely what the operator ruled against. And it must not
  REFUSE either, even for a credential bind: refusal here is earned by
  PROOF that the mount is empty, and an unanswerable stat proves
  nothing. Refusing on an unproven premise would ground agents on a
  transient I/O error, which is the fleet outage this module is
  explicitly not willing to trade for. So the verdict is: say so, name
  the errno, and point at this line as the first thing to check if the
  capability later reports "not configured".

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
import socket
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


def _probe(path: Path) -> tuple[bool, OSError | None]:
    """Answer "does this exist" WITHOUT letting the answer kill the start.

    ``Path.exists()`` is not total. It swallows exactly four errnos —
    ``pathlib._IGNORED_ERRNOS`` is ``ENOENT, ENOTDIR, EBADF, ELOOP`` —
    and re-raises the rest, so EACCES (a parent directory owned by
    another user, mode ``0700``), ESTALE (a stale NFS handle), EIO and
    ETIMEDOUT (autofs / GPFS not answering) all come back as a thrown
    ``OSError`` rather than as ``False``.

    That distinction is the whole point: "the path is not there" is a
    FACT this module is entitled to act on, while "I could not find out"
    is not. Returning the two separately — ``(exists, None)`` when the
    question was answerable and ``(False, err)`` when it was not — is
    what lets the callers below refuse only on proof.
    """
    try:
        return path.exists(), None
    except OSError as exc:  # EACCES / ESTALE / EIO / ETIMEDOUT / ...
        return False, exc


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
    source_exists: bool,
    required: Path,
    rule: CapabilityBind,
) -> str:
    # `source_exists` is PASSED IN, never re-statted here: the caller has
    # already probed it once through _probe, and a second stat could both
    # disagree with the first and raise the very OSError _probe exists to
    # contain — from inside the error formatter, of all places.
    if not source_exists:
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
        f"  host:       {socket.gethostname()}\n"
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


def _render_unverifiable(
    *,
    agent: str,
    spec_path: str,
    bind_str: str,
    path: Path,
    error: OSError,
    rule: CapabilityBind | None,
) -> str:
    """The message for a bind source this process could not stat at all."""
    if rule is None:
        return (
            f"bind source UNVERIFIABLE for agent {agent!r}: the spec "
            f"{spec_path} declares {bind_str!r} but the host path {path} "
            f"could not be checked ({error.__class__.__name__}: {error}). "
            f"host: {socket.gethostname()}. NOT fatal — this bind carries no "
            "credential and the start continues; apptainer will report its "
            "own error if the mount is genuinely unusable. If the agent "
            "needs it: fix the permissions or the mount on this host, or "
            f"fix/remove the bind line in {spec_path}."
        )
    return (
        f"bind {bind_str!r} declared for agent {agent!r} MAY NOT deliver "
        f"{rule.capability}: the host path {path} could not be checked "
        f"({error.__class__.__name__}: {error}), so this process can prove "
        "neither that the credential is there nor that it is missing.\n"
        f"  host:       {socket.gethostname()}\n"
        f"  spec file:  {spec_path}\n"
        f"  host path:  {path}\n"
        "  NOT REFUSING: a refusal here is earned by PROOF that the "
        "credential mount is empty, and an unanswerable stat proves "
        "nothing. Grounding an agent on a permission or transient-I/O error "
        "would be the fleet outage this guard is explicitly unwilling to "
        "trade for. The start continues.\n"
        "  IF the capability later reports 'not configured' inside the "
        f"container, START HERE — then: {rule.remedy}."
    )


def validate_capability_binds(config: object, spec_binds: Iterable[str]) -> None:
    """Refuse (or loudly report) spec binds that cannot deliver.

    ``spec_binds`` is ``spec.apptainer.binds`` — the OPERATOR-declared
    entries only. Fleet defaults are excluded by design; see the module
    docstring for the refuse-vs-log split and why each half is what it
    is.

    Three verdicts, and only the first one stops anything:

    * credential bind PROVEN unable to deliver → ``logging.error`` and
      :class:`BindCapabilityError`;
    * non-credential bind whose source is PROVEN absent → one
      ``logging.error``, start continues;
    * any bind whose source could not be statted at all (EACCES, ESTALE,
      …) → one ``logging.error`` naming the errno, start continues,
      credential destinations included. Never refuse on a non-answer.

    Returns ``None`` in the last two cases.
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
        src_ok, src_err = _probe(source)
        if src_err is not None:
            # Could not find out. Say so; never refuse on a non-answer.
            logger.error(
                "%s",
                _render_unverifiable(
                    agent=agent,
                    spec_path=spec_path,
                    bind_str=str(bind_str),
                    path=source,
                    error=src_err,
                    rule=rule,
                ),
            )
            continue
        if rule is None:
            if not src_ok:
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
        req_ok, req_err = _probe(required)
        if req_err is not None:
            # The directory answered but the proof file did not (an
            # unreadable credential dir, mode 0700 owned by someone else).
            # Same rule as above: no proof, no refusal.
            logger.error(
                "%s",
                _render_unverifiable(
                    agent=agent,
                    spec_path=spec_path,
                    bind_str=str(bind_str),
                    path=required,
                    error=req_err,
                    rule=rule,
                ),
            )
            continue
        if req_ok:
            continue
        message = _render_capability_failure(
            agent=agent,
            spec_path=spec_path,
            bind_str=str(bind_str),
            source=source,
            source_exists=src_ok,
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
