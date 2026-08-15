"""Point the ``openai-agents`` SDK at an API surface the endpoint can serve.

The SDK defaults to the **Responses** API, which is an OpenAI-proprietary
surface. Self-hosted gateways (litellm in front of vLLM, and most
OpenAI-compatible servers) implement only ``/v1/chat/completions``, so
that default turns a perfectly healthy endpoint into an opaque failure.

MEASURED, compute-04, 2026-08-14. Driving qwen through litellm on the
fleet's Spartan tunnel, every turn died with::

    openai_session failed: Error code: 404 - {'detail': 'Not Found'}

That message names neither the route attempted nor the reason, and reads
exactly like a dead endpoint or a broken tunnel — two hypotheses that
were checked first and were both wrong. The model was up and answering
``/v1/chat/completions`` the entire time; the SDK was asking for
``/v1/responses``, which litellm does not route. A single
``set_default_openai_api("chat_completions")`` was the whole fix, and
the same call makes the difference between "the local-model agent works"
and "the local-model agent 404s".

The rule below is therefore deliberately biased: a configured
``OPENAI_BASE_URL`` that is not OpenAI's own host selects
chat-completions, because that is what such a host almost always speaks.
Where a gateway does implement Responses, :data:`API_ENV` overrides it.
With no base URL configured nothing is touched — talking to OpenAI proper
keeps the richer default surface.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = ["API_ENV", "select_api_surface"]

#: Env override, for when the inference below is wrong in either direction.
#: Accepts exactly the two values the SDK accepts; anything else is ignored
#: in favour of the inference, so a typo cannot silently disable the fix.
API_ENV = "SAC_OPENAI_API"

#: Hosts known to implement the Responses API. Anything else reached over
#: an OpenAI-compatible base URL is assumed chat-completions only.
RESPONSES_HOSTS = ("api.openai.com",)

_VALID = ("chat_completions", "responses")


def select_api_surface(agents: Any) -> str | None:
    """Configure ``agents``' default API surface. Returns the choice made.

    ``None`` means "left alone": either no base URL is configured, or it
    points at a host that serves Responses. Passing the module in (rather
    than importing it here) keeps this callable from the lazy-import sites
    that already hold a reference, and keeps the module import-light on
    Claude-only deployments where the SDK is absent.
    """
    choice = (os.environ.get(API_ENV) or "").strip().lower()

    if choice not in _VALID:
        base_url = (os.environ.get("OPENAI_BASE_URL") or "").strip()
        if not base_url:
            return None
        if any(host in base_url for host in RESPONSES_HOSTS):
            return None
        choice = "chat_completions"

    setter = getattr(agents, "set_default_openai_api", None)
    if setter is None:
        # Older/newer SDK without the knob: the caller still works, it just
        # keeps whatever default that version ships. Not worth raising over.
        return None
    setter(choice)
    return choice
