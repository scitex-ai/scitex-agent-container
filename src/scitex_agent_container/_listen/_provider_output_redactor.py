"""Redact propagated provider values from detached restart output."""

from __future__ import annotations

import os
import sys

from ._provider_env import redact_provider_secrets


def main() -> int:
    """Copy stdin to stdout with registered provider values removed."""
    payload = sys.stdin.read()
    sys.stdout.write(redact_provider_secrets(payload, os.environ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
