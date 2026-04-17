"""Concrete :class:`PaneAction` implementations.

Each module in this package provides a single ``PaneAction``
subclass that composes the observers (:mod:`..liveness_probe`,
pane-state classifiers, etc.) with the engine
(:func:`..action_base.run_action`). Operators who want to
disable auto-response simply never instantiate anything here —
the observers remain importable on their own.
"""

from .nonce_probe import NonceProbeAction  # noqa: F401
