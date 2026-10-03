#!/usr/bin/env python3
"""Delegate home hooks to the matched package-owned Telegram predicate."""

from __future__ import annotations

import sys
from importlib import import_module

__all__ = ["ESCAPE_ENV", "check_message"]
ESCAPE_ENV = "CC_ALLOW_BARE_ISSUE"
_REQUIRED_HOOK_INPUT = "cct.telegram-hook-input/v1"


def _owner():
    module = import_module("claude_code_telegrammer._telegram_rules")
    if getattr(module, "HOOK_INPUT_VERSION", None) != _REQUIRED_HOOK_INPUT:
        raise ImportError("matched CCT hook-input capability unavailable")
    adapter = import_module("claude_code_telegrammer._hook_input")
    if getattr(adapter, "HOOK_INPUT_VERSION", None) != _REQUIRED_HOOK_INPUT:
        raise ImportError("matched whole CCT input adapter unavailable")
    return module


def check_message(text):
    """Preserve the public predicate API without retaining any rule copy."""
    return _owner().check_message(text)


def main(argv):
    """Unknown hook input is fail-open; unavailable text transport is an error."""
    mode = argv[1] if len(argv) > 1 else "--hook-json"
    try:
        owner = _owner()
    except ImportError:
        sys.stderr.write(
            "UNKNOWN Telegram hook binding: matched CCT package unavailable; "
            "outbound guard remains required.\n"
        )
        return 0 if mode == "--hook-json" else 70
    return owner.main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
