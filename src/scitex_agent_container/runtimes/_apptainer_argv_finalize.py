"""The final assembly phase of the ``apptainer exec`` flag argv.

Extracted from :mod:`._apptainer_build_argv` (which crossed the 512-line
per-file cap). Everything here runs AFTER the last ``--env`` / ``--bind``
contributor — including ``spec.apptainer.raw_args`` — and BEFORE the SIF
path is appended.

That is a real phase, not an arbitrary cut: every step in it exists
because of an **ordering invariant** on the finished flag region, and
those invariants only hold if the whole region is already present.

1. **Reconcile duplicate ``--env`` keys.** Several layers contribute
   ``--env`` and two of them routinely name the same key. Collapse to a
   single occurrence so the launch stops depending on apptainer's
   last-wins tie-break to be correct — see :mod:`._apptainer_env_dedup`.
2. **Refuse a banned scitex DSN.** Once one value per key survives, check
   the one that will actually reach the container (ADR-0022: port 5432 is
   never used for scitex).
3. **Bind the overlay upper-home** over the container ``$HOME``, after
   ``raw_args`` so it wins over a raw-arg ``--home`` tmpfs.
4. **Bind ``/uvwork`` from the host scratch volume** (ADR-0024), after
   every spec-declared bind so an explicit spec bind to ``/uvwork`` wins,
   and once per start — the resolver refuses the launch outright when
   the host has no scratch root and no written decision to go without.
5. **Lift secret-shaped ``--env`` into a 0600 env-file**, after every
   ``--env`` source so nothing is missed, before the creds bind so that
   bind stays last.
6. **Emit the designated credentials bind last**, so no earlier bind can
   shadow it.
7. **Validate the flag region** as a whole, which is only meaningful once
   it is complete.

``finalize_flag_argv`` is pure with respect to its ``argv`` argument (a
new list is returned). It does touch the filesystem — writing the 0600
secrets file and pre-creating the credentials bind target — because both
must exist before apptainer runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._apptainer_argv_guard import validate_flag_argv


def finalize_flag_argv(
    argv: list[str],
    config: Any,
    *,
    state_dir: Path,
    home_host: Path,
    upper_home: Path | None,
    spec_raw_args: list[str],
) -> list[str]:
    """Run the ordering-sensitive tail passes over the flag argv.

    ``argv`` must already carry every contributed flag, ``raw_args``
    included, and must NOT yet carry the SIF path or the inner command —
    the passes below assume the list they walk is the flag region.

    ``upper_home`` is the resolved overlay upper-home (``None`` for
    non-overlay specs) and ``home_host`` the workspace home; both are
    resolved by the caller, which needs them earlier for its own bind
    decisions. ``spec_raw_args`` is passed through solely so the
    malformed-flag guard can attribute a fault to the spec rather than
    to sac.

    Raises :class:`._apptainer_env_dedup.ForbiddenScitexDsnError` or
    :class:`._apptainer_argv_guard.ApptainerArgvError` rather than
    returning an argv that would misroute or misparse.
    """
    from ._apptainer_env_dedup import (
        assert_no_forbidden_scitex_dsn,
        collapse_duplicate_env,
    )

    agent = getattr(config, "name", None)

    # The LAST ``--env`` contributor has now run, so reconcile the layers
    # that can name the same key. The fleet/spec env layer and raw_args
    # both declare SCITEX_CARDS_DB across this fleet, and until now the
    # argv simply carried it twice — correct only because apptainer's
    # ``--env`` is last-wins. Collapse to one occurrence per key (the
    # last, i.e. the value apptainer already resolved) so reordering this
    # assembly can never silently repoint an agent's card store, then
    # refuse outright if what survives is a scitex DSN on the banned port
    # 5432. See _apptainer_env_dedup and ADR-0022.
    argv = collapse_duplicate_env(argv, agent=agent)
    assert_no_forbidden_scitex_dsn(argv, agent=agent)

    # Relaxed + directory-overlay + explicit ``--home`` shadows the
    # to_home tree. ``deploy_to_home_overlay`` materialises the tree
    # into ``<overlay>/upper/<container_home>/``, but a raw-arg
    # ``--home /home/agent`` makes apptainer mount a FRESH tmpfs at
    # that path (verified via `mount`: ``tmpfs on /home/agent``),
    # which shadows the overlay's upper-home — so $HOME/.mcp.json,
    # $HOME/CLAUDE.md, $HOME/.claude/ are all silently absent in the
    # container. The SDK runner's ``merge_home_mcp_servers`` then
    # reads an empty ``$HOME/.mcp.json`` and a per-agent MCP (e.g. an
    # agent's own telegrammer bot) never reaches the SDK.
    #
    # Fix: bind the materialised upper-home OVER the container HOME,
    # appended AFTER raw_args so it wins over the ``--home`` tmpfs
    # (apptainer applies user binds after home setup). No-op for
    # non-relaxed / non-directory-overlay specs (resolver returns
    # None) and when the upper-home wasn't materialised.
    if upper_home is not None and upper_home.is_dir():
        from ._to_home_overlay import resolve_container_home

        container_home = resolve_container_home(config)
        argv += ["--bind", f"{upper_home}:{container_home}"]

    # /uvwork → the host SCRATCH volume, not the overlay upper (ADR-0024).
    # Every spec's startup_commands put uv, the uv cache, TMPDIR and the
    # agent venv under /uvwork; the image creates that directory and
    # nothing bound it, so all of it accumulated in overlays/<agent>/upper
    # on the host's ROOT LV — measured 11.7 GB for sac alone, and the root
    # LV on scitex-compute-04 filled to 0 four times on 2026-09-02. The
    # resolver reads config.yaml's `scratch_root:` (else probes /scratch,
    # else REFUSES the start naming both fixes); the helper creates
    # <root>/sac/agents/<name>/uvwork (0700) and emits the bind. Placed
    # after raw_args and the spec binds so an explicit spec bind to
    # /uvwork still wins (first bind to a destination wins in apptainer),
    # and before the secret lift so the creds bind below stays LAST. The
    # bind reaches the on-disk argv record with every other bind.
    from ._apptainer_scratch import uvwork_bind_flags

    argv += uvwork_bind_flags(config, argv)

    # SECURITY (P1 credential fix): lift secret-shaped ``--env KEY=VALUE``
    # pairs out of the WORLD-READABLE argv (it becomes a tmux ``bash -c``
    # pane cmd; /proc/<pid>/cmdline leaks it to any local process) into a
    # per-agent 0600 ``--env-file``. AFTER every ``--env`` source
    # (auth/provider/listen/spec.env/raw_args) so any secret is caught, but
    # BEFORE the creds bind below so that bind stays LAST (its last-wins
    # shadowing invariant). apptainer still delivers every value; see
    # _apptainer_secret_env.
    from ._apptainer_secret_env import redact_secret_env_to_file

    argv = redact_secret_env_to_file(argv, state_dir=state_dir)

    # Designated credentials file (spec.claude.credentials_file) — bound
    # writable at ``$HOME/.claude/.credentials.json``. Emitted LAST among
    # binds (after the overlay-upper-home bind) so the relaxed ``--home``
    # tmpfs / upper-home bind cannot shadow it; last bind to a path wins.
    # apptainer FILE binds need the in-container destination to pre-exist,
    # so first ensure an empty placeholder at the host path backing the
    # container $HOME (overlay upper-home when relaxed-directory-overlay,
    # else the workspace-home bind). Without it a fresh overlay agent FATALs
    # at boot: "destination doesn't exist in container". The placeholder
    # goes in the bind DESTINATION backing, never to_home (whose
    # credential-leak guard refuses .credentials.json). No-op w/o a creds bind.
    from ._apptainer_auth import credentials_file_bind, ensure_credentials_bind_target

    creds_bind = credentials_file_bind(config)
    ensure_credentials_bind_target(
        config,
        home_host=home_host,
        overlay_upper_home=upper_home,
        bind_flags=creds_bind,
    )
    argv += creds_bind

    # Root-cause guard for the stray ``--fakeroot`` file in the project root
    # (see _apptainer_argv_guard): a value-taking flag missing its value.
    # raw_args + name let the message attribute the fault and name the spec.
    validate_flag_argv(argv, raw_args=spec_raw_args, agent=agent)

    return argv


__all__ = ["finalize_flag_argv"]
