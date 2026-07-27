"""sac's scheduled-job surface.

* :mod:`._jobs_plugin` — the ``scitex_dev.jobs`` entry-point provider that
  surfaces sac's periodic jobs through the ecosystem aggregator.
* :mod:`._jobs_audit` — the inert-feature detector that checks every
  declared job actually has a live counterpart.
"""
