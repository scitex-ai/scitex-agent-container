"""Resolve ONE host env var by name: shell export, then ``$HOME/.env``.

Functionalities
---------------
- ``resolve_host_env(name)`` — the value of ``name`` from the process
  environment, else from ``$HOME/.env``, else ``""``.

Why this exists (scitex-config#47): sac used to call
``scitex_config.load_dotenv(dotenv_path=$HOME/.env)`` before reading a
provider key. That copied EVERY ``$HOME/.env`` variable, unrelated
credentials included, into sac's ``os.environ``. Every process sac spawned
afterwards (tmux panes, apptainer, probes) inherited them. This helper
reads the file without exporting anything and returns only the variable
that was asked for.

IO
--
- Reads: ``os.environ``, ``$HOME/.env`` (only when it is a regular file).
- Writes: nothing. ``os.environ`` is never modified.

Precedence is unchanged: a shell export wins over ``$HOME/.env``. The path
stays pinned to ``$HOME/.env`` (not cwd-first) so an operator running
``sac agents start`` from an unrelated project directory gets no surprise.
``PriorityConfig`` masks secret-shaped keys in its resolution log, so the
value is never logged.
"""

from __future__ import annotations

from pathlib import Path


def resolve_host_env(name: str) -> str:
    """Return ``name`` from the shell env, else ``$HOME/.env``, else ``""``."""
    from scitex_config import PriorityConfig, read_dotenv

    home_dotenv = Path.home() / ".env"
    file_value = read_dotenv(home_dotenv).get(name, "") if home_dotenv.is_file() else ""
    resolver = PriorityConfig(auto_uppercase=False)
    return resolver.resolve(key=name, default=file_value) or ""


# EOF
