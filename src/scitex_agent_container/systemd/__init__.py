"""Reference systemd ``--user`` unit TEMPLATES for sac's host services.

These are the durable source for units that, until now, existed in exactly one
place: live-only files under ``~/.config/systemd/user/``, untracked, with no
history and no review — while brokering the entire agent fleet. If that host is
rebuilt they are gone, along with any pin applied to them.

THEY ARE TEMPLATES, NOT DEPLOYED ARTIFACTS. Nothing in sac installs them.
Placeholders (``@SAC_BIN@``, ``@HOME@``, ``@SAC_SECRETS_ENVRC@``) must be filled
per host, and installing or restarting a unit is an OPERATOR action — the listen
unit brokers ``host_exec`` / spawn / restart for every agent, so restarting it
interrupts the whole fleet. See ``README.md`` in this directory for the
measurement that motivated tracking them.

Deliberately parameterised rather than copied verbatim: the live
``sac-listen.service`` carries an ``Environment=SAC_SECRETS_ENVRC=`` line
enumerating ~30 secret file paths under the operator's home. Committing that
literally would publish a map of the secrets layout to a public repository —
the paths are not themselves secrets, but they are a reconnaissance aid, and a
template has no reason to carry them.
"""
