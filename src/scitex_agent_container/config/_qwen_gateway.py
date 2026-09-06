"""The fleet Qwen gateway — ONE address, written once, overridable per host.

WHY THIS MODULE EXISTS. Measured over the 119 tracked specs on 2026-09-06,
the same gateway is spelled two incompatible ways and copy-pasted eleven
times: ``http://127.0.0.1:18772`` in the eight handyman specs (they run on
the machine that serves it) and ``http://100.64.0.1:18772`` in ``business``
(it runs on scitex-compute-01, where a loopback URL points at nothing —
its own comment says so). One of those two is wrong on any given host, and
which one is wrong depends on where the agent starts. ``handyman-08`` also
names a different ``auth_token_env`` than its seven siblings, which is what
copy-paste drift looks like when it is silent.

THE FLEET ENGINE LIBRARY declares the Qwen engine ONCE
(``$SCITEX_DIR/agent-container/engines.yaml``, :mod:`._engine_library`) and
names its backend by provider NAME — :data:`QWEN_GATEWAY_PROVIDER` — which
resolves through ``_provider_registry.resolve_provider`` at start time, on
whichever host the agent actually starts on. The address lives here and
nowhere else, so a gateway move is an edit to THIS file rather than to 119
spec files.

WHAT THIS MODULE NO LONGER OWNS. It used to carry the ENGINE half too
(``QWEN_ENGINE_KEY`` / ``_MODEL`` / ``_HARNESS`` / ``_REASONING_EFFORT`` /
``_MAX_CONTEXT_TOKENS``), because ``sac agents migrate-engines`` copied a
whole ``qwen38-27b`` entry into every spec it swept. That entry is data, and
data belongs in the engine library where one line moves the whole fleet — a
Python constant naming a model, an effort and a context window was a second
place to say what a YAML row already says. The engine definition moved
there; the ADDRESS stayed here, because an address that must resolve
differently per host is code, not data.

THE SPELLING IS LOAD-BEARING. Measured by scitex-hub on 2026-09-05:

    scitex-compute-04:18772   HTTP 401   listening and auth-gating = REACHABLE
    compute-04:18772          000        the NAME does not resolve
    compute-04-lan:18772      000        the NAME does not resolve

``scitex-compute-04`` is the spelling that resolves from every host on the
fleet; the shorter forms and the ``-lan`` convention do not. curl reports
``000`` for an unresolvable name, which reads as "the gateway is down" while
meaning "the hostname is wrong" — see :mod:`._engine_reach`, which refuses to
collapse those two into one answer.

OVERRIDING IT. :data:`QWEN_GATEWAY_URL_ENV` and
:data:`QWEN_GATEWAY_TOKEN_ENV_ENV` override the address and the key's env-var
NAME for one host or one process, so a peer that reaches the gateway by a
different route does not need its specs rewritten. They are read at RESOLVE
time, not at import time: a module-level constant frozen at import would make
an export set after ``import scitex_agent_container`` silently ineffective.

The value of the API key is never held here — only the NAME of the host env
var that holds it, which is the same rule ``ProviderSpec.auth_token_env``
already follows.
"""

from __future__ import annotations

import os

__all__ = [
    "DEFAULT_QWEN_GATEWAY_TOKEN_ENV",
    "DEFAULT_QWEN_GATEWAY_URL",
    "QWEN_GATEWAY_HOST",
    "QWEN_GATEWAY_PORT",
    "QWEN_GATEWAY_PROBE_PATH",
    "QWEN_GATEWAY_PROVIDER",
    "QWEN_GATEWAY_TOKEN_ENV_ENV",
    "QWEN_GATEWAY_URL_ENV",
    "qwen_gateway_probe_url",
    "qwen_gateway_provider_entry",
    "qwen_gateway_token_env",
    "qwen_gateway_url",
]

#: The registered provider name a migrated spec writes instead of an address.
QWEN_GATEWAY_PROVIDER = "qwen-gateway"

#: The hostname that RESOLVES from every fleet host. Not an alias — see the
#: module docstring for the measurement that rules out the short forms.
QWEN_GATEWAY_HOST = "scitex-compute-04"

