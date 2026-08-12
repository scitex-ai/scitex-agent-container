"""Can sac on the target be REACHED — and which of the three PATHs is that about?

One check, and it takes a whole file because the question has been asked wrongly
once already and the wrong answer was a hint that sent people to do something
they had already done.

    2026-08-11, scitex-compute-04: sac lives at ~/.env-sac/bin/sac and is absent
    from the non-interactive ssh PATH, so `ssh compute-04 sac agents list`
    answers "No such file or directory" — the same words a machine with no sac
    at all produces. That is why the check separates INSTALLED from FINDABLE:
    the two need opposite fixes and produce identical errors.

    2026-08-12, ywata-note-win: the check FAILED and told the operator to set
    the peer's `env_preamble`. It was already set, to a preamble that works, and
    :class:`._relocate_shell.Shell` prepends it to every command a relocation
    sends. So the check was reading a PATH that nothing in this feature uses,
    and answering with a step that changes nothing.

THREE PATHS, AND THE CHECK MUST SAY WHICH IT MEANS:

    sac_on_path       the BARE non-interactive ssh PATH. What a person typing
                      `ssh host sac …` gets. A true answer to a stricter
                      question than this feature needs.
    sac_usable_path   that PATH PLUS the peer's env_preamble — what every
                      relocation command actually runs under. THE ONE THAT
                      DECIDES.
    sac_resolved_path found by looking harder (login shell, known venvs). Only
                      ever used to tell "not installed" from "installed and
                      unreachable".

The hint is narrow for the same reason :mod:`.._state._remote_sac_hint` is
narrow, and it states that reason where it branches: a peer that ALREADY
declares a preamble has a different problem, and naming env_preamble there is a
confident wrong answer.

NO I/O, like its siblings. Facts in, a :class:`Check` out.
"""

from __future__ import annotations

from typing import Final

from ._relocate_checks import _unobserved
from ._relocate_preflight_facts import Check, TargetFacts

__all__ = ["CHECK_SAC_PRESENT", "check_sac_present"]

CHECK_SAC_PRESENT: Final = "sac_present_on_target"


def check_sac_present(facts: TargetFacts, to_host: str) -> Check:
    """Installed AND findable are two questions; the answer names which failed.

    The fact that DECIDES is ``sac_usable_path`` — where sac resolves under the
    raw ssh PATH plus the peer's preamble, which is what the relocation uses.
    ``sac_on_path`` is still read, because it is what an operator typing a bare
    ssh command will get and it is the only answer an older probe supplies, and
    it still PASSES on its own; it simply no longer FAILS on its own.
    """
    if facts.sac_usable_path:
        via = (
            " (via the peer's env_preamble)"
            if facts.preamble_declared and not facts.sac_on_path
            else ""
        )
        return Check(
            name=CHECK_SAC_PRESENT,
            ok=True,
            detail=(
                f"sac is reachable on {to_host} at {facts.sac_usable_path} under the PATH "
                f"this relocation's own commands run under{via}"
            ),
        )
    if facts.sac_on_path:
        where = f" at {facts.sac_resolved_path}" if facts.sac_resolved_path else ""
        return Check(
            name=CHECK_SAC_PRESENT,
            ok=True,
            detail=f"sac is on {to_host}'s non-interactive ssh PATH{where}",
        )
    if facts.sac_usable_path is None and facts.sac_on_path is None:
        return _unobserved(CHECK_SAC_PRESENT, "whether sac is on the target's ssh PATH")
    if facts.sac_usable_path is None and facts.preamble_declared:
        return Check(
            name=CHECK_SAC_PRESENT,
            ok=None,
            detail=(
                f"sac is not on {to_host}'s BARE ssh PATH, and this peer declares an "
                "env_preamble whose effect on that PATH was not measured — so whether a "
                "relocation command would find sac there is undetermined"
            ),
            hint=(
                f"measure the PATH the relocation actually uses: ssh {to_host} "
                "'<the peer's env_preamble>; command -v sac'. The bare PATH is a "
                "stricter question than this feature needs answered"
            ),
        )
    if facts.sac_resolved_path is None:
        return Check(
            name=CHECK_SAC_PRESENT,
            ok=None,
            detail=(
                f"sac is not reachable on {to_host} under the PATH relocation commands "
                "use, and whether it is installed there at all was not established"
            ),
            hint=(
                "look for it directly before concluding anything: "
                f"ssh {to_host} 'bash -lc \"command -v sac\"', and check the known venv "
                "(~/.env-sac/bin/sac). 'Not on PATH' and 'not installed' produce the "
                "same error and need opposite fixes"
            ),
        )
    if not facts.sac_resolved_path:
        return Check(
            name=CHECK_SAC_PRESENT,
            ok=False,
            detail=f"sac is NOT INSTALLED on {to_host} — not on the ssh PATH and at no known location",
            hint=(
                f"install sac on {to_host} before relocating onto it. Every remote step "
                "of the relocation — starting the target, verifying the source stopped — "
                "is a sac call over ssh"
            ),
        )
    return Check(
        name=CHECK_SAC_PRESENT,
        ok=False,
        detail=(
            f"sac IS INSTALLED on {to_host} at {facts.sac_resolved_path}, and is not "
            "reachable under the PATH this relocation's commands run under"
        ),
        hint=_sac_path_hint(facts, to_host),
    )


def _sac_path_hint(facts: TargetFacts, to_host: str) -> str:
    """What to actually do — which depends on whether a preamble is already set.

    The narrowness is the point, and it is the same discipline
    :mod:`.._state._remote_sac_hint` already states for the rc=127 case: a peer
    that ALREADY declares an ``env_preamble`` has a different problem (a wrong
    path in it, a venv that moved) and pointing it at ``env_preamble`` would be a
    confident wrong answer. Measured 2026-08-12 on ywata-note-win, which declares
    ``export PATH="$HOME/.env-3.11/bin:$PATH"`` and was told to declare one.
    """
    found = facts.sac_resolved_path or "the path it was found at"
    if facts.preamble_declared:
        return (
            f"do NOT add an env_preamble — {to_host} already declares one, and it is "
            "applied to every command this relocation sends. It is not putting "
            f"{found} on PATH, so the preamble itself is what to fix: compare the "
            f"directory it exports against $(dirname {found}) in "
            "~/.scitex/agent-container/config.yaml"
        )
    if facts.preamble_declared is None:
        return (
            f"do NOT install a second copy — sac is at {found}. Check whether "
            f"{to_host} declares an env_preamble in ~/.scitex/agent-container/config.yaml: "
            "if it does, that preamble is not exporting this directory and is what to "
            f"fix; if it does not, add one exporting $(dirname {found})"
        )
    return (
        f"do NOT install a second copy — {to_host} declares NO env_preamble in "
        "~/.scitex/agent-container/config.yaml, and ssh runs a non-login shell, so a "
        f"venv install is invisible there. Add one exporting $(dirname {found}); "
        "every script this relocation sends is prefixed with it"
    )
