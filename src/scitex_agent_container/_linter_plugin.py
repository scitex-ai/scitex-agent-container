"""Linter plugin for scitex-agent-container.

Registers sac-specific lint rules with scitex-dev's linter via the
``scitex_dev.linter.plugins`` entry-point group. Plugin shape is the
canonical one (rules / call_rules / axes_hints / checkers) — see
``scitex_dev.linter._plugin_loader.load_plugins``.

Rule numbering is ``STX-SAC<NNN>``. Each rule corresponds to a real
bug class we have shipped fixes for; the linter is how we prevent the
next one of the same shape.

Adding a rule
=============
1. Append a ``Rule(...)`` and (if call-detectable) a ``call_rules`` entry
   below.
2. For structural rules (dict-literal / string-literal patterns), write
   an AST ``checker`` class and add it to ``checkers``.
3. Bump the relevant skill leaf if user-visible.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


def get_plugin():
    from scitex_dev.linter._rules._base import Rule

    SAC001 = Rule(
        id="STX-SAC001",
        severity="error",
        category="a2a",
        message=(
            "AgentCard dict literal contains an A2A v0 field "
            "(``url`` / ``authentication`` / ``stateTransitionHistory``). "
            "A2A v1.0 replaces these with ``supportedInterfaces`` (and "
            "drops ``stateTransitionHistory`` entirely). Card validation "
            "via ``ParseDict(card, AgentCard())`` will reject the v0 shape."
        ),
        suggestion=(
            "Build the card via ``scitex_agent_container.a2a._card.build_card`` "
            "or ``fleet_card`` — both emit the v1 shape and pass "
            "``validate_card_v1`` on every served route. If you must hand-"
            "construct, use ``supportedInterfaces: [{transport: 'jsonrpc', "
            "protocolBinding: 'HTTP+JSON', url: ...}]`` and drop "
            "``authentication`` / ``stateTransitionHistory``."
        ),
    )

    SAC002 = Rule(
        id="STX-SAC002",
        severity="error",
        category="a2a",
        message=(
            "JSON-RPC method name is an A2A v0 string "
            "(``tasks/send`` / ``tasks/sendSubscribe``). A2A v1.0 uses "
            "``SendMessage`` / ``SendStreamingMessage``; v0 strings will "
            "404 against a v1 server."
        ),
        suggestion=(
            "Replace ``tasks/send`` with ``SendMessage`` and "
            "``tasks/sendSubscribe`` with ``SendStreamingMessage``. "
            "Sac-extension fields (``from_agent``, ``priority``, ...) move "
            "under ``params.metadata`` — strict v1 validation rejects them "
            "at the top level of ``params``."
        ),
    )

    SAC003 = Rule(
        id="STX-SAC003",
        severity="warning",
        category="env",
        message=(
            "Direct ``os.environ[...]`` read of a ``SAC_`` / "
            "``SCITEX_AGENT_CONTAINER_`` env key. This bypasses the dual-"
            "form support in ``_env.getenv()`` — if a user sets only the "
            "long form, your code misses it (and vice-versa). Reads also "
            "skip the conflict-detection that ``getenv`` performs when "
            "both forms are set to different values."
        ),
        suggestion=(
            "Use ``from scitex_agent_container._env import getenv`` "
            "and call ``getenv('HUB_URL')`` instead of "
            "``os.environ['SAC_HUB_URL']``. For writes, use ``setenv`` "
            "which writes both aliases. The ``_env`` module itself is "
            "exempted from this rule."
        ),
    )

    return {
        "rules": [SAC001, SAC002, SAC003],
        # Method-string detection (SAC002) is a string-literal pattern, not
        # a function call, so it lives in a checker rather than call_rules.
        "call_rules": {},
        "axes_hints": {},
        "checkers": [_SacCardChecker, _SacMethodChecker, _SacEnvChecker],
    }


# ---------------------------------------------------------------------------
# Checkers
# ---------------------------------------------------------------------------
#
# The scitex-dev checker contract (see ``scitex_dev.linter.checker``) wants
# objects that take an AST module + filepath and yield diagnostics. Each
# class below implements that contract for one SAC rule. The diagnostic
# shape mirrors what the core checker emits so the runner can render them
# uniformly.


@dataclass
class _Diag:
    rule_id: str
    line: int
    col: int


class _SacCardChecker:
    """SAC001 — flag v0 AgentCard fields in dict literals."""

    _V0_KEYS = frozenset({"url", "authentication", "stateTransitionHistory"})
    # An AgentCard always carries both ``name`` and ``description`` (per
    # the v1 proto). Heuristic: only flag dicts that look like an
    # AgentCard, i.e. contain ``name`` AND at least one v0 key.
    _CARD_MARKER = "name"

    rule_id = "STX-SAC001"

    def __init__(self, filepath: str):
        self.filepath = filepath

    def visit(self, tree: ast.AST):
        diags: list[_Diag] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {
                k.value
                for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            if self._CARD_MARKER in keys and keys & self._V0_KEYS:
                diags.append(_Diag(self.rule_id, node.lineno, node.col_offset))
        return diags


class _SacMethodChecker:
    """SAC002 — flag legacy JSON-RPC method strings."""

    _V0_METHODS = frozenset({"tasks/send", "tasks/sendSubscribe"})
    rule_id = "STX-SAC002"

    def __init__(self, filepath: str):
        self.filepath = filepath

    def visit(self, tree: ast.AST):
        diags: list[_Diag] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value in self._V0_METHODS:
                diags.append(_Diag(self.rule_id, node.lineno, node.col_offset))
        return diags


class _SacEnvChecker:
    """SAC003 — flag direct os.environ reads of SAC_ / SCITEX_AGENT_CONTAINER_."""

    _SAC_PREFIXES = ("SAC_", "SCITEX_AGENT_CONTAINER_")
    rule_id = "STX-SAC003"

    def __init__(self, filepath: str):
        self.filepath = filepath

    def visit(self, tree: ast.AST):
        # The ``_env`` module itself implements the dual-form lookup and is
        # exempt from this rule. Likewise tests in ``tests/`` may manipulate
        # env directly for setup; we exempt them by filepath suffix.
        if self.filepath.endswith("/_env.py") or "/tests/" in self.filepath:
            return []
        diags: list[_Diag] = []
        for node in ast.walk(tree):
            # Match os.environ[<str literal>] subscripts and
            # os.environ.get(<str literal>) calls.
            if isinstance(node, ast.Subscript) and self._is_os_environ(node.value):
                key = self._literal_str(node.slice)
                if key and any(key.startswith(p) for p in self._SAC_PREFIXES):
                    diags.append(_Diag(self.rule_id, node.lineno, node.col_offset))
            elif isinstance(node, ast.Call) and self._is_os_environ_get(node.func):
                if node.args:
                    key = self._literal_str(node.args[0])
                    if key and any(key.startswith(p) for p in self._SAC_PREFIXES):
                        diags.append(_Diag(self.rule_id, node.lineno, node.col_offset))
        return diags

    @staticmethod
    def _is_os_environ(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        )

    @classmethod
    def _is_os_environ_get(cls, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "get"
            and cls._is_os_environ(node.value)
        )

    @staticmethod
    def _literal_str(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None
