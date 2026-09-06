"""Can this engine be HONOURED? — the three-valued verdict behind the refusal.

Q3 (operator, 2026-09-03): when the selected engine cannot be honoured
the start FAILS naming the engine key, what was unhonourable, and the
fix. 「勝手なフォールバックはしないと言うルールなので」 — no silent
fallback, ever, and never onto the default when an explicit ``--engine``
was given. This module answers the question; :mod:`_lifecycle.
_engine_select` turns the answer into a refusal.

THREE STATES, DELIBERATELY DISTINGUISHABLE:

  ``honourable``      every declared piece resolves.
  ``not-honourable``  a piece is DEFINITELY wrong — the provider name is
                      not in the registry, the inline dict is
                      incomplete, ``$AUTH_TOKEN_ENV`` is unset, the
                      harness is unknown, the endpoint actively refused
                      the connection. The start REFUSES.
  ``could-not-tell``  the probe could not reach a verdict — DNS did not
                      resolve, the connection timed out, the URL has no
                      host to dial. NEVER silently read as honourable:
                      the start proceeds but emits a LOUD warning naming
                      the engine and what could not be determined.

STATIC RESOLUTION ALWAYS; A LIVE PROBE ONLY ON DEMAND — and this is a
deliberate choice, stated here, in the ``--engine`` help, and in the
ADR. Making every start depend on a possibly-remote endpoint answering
is how a refusal-on-unreachable-dependency grounds a fleet (the hazard
already recorded on
``hub-cards-dsn-unreachable-should-refuse-to-boot-20260815``): the
endpoint is down for ten seconds, and every agent that restarts in that
window refuses. So:

  * STATIC resolution runs on EVERY start. It reads the spec and the
    host's environment — no sockets — and it is the whole refusal
    surface by default. Everything it rejects is a fact about the
    declaration, not about the network, so it cannot flap.
  * A LIVE PROBE runs only when asked (``--probe-engine``, or
    ``SAC_ENGINE_PROBE=1``), on a short bounded timeout, and it FAILS
    OPEN: a timeout is ``could-not-tell`` with a loud warning, never a
    refusal. Only an ACTIVE refusal from the endpoint — a closed port,
    which is a definite answer — is allowed to refuse a start.

The auth-token cascade mirrors ``runtimes._apptainer_provider.
provider_env_flags`` exactly (shell export → ``$HOME/.env`` →
scitex-config), so the verdict answers about the value the LAUNCH will
see rather than about a different, more optimistic environment. The key
VALUE is never returned, logged, or embedded in a message; only its
env-var NAME appears.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from urllib.parse import urlparse

from ._engine_types import ENGINES_KEY, EngineSpec
from ._provider_registry import list_providers, resolve_provider

__all__ = [
    "ENGINE_PROBE_ENV",
    "VERDICT_HONOURABLE",
    "VERDICT_NOT_HONOURABLE",
    "VERDICT_UNKNOWN",
    "EngineVerdict",
    "effective_harness",
    "engine_verdict",
    "probe_verdict",
    "static_verdict",
]

VERDICT_HONOURABLE = "honourable"
VERDICT_NOT_HONOURABLE = "not-honourable"
VERDICT_UNKNOWN = "could-not-tell"

#: Ops-only opt-in for the live reachability probe. NOT a spec surface —
#: a spec must not be able to make its own start depend on a network.
ENGINE_PROBE_ENV = "SAC_ENGINE_PROBE"

#: Bounded probe timeout. Short on purpose: the probe is an accelerator
#: for a definite "the port is closed", not a health check.
PROBE_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class EngineVerdict:
    """One engine's honourability, with the reason and the fix."""

    engine: str
    verdict: str
    reason: str = ""
    fix: str = ""
    probed: bool = False

    @property
    def honourable(self) -> bool:
        return self.verdict == VERDICT_HONOURABLE

    @property
    def refuses(self) -> bool:
        return self.verdict == VERDICT_NOT_HONOURABLE

    @property
    def undetermined(self) -> bool:
        return self.verdict == VERDICT_UNKNOWN


def _resolve_token(name: str) -> str:
    """The host value of env var ``name`` through the launch's own cascade.

    Returns ``""`` when unresolvable. Imported lazily so ``import
    scitex_agent_container.config`` does not pay scitex-config's
    first-import auto-configuration — the same reason
    ``_harness_types._harness_logger`` imports lazily.
    """
    from pathlib import Path

    from scitex_config import PriorityConfig, load_dotenv

    load_dotenv(dotenv_path=str(Path.home() / ".env"))
    return PriorityConfig(auto_uppercase=False).resolve(key=name, default="") or ""


def _ok(engine: EngineSpec, *, probed: bool = False) -> EngineVerdict:
    return EngineVerdict(engine.key, VERDICT_HONOURABLE, probed=probed)


def _no(engine: EngineSpec, reason: str, fix: str) -> EngineVerdict:
    return EngineVerdict(engine.key, VERDICT_NOT_HONOURABLE, reason, fix)


def effective_harness(engine: EngineSpec, harness: str | None = None) -> str:
    """The harness this start actually runs: the ENGINE's, else the SPEC's.

    An engine that states no harness states NO OPINION (that is the
    harness/engine split), so the spec's own ``harness:`` stands. Returns
    ``""`` when neither says anything, which the combination check reads
    as ``could-not-tell`` rather than as a vendor default.
    """
    if engine.harness:
        return str(engine.harness).strip().lower()
    return str(harness or "").strip().lower()