#: The Anthropic-Messages port the gateway serves. Distinct from the per-agent
#: vLLM replica ports (18773-18775, OpenAI transport), which are a different
#: axis and are NOT touched by the engines migration.
QWEN_GATEWAY_PORT = 18772

#: Where the gateway is, absent an override.
DEFAULT_QWEN_GATEWAY_URL = f"http://{QWEN_GATEWAY_HOST}:{QWEN_GATEWAY_PORT}"

#: The NAME of the host env var holding the gateway key — never the key.
DEFAULT_QWEN_GATEWAY_TOKEN_ENV = "SCITEX_GENAI_GATEWAY_API_KEY"

#: Per-host override for the address. Read at resolve time.
QWEN_GATEWAY_URL_ENV = "SAC_QWEN_GATEWAY_URL"

#: Per-host override for the env-var NAME the key is read from. This is one
#: level of indirection deeper than it looks: the value of THIS variable is
#: itself a variable name, which is how ``handyman-08``'s divergent
#: ``SAC_LOCAL_GPTOSS_KEY`` can be honoured on one host without editing specs.
QWEN_GATEWAY_TOKEN_ENV_ENV = "SAC_QWEN_GATEWAY_TOKEN_ENV"


def qwen_gateway_url() -> str:
    """The gateway address for THIS host, honouring the env override.

    An override that is set but blank is treated as unset rather than as "the
    empty URL": an exported-but-empty variable is how a shell says nothing,
    and resolving it to an unusable address would turn a typo in a profile
    into a fleet-wide refusal.
    """
    return (os.environ.get(QWEN_GATEWAY_URL_ENV) or "").strip() or (
        DEFAULT_QWEN_GATEWAY_URL
    )


#: The path a PREFLIGHT asks for, and it is not the base. Measured from
#: scitex-compute-04 on 2026-09-06 against this very gateway:
#:
#:     /                     404   listening, but this path does not exist
#:     /v1/models            401   REACHABLE + AUTH-GATED — the informative one
#:     /v1/chat/completions   401   same
#:     /health               200   a real health endpoint exists
#:     /healthz              404   does not exist
#:     /v1                   307   redirect
#:     CONTROL scitex-compute-99:18772/v1/models -> 000 (name unresolvable)
#:
#: A 401 HERE proves the inference API is present AND gating, which is exactly
#: what an engine entry needs. ``/health`` is deliberately not it: a 200 there
#: proves the process is up and says nothing about whether ``/v1`` is served
#: or auth is wired — a gate that cannot fail.
QWEN_GATEWAY_PROBE_PATH = "/v1/models"


def qwen_gateway_probe_url() -> str:
    """The address a reachability probe should actually dial.

    The base URL answers 404, which :mod:`._engine_reach` now names
    ``listening-wrong-path`` rather than passing off as a healthy gateway.
    Joined by hand rather than with ``urljoin`` so a base carrying a path
    prefix (a reverse proxy mounting the gateway under ``/qwen``) keeps it —
    ``urljoin`` would discard the prefix and probe the wrong place.
    """
    return f"{qwen_gateway_url().rstrip('/')}{QWEN_GATEWAY_PROBE_PATH}"


def qwen_gateway_token_env() -> str:
    """The NAME of the env var holding the gateway key, honouring the override."""
    return (os.environ.get(QWEN_GATEWAY_TOKEN_ENV_ENV) or "").strip() or (
        DEFAULT_QWEN_GATEWAY_TOKEN_ENV
    )


def qwen_gateway_provider_entry() -> "dict[str, str | None]":
    """The registry entry for :data:`QWEN_GATEWAY_PROVIDER`, resolved now.

    Same two-field shape every other ``PROVIDERS`` entry has, so a spec
    writing ``provider: qwen-gateway`` is byte-equivalent to one that
    copy-pasted this dict inline — which is exactly the equivalence the
    provider registry was created for (operator directive 2026-05-28).
    """
    return {
        "base_url": qwen_gateway_url(),
        "auth_token_env": qwen_gateway_token_env(),
    }
