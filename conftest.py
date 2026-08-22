"""Root conftest. Its ONLY job is to make store isolation survive a cleared ``addopts``.

``tests/_store_isolation.py`` is registered from ``pyproject.toml``
(``addopts = "... -p tests._store_isolation ..."``) and its docstring calls that
placement unconditional. It is unconditional with respect to WHICH DIRECTORY a
run collects -- the failure it was written to fix, where ``tests/smoke/`` and
``tests/integration/`` were siblings of the conftest that guarded them and so
escaped it entirely.

It is NOT unconditional with respect to the command line. ``-o addopts=`` erases
the whole string, and with it both the ``-p`` and the
``-m 'not integration and not docker_smoke'`` filter. MEASURED on this branch,
one probe test printing ``SCITEX_STORE_DSN``::

    pytest tests/tmp_probe.py                 -> ...@127.0.0.1:1/tests_must_not_write_to_the_fleet_store
    pytest tests/tmp_probe.py -o addopts=     -> ...@127.0.0.1:55432/scitex

The second is the LIVE per-host card store. So a single flag, reached for to
"simplify" a run, points every test at the fleet's real database and
simultaneously un-deselects the integration and docker suites.

That is not hypothetical. An agent working in this repo on 2026-08-22 ran
``pytest -o addopts=`` for exactly that reason, noticed within about ten seconds
and killed it, and had to go re-read the card store to confirm nothing had
landed. It is the third occurrence of this class: ``#1154`` wrote 46 fixture
rows into the live incarnations store, and the smoke suite later wrote
``alpha``/``beta``/``gamma`` into the live ``acl_deny_notify`` table.

A ROOT conftest is loaded by pytest before collection on every invocation and
cannot be switched off from the command line, so registering the plugin here
closes the remaining hole. The ``pyproject.toml`` entry stays: it loads EARLIER
than any conftest, which still matters for the directory-escape case. Both
registrations naming the same module is a no-op -- pytest registers a given
plugin module once.

Deliberately nothing else lives in this file. A root conftest is loaded for
every run in the repository, so anything added here is paid for by every test.
"""

pytest_plugins = ["tests._store_isolation"]