def static_verdict(
    engine: EngineSpec, harness: str | None = None
) -> EngineVerdict:
    """Resolve ``engine`` against the spec text and the host environment.

    NO SOCKETS. Every ``not-honourable`` returned here is a fact about
    the declaration (an unrunnable pairing, an unregistered name, an
    incomplete dict, an unset env var), so it is stable: re-running gives
    the same answer until someone changes the spec or the environment.

    ``harness`` is the SPEC's harness, used when the engine states none.

    THE PAIRING IS CHECKED FIRST, deliberately: an unsupported
    combination must be named AS a combination. Reporting it as "the
    provider dict is incomplete" would send the operator to fix a field
    that is not the problem.

    A ``could-not-tell`` PAIRING DOES NOT SHORT-CIRCUIT, and the
    asymmetry is the point: not knowing the HARNESS is not knowing one
    thing, and the declaration checks below can still return a DEFINITE
    "this is wrong" (an unregistered provider name, an incomplete inline
    dict, an unset token) that holds whatever the harness turns out to
    be. A definite answer outranks an undetermined one, so the
    undetermined verdict is HELD BACK and returned only when nothing
    definite was found — never discarded, because that would turn "I do
    not know" into "it is fine".
    """
    from ._engine_harness_combos import combination_verdict

    combo = combination_verdict(engine, effective_harness(engine, harness))
    if combo.refuses:
        return combo
    undetermined = combo if combo.undetermined else None

    declared = engine.provider_declared
    if isinstance(declared, str) and declared.strip():
        if resolve_provider(declared.strip()) is None:
            return _no(
                engine,
                f"provider={declared.strip()!r} is not a registered provider "
                "name",
                "use one of the registered providers "
                f"({', '.join(list_providers())}), write the inline "
                "{base_url, auth_token_env} form, or append the backend to "
                "PROVIDERS in config/_provider_registry.py",
            )
    elif isinstance(declared, dict):
        missing = [
            field
            for field in ("base_url", "auth_token_env")
            if not str(declared.get(field) or "").strip()
        ]
        if missing:
            return _no(
                engine,
                "the inline provider dict is incomplete — missing "
                + ", ".join(missing),
                f"give spec.{ENGINES_KEY}.{engine.key}.provider both "
                "`base_url` (the Anthropic-compatible endpoint) and "
                "`auth_token_env` (the NAME of the host env var holding "
                "the key, never the key itself)",
            )

    provider = engine.provider
    if provider is None:
        # No backend override: the harness's own built-in auth path,
        # which this engine axis does not add a failure mode to.
        return undetermined or _ok(engine)

    token_env = str(getattr(provider, "auth_token_env", "") or "").strip()
    if not token_env:
        return _no(
            engine,
            "the provider declares no auth_token_env, so there is no API "
            "key to resolve",
            f"set spec.{ENGINES_KEY}.{engine.key}.provider.auth_token_env to "
            "the NAME of the host env var holding the key",
        )
    if not _resolve_token(token_env):
        return _no(
            engine,
            f"the provider's auth_token_env names ${token_env}, which is "
            "unset on this host (shell export → $HOME/.env → scitex-config "
            "all resolved empty)",
            f"export {token_env}=... in the shell that runs sac, or add "
            f"`{token_env}=...` to $HOME/.env (chmod 0600). sac reads the "
            "value at start and never logs it",
        )
    return undetermined or _ok(engine)


def probe_verdict(
    engine: EngineSpec, *, timeout_s: float = PROBE_TIMEOUT_S
) -> EngineVerdict:
    """OPT-IN live reachability probe of the engine's ``base_url``.

    One bounded TCP connect. The mapping from outcome to verdict is the
    whole point:

      * connected                      → ``honourable``
      * connection REFUSED             → ``not-honourable`` (definite:
        something answered and said "closed")
      * timeout / DNS failure / no host in the URL / any other socket
        error → ``could-not-tell``. The caller must warn loudly and
        proceed; treating it as honourable silently, or as a refusal,
        both convert "I do not know" into a claim.

    An engine with no provider override (plain Anthropic OAuth) has no
    ``base_url`` to dial and is reported ``honourable`` unprobed — there
    is nothing this probe could add.
    """
    provider = engine.provider
    base_url = str(getattr(provider, "base_url", "") or "").strip()
    if not base_url:
        return _ok(engine)
    parsed = urlparse(base_url)
    host = parsed.hostname
    if not host:
        return EngineVerdict(
            engine.key,
            VERDICT_UNKNOWN,
            f"provider.base_url={base_url!r} has no host to dial, so "
            "reachability could not be determined",
            "check the base_url is a full URL (scheme://host[:port]/path)",
            probed=True,
        )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return _ok(engine, probed=True)
    except ConnectionRefusedError:
        return EngineVerdict(
            engine.key,
            VERDICT_NOT_HONOURABLE,
            f"{host}:{port} REFUSED the connection — the endpoint declared "
            f"by provider.base_url={base_url!r} is not listening",
            "start the backend that serves that URL, or select a different "
            "--engine",
            probed=True,
        )
    except OSError as exc:
        return EngineVerdict(
            engine.key,
            VERDICT_UNKNOWN,
            f"could not reach {host}:{port} within {timeout_s}s ({exc}); "
            "this is NOT evidence the endpoint is down",
            "re-run with the probe off (the default) to start anyway, or "
            "check the network path to the endpoint",
            probed=True,
        )


def engine_verdict(
    engine: EngineSpec,
    *,
    harness: str | None = None,
    probe: bool = False,
    timeout_s: float = PROBE_TIMEOUT_S,
) -> EngineVerdict:
    """Static resolution always; the live probe only when ``probe`` is set.

    Static runs FIRST and short-circuits: a spec that names an
    unregistered provider is wrong whether or not anything is listening,
    and dialling a socket to discover that would be slower and less
    specific.
    """
    verdict = static_verdict(engine, harness)
    if not verdict.honourable or not probe:
        return verdict
    return probe_verdict(engine, timeout_s=timeout_s)
