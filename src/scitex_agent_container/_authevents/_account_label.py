"""Best-effort: WHICH account was an agent using when this happened?

The account is the field that makes the log answer the question the operator
actually asks — "six died at once, whose token rotated?" Correlating deaths
against rotations is only possible if each death record names an account.

IT MUST BE ALLOWED TO SAY "I DON'T KNOW"
    So this resolver returns ``None`` on every failure path, and ``None`` is
    written to the log as JSON ``null`` — present, and explicitly unknown.
    The alternative (guessing the host's current account, or reusing the
    literal ``"unknown"`` string the label resolver returns) would put a
    plausible value into the exact field investigators join on. A field that
    can only ever say "yes" cannot corroborate anything; an account that is
    wrong is worse than an account that is missing, because it will be
    believed and it will send someone to the wrong rotation.

    Note also what this resolves: the account an agent is CONFIGURED to use,
    read from its spec. That is a strong hint, not proof of the credential the
    wedged process is actually holding in memory — an agent started before a
    reassignment holds the OLD one. Treat it as a hint and say so in the log.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["resolve_account_for_agent"]


def resolve_account_for_agent(
    name: str, *, specs_dir: Path | None = None
) -> str | None:
    """The account label for ``name``, or ``None`` if undeterminable.

    Never raises, never blocks and never guesses. Every failure — no registry,
    no spec, an unparseable spec, a resolver hiccup — collapses to ``None``,
    which the writer records as ``null``.
    """
    # stx-allow: fallback (reason: this fills ONE optional field on an
    # observability record. Any failure must render as UNKNOWN (None) and can
    # never be allowed to raise into the restart path being observed.)
    try:
        from .._reconcile._pass import fleet_agents_dir

        root = specs_dir if specs_dir is not None else fleet_agents_dir()
        spec = Path(root) / name / "spec.yaml"
        if not spec.is_file():
            return None

        from .._account.agent_account import resolve_agent_account_label
        from ..config import load_config

        config = load_config(spec)
        env = getattr(config, "env", None)
        assigned = getattr(getattr(config, "claude", None), "account", "") or None
        label = resolve_agent_account_label(env, assigned_account=assigned)
        # The label resolver answers the literal "unknown" when it has no
        # credentials file and no override. That is a non-answer, so it must
        # not travel as though it were one.
        if not label or str(label).strip().lower() == "unknown":
            return None
        return str(label)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None
