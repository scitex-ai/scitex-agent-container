"""Everything a preflight needs to READ out of an agent's spec, in one place.

Split out of :mod:`_relocate_probe_adapter`, which had grown to hold two jobs:
asking a host questions, and knowing where in a spec the questions live. The
second is the one that keeps being got wrong, and it is worth reading on its own.

WHY THIS IS NOT A ONE-LINE ``spec["apptainer"]["binds"]`` PER FIELD. Every reader
below exists because the obvious lookup returned a plausible wrong answer:

    binds/image   live under ``spec.apptainer``, not at the top of ``spec``. A
                  top-level ``binds`` lookup reported "(none)" for a spec
                  carrying nineteen of them (2026-08-08) — and "no binds" is
                  exactly the answer that makes a bind check look satisfied when
                  it was never asked.
    card store    hides in TWO places: ``apptainer.env`` and the ``--env KEY=VALUE``
                  pairs of ``apptainer.raw_args``. This repo's own spec uses the
                  second and leaves the first empty, so a reader of ``env`` alone
                  concludes the agent has no card store at all.
    groups        are ``metadata.labels.groups`` / ``metadata.labels.group`` —
                  NOT ``spec.lineage.group``, which is the Phase-3 isolation
                  bucket and a different field wearing a similar name.
    workdir       is the only checkout key there is. ``spec.repo`` does not exist
                  and the validator rejects it; ``spec.workdir`` is what becomes
                  apptainer's ``--pwd``.

Every reader is DEFENSIVE and total: a missing, ``None`` or wrongly-typed key
yields the empty answer rather than raising. A relocation must be able to report
on a half-written spec — refusing to read because one field is malformed would
hide the very thing the operator is trying to see.

Pure dict traversal. No I/O, no yaml, no network.
"""

from __future__ import annotations

__all__ = [
    "apptainer_section",
    "bind_sources_from_spec",
    "card_store_url_from_spec",
    "credential_paths_from_spec",
    "declared_groups_from_spec",
    "group_labels_from_spec",
    "spec_body",
    "workdirs_from_spec",
]


def spec_body(spec: dict) -> dict:
    """The ``spec:`` mapping, tolerating a spec handed in already unwrapped."""
    inner = spec.get("spec")
    return inner if isinstance(inner, dict) else spec


def apptainer_section(spec: dict) -> dict:
    app = spec_body(spec).get("apptainer")
    return app if isinstance(app, dict) else {}


def card_store_url_from_spec(spec: dict) -> str:
    """The ``SCITEX_CARDS_DB`` the agent would use, wherever the spec hides it.

    Two places, both real: ``apptainer.env`` and the ``--env KEY=VALUE`` pairs in
    ``apptainer.raw_args``. Reading only the first is how the store check ends up
    with nothing to check while looking like it passed.
    """
    app = apptainer_section(spec)
    env = app.get("env")
    if isinstance(env, dict):
        value = env.get("SCITEX_CARDS_DB")
        if isinstance(value, str) and value:
            return value
    raw = app.get("raw_args")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.startswith("SCITEX_CARDS_DB="):
                return item.split("=", 1)[1]
    return ""


def bind_sources_from_spec(spec: dict) -> tuple[str, ...]:
    """The left half of every ``src:dst:mode`` bind."""
    binds = apptainer_section(spec).get("binds")
    if not isinstance(binds, list):
        return ()
    return tuple(
        b.split(":", 1)[0] for b in binds if isinstance(b, str) and b.split(":", 1)[0]
    )


def credential_paths_from_spec(spec: dict) -> tuple[str, ...]:
    """Candidate credential files, in the spec's own order, de-duplicated."""
    claude = spec_body(spec).get("claude")
    if not isinstance(claude, dict):
        return ()
    paths: list[str] = []
    single = claude.get("credentials_file")
    if isinstance(single, str) and single.strip():
        paths.append(single.strip())
    listed = claude.get("credentials_files")
    if isinstance(listed, list):
        paths += [p.strip() for p in listed if isinstance(p, str) and p.strip()]
    return tuple(dict.fromkeys(paths))


def workdirs_from_spec(spec: dict) -> tuple[str, ...]:
    """The directories the agent must RUN IN — ``spec.workdir``, when it has one.

    A TUPLE for a single value on purpose. There is no ``spec.repo`` today, but
    the fact this feeds is shaped like the binds fact — empty means looked and
    nothing missing — so a second such path later costs one list entry instead of
    a new fact, a new probe section and a new check.
    """
    workdir = spec_body(spec).get("workdir")
    if isinstance(workdir, str) and workdir.strip():
        return (workdir.strip(),)
    return ()


def group_labels_from_spec(spec: dict) -> dict:
    """``metadata.labels`` — where the ACL's group names are authored."""
    meta = spec.get("metadata")
    if not isinstance(meta, dict):
        return {}
    labels = meta.get("labels")
    return labels if isinstance(labels, dict) else {}


def declared_groups_from_spec(spec: dict) -> tuple[str, ...]:
    """The group names this spec CLAIMS — singular and plural forms both.

    Read here rather than through ``config._group_resolver`` deliberately. This
    side of the comparison is what the spec says; the other side is what the
    TARGET's own resolver makes of the same labels. Running both through the same
    local function would make the check incapable of failing, and the case worth
    catching is precisely when the two disagree.
    """
    labels = group_labels_from_spec(spec)
    out: list[str] = []
    single = labels.get("group")
    if isinstance(single, str) and single.strip():
        out.append(single.strip())
    listed = labels.get("groups")
    if isinstance(listed, str):
        listed = [listed]
    if isinstance(listed, list):
        out += [g.strip() for g in listed if isinstance(g, str) and g.strip()]
    return tuple(dict.fromkeys(out))
