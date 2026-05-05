"""Parameterised-fleet expansion (F-CS2).

Replaces the shell-heredoc per-capsule yaml regeneration pattern with
a single template + CSV. One row per agent instance; the CSV header
names the substitution variables.

Wire format:

    template.yaml::

        spec:
          workdir: /tmp/${CAPSULE_ID}
          startup_commands:
            - command: |
                Run capsule ${CAPSULE_ID} on ${TASK}.

    fleet.csv::

        name,CAPSULE_ID,TASK
        capsule-aa-1,aa-1,structural-mask
        capsule-aa-2,aa-2,structural-mask

Result (each row materialises one ``<name>/<name>.yaml`` under
``output_dir``)::

    <output_dir>/capsule-aa-1/capsule-aa-1.yaml
    <output_dir>/capsule-aa-2/capsule-aa-2.yaml

Substitution is **string-level on the rendered YAML text**, applied
BEFORE parsing. ``${VAR}`` is the only syntax recognised; values
containing literal ``${`` should not occur in agent yamls today.
The first column is special: it must be ``name`` and supplies the
per-instance ``<dir>/<file>.yaml`` filename. Other columns are
substitution variables.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Iterable

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _substitute(template_text: str, mapping: dict[str, str]) -> str:
    def _sub(m: re.Match) -> str:
        key = m.group(1)
        replacement = mapping.get(key)
        return replacement if replacement is not None else m.group(0)

    return _VAR_RE.sub(_sub, template_text)


def find_unsubstituted_vars(rendered: str) -> list[str]:
    """Return any ``${VAR}`` placeholders that survived expansion."""
    return sorted({m.group(1) for m in _VAR_RE.finditer(rendered)})


def read_csv_rows(csv_path: Path) -> list[dict[str, str]]:
    """Read the CSV; require ``name`` column. Empty rows are skipped."""
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"params CSV is empty: {csv_path}")
        if "name" not in reader.fieldnames:
            raise ValueError(
                f"params CSV must have a 'name' column (header: {reader.fieldnames})"
            )
        rows = []
        for row in reader:
            if not row.get("name", "").strip():
                continue
            rows.append({k: (v or "") for k, v in row.items()})
        return rows


def expand_params_file(
    template_path: Path,
    csv_path: Path,
    output_dir: Path,
    *,
    overwrite: bool = False,
) -> list[Path]:
    """Materialise per-row yamls into ``output_dir`` and return their paths.

    Each row produces ``<output_dir>/<name>/<name>.yaml`` (the canonical
    sac yaml layout).  Survives an existing ``output_dir`` — the caller
    decides whether to wipe it. ``overwrite=False`` raises if a target
    yaml already exists; ``True`` lets it be replaced.

    Raises ``ValueError`` when:
      - the CSV lacks a ``name`` column,
      - any row leaves a ``${VAR}`` unexpanded,
      - two rows declare the same ``name``.
    """
    template_text = template_path.read_text()
    rows = read_csv_rows(csv_path)

    seen_names: set[str] = set()
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    for i, row in enumerate(rows):
        name = row["name"].strip()
        if name in seen_names:
            raise ValueError(f"params CSV row {i + 1}: duplicate name '{name}'")
        seen_names.add(name)

        mapping = {k: v for k, v in row.items() if k != "name"}
        # Make ${name} resolve to the row's name too — convenient for
        # workdir templates like /tmp/${name}.
        mapping["name"] = name

        rendered = _substitute(template_text, mapping)
        leftover = find_unsubstituted_vars(rendered)
        if leftover:
            raise ValueError(
                f"params CSV row {i + 1} ({name}): template references "
                f"unsubstituted variable(s) {leftover}; "
                f"add to CSV header or remove from yaml."
            )

        agent_dir = output_dir / name
        agent_dir.mkdir(parents=True, exist_ok=True)
        target = agent_dir / f"{name}.yaml"
        if target.exists() and not overwrite:
            raise FileExistsError(
                f"materialised yaml already exists: {target} "
                f"(pass --overwrite to replace)"
            )
        target.write_text(rendered)
        paths.append(target)
    return paths


def render_one(
    template_path: Path,
    mapping: dict[str, str],
    output_dir: Path,
    *,
    name: str,
    overwrite: bool = False,
) -> Path:
    """Render a single instance from ``template_path`` + ``mapping``.

    Used by ``--instance-id`` for ad-hoc one-offs that don't justify
    a CSV; ``mapping`` mimics one row of read_csv_rows output.
    """
    return expand_params_file(
        template_path,
        _make_inline_csv([{"name": name, **mapping}]),
        output_dir,
        overwrite=overwrite,
    )[0]


def _make_inline_csv(rows: Iterable[dict[str, str]]) -> Path:
    """Internal helper: write a temporary CSV holding ``rows``.

    ``render_one`` re-uses ``expand_params_file`` for parsing parity;
    this is the cheapest way to keep one source of truth.
    """
    import tempfile

    rows = list(rows)
    if not rows:
        raise ValueError("render_one: empty mapping")
    cols = list(rows[0].keys())
    if "name" not in cols:
        cols = ["name", *cols]
    fd, path = tempfile.mkstemp(suffix=".csv")
    with open(fd, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return Path(path)
