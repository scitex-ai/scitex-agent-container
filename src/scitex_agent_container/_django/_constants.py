"""Constants for the browser-facing Agents app."""

from __future__ import annotations

# Standalone server port. The app is mounted elsewhere (scitex-hub) under its
# own path, so the standalone port is a loopback dev convenience only.
DEFAULT_PORT = 31296

# Environment contract for a mounted deployment. The web process reads the SAC
# host control plane over HTTP with a Bearer token; these names are the SSOT.
API_URL_ENV = "SCITEX_AGENT_CONTAINER_API_URL"
TOKEN_FILE_ENV = "SCITEX_AGENT_CONTAINER_API_TOKEN_FILE"
TOKEN_ENV = "SCITEX_AGENT_CONTAINER_API_TOKEN"

# Default listener the standalone server expects on this host.
DEFAULT_API_URL = "http://127.0.0.1:7878"

# Authorization. A comma-separated allowlist of operator identities. Control
# over THIS node's agents is granted to a listed identity; control over OTHER
# nodes' agents (cross-host) additionally requires membership in the cross-host
# list. The browser-facing identity is declared by the deployment (standalone
# has no Django login; a mounted host authenticates via its own Django auth).
OPERATORS_ENV = "SCITEX_AGENT_CONTAINER_LIFECYCLE_OPERATORS"
CROSSHOST_OPERATORS_ENV = "SCITEX_AGENT_CONTAINER_CROSSHOST_OPERATORS"
IDENTITY_ENV = "SCITEX_AGENT_CONTAINER_GUI_IDENTITY"

# Audit trail for cross-host (audited) authorizations. An absolute path wins;
# otherwise the audit log sits beside the GUI runtime state.
AUDIT_LOG_ENV = "SCITEX_AGENT_CONTAINER_GUI_AUDIT_LOG"

__all__ = [
    "API_URL_ENV",
    "AUDIT_LOG_ENV",
    "CROSSHOST_OPERATORS_ENV",
    "DEFAULT_API_URL",
    "DEFAULT_PORT",
    "IDENTITY_ENV",
    "OPERATORS_ENV",
    "TOKEN_ENV",
    "TOKEN_FILE_ENV",
]
