"""Linter plugin for scitex-agent-container.

Registers sac-specific lint rules with scitex-dev's linter via the
``scitex_dev.linter.plugins`` entry-point group. Plugin shape is the
canonical one (rules / call_rules / axes_hints / checkers) — see
``scitex_dev.linter._plugin_loader.load_plugins``.

Checker contract (per ``scitex_dev.linter.checker.lint_source``):

- Subclass ``ast.NodeVisitor``.
- Constructor signature: ``__init__(self, source_lines, config)``.
- ``self.issues`` is a list of ``scitex_dev.linter.checker.Issue``
  instances populated by ``visit(tree)``.
- Optional ``category`` class attribute for opt-in gating (e.g.
  ``"figure"`` gates behind ``config.enable=["FM"]``).

Rule numbering is ``STX-SAC<NNN>``. Each rule corresponds to a real
bug class we have shipped fixes for; the linter is how we prevent the
next one of the same shape.

Adding a rule
=============
1. Append a ``Rule(...)`` below.
2. For call-detectable rules, add an entry to ``call_rules``.
3. For structural rules (dict-literal / string-literal patterns),
   write an AST checker class and add it to ``checkers``.
4. Bump the relevant skill leaf if user-visible.
"""

from __future__ import annotations

import ast


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

    # NOTE: SAC003 (direct os.environ read of SAC_* keys) is drafted but
    # deferred. It needs filepath context to exempt ``_env.py`` itself and
    # tests/ (where direct env manipulation is a legitimate setup pattern).
    # scitex-dev's ``lint_source`` doesn't currently pass filepath to plugin
    # checkers — the planned upstream fix is to stash ``filepath`` on the
    # config object before invoking plugin checkers. Once that lands, SAC003
    # can be re-enabled by re-adding it to the returned dict.

    return {
        "rules": [SAC001, SAC002],
        "call_rules": {},
        "axes_hints": {},
        "checkers": [_SacCardChecker, _SacMethodChecker],
    }


# ---------------------------------------------------------------------------
# Checkers — each is an ast.NodeVisitor matching scitex-dev's contract.
# ---------------------------------------------------------------------------


def _get_rule(rule_id: str):
    """Resolve a rule by ID via scitex-dev's lookup (works across versions)."""
    from scitex_dev.linter._rules._lookup import lookup

    return lookup(rule_id)


def _make_issue(rule, line: int, col: int, source_line: str):
    """Construct an ``Issue`` matching scitex-dev's checker shape.

    Returns ``None`` if *source_line* carries a ``# stx-allow`` comment
    suppressing *rule.id*. scitex-dev's main ``SciTeXChecker._add`` honours
    these comments but plugin checkers populate ``self.issues`` directly,
    bypassing the suppression path — so we re-implement the check here.
    """
    from scitex_dev.linter.checker import Issue, _is_allowed_by_comment

    if _is_allowed_by_comment(source_line, rule.id):
        return None
    return Issue(rule=rule, line=line, col=col, source_line=source_line)


def _source_at(source_lines, lineno: int) -> str:
    if 1 <= lineno <= len(source_lines):
        return source_lines[lineno - 1].rstrip()
    return ""


class _SacCardChecker(ast.NodeVisitor):
    """SAC001 — flag v0 AgentCard fields in dict literals.

    Heuristic: only flag dicts that ALSO carry a ``name`` string key, so
    non-card dicts that happen to use ``url`` (e.g. config dicts) don't
    false-positive fire.
    """

    _V0_KEYS = frozenset({"url", "authentication", "stateTransitionHistory"})
    _CARD_MARKER = "name"

    def __init__(self, source_lines, config):
        self.source_lines = source_lines
        self.config = config
        self.issues: list = []

    def visit_Dict(self, node: ast.Dict):
        keys = {
            k.value
            for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        if self._CARD_MARKER in keys and keys & self._V0_KEYS:
            rule = _get_rule("STX-SAC001")
            if rule is not None:
                src = _source_at(self.source_lines, node.lineno)
                issue = _make_issue(rule, node.lineno, node.col_offset, src)
                if issue is not None:
                    self.issues.append(issue)
        self.generic_visit(node)


class _SacMethodChecker(ast.NodeVisitor):
    """SAC002 — flag legacy A2A JSON-RPC method strings."""

    # The v0 method strings are assembled from parts so this rule definition
    # does not self-flag under STX-SAC002. The reconstructed values are
    # ``tasks/send`` and ``tasks/sendSubscribe``.
    _V0_METHODS = frozenset(
        {
            "tasks" + "/" + "send",
            "tasks" + "/" + "sendSubscribe",
        }
    )

    def __init__(self, source_lines, config):
        self.source_lines = source_lines
        self.config = config
        self.issues: list = []

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, str) and node.value in self._V0_METHODS:
            rule = _get_rule("STX-SAC002")
            if rule is not None:
                src = _source_at(self.source_lines, node.lineno)
                issue = _make_issue(rule, node.lineno, node.col_offset, src)
                if issue is not None:
                    self.issues.append(issue)
        self.generic_visit(node)


# NOTE: _SacEnvChecker (for STX-SAC003) is intentionally not shipped in
# this initial cut. It needs the filepath of the source being linted to
# exempt ``_env.py`` (which legitimately implements the dual-form lookup)
# and ``tests/`` (which legitimately manipulates env vars for setup).
# scitex-dev's ``lint_source`` passes ``(lines, config)`` to plugin
# checkers — no filepath. Until upstream propagates filepath, this rule
# would either fire incorrectly on legitimate cases (false positives) or
# be inactive everywhere. Defer until the upstream plumbing lands.
