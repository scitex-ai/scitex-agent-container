"""Task-ladder fixtures for local-model coding trials.

Each rung materializes a throwaway repo from ``fixture_repos/<rung>/*.tmpl``
(the ``.tmpl`` suffix keeps fixture data out of this repo's own pytest
collection and linters), plus a task prompt, the set of files the task
legitimately touches, the symbol deletions the task explicitly requires,
and a mechanical assert run against the finished repo.

Nothing here trusts the model: every assert is AST- or subprocess-based.
"""

from __future__ import annotations

import ast
import os
import subprocess

PY = "/opt/venv-sac/bin/python"
FIXTURE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixture_repos")


def load_files(rung: str) -> dict:
    """Return {filename: content} for a rung's template repo."""
    src = os.path.join(FIXTURE_ROOT, rung)
    out = {}
    for name in sorted(os.listdir(src)):
        if not name.endswith(".tmpl"):
            continue
        with open(os.path.join(src, name), encoding="utf-8") as fh:
            out[name[: -len(".tmpl")]] = fh.read()
    if not out:
        raise FileNotFoundError(f"no templates for rung {rung} under {src}")
    return out


def _run_py(repo: str, code: str, timeout: int = 60):
    """Run a python snippet with the venv interpreter, cwd=repo."""
    return subprocess.run(
        [PY, "-c", code], cwd=repo, capture_output=True, text=True,
        timeout=timeout,
    )


def _read(repo: str, name: str) -> str:
    with open(os.path.join(repo, name), encoding="utf-8") as fh:
        return fh.read()


def _func_uses_name(fn_node: ast.AST, name: str) -> bool:
    return any(
        isinstance(n, ast.Name) and n.id == name for n in ast.walk(fn_node)
    )


# --------------------------------------------------------------------------
# per-rung asserts
# --------------------------------------------------------------------------

def s1_assert(repo: str):
    src = _read(repo, "calc.py")
    tree = ast.parse(src)
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    if "sum_list" not in fns or "product_list" not in fns:
        return False, "sum_list or product_list missing"
    if not ast.get_docstring(fns["sum_list"]):
        return False, "sum_list has no docstring"
    if _func_uses_name(fns["sum_list"], "tmp"):
        return False, "sum_list still uses variable 'tmp'"
    if not _func_uses_name(fns["sum_list"], "total"):
        return False, "sum_list does not use variable 'total'"
    if not _func_uses_name(fns["product_list"], "tmp"):
        return False, "product_list was modified (lost its 'tmp' variable)"
    return True, "ok"


def s2_assert(repo: str):
    tree = ast.parse(_read(repo, "stringutils.py"))
    if not any(
        isinstance(n, ast.FunctionDef) and n.name == "snake_to_camel"
        for n in tree.body
    ):
        return False, "snake_to_camel not defined in stringutils.py"
    check = (
        "from stringutils import snake_to_camel\n"
        "assert snake_to_camel('hello_world') == 'helloWorld'\n"
        "assert snake_to_camel('foo_bar_baz') == 'fooBarBaz'\n"
        "assert snake_to_camel('single') == 'single'\n"
        "print('S2-OK')\n"
    )
    proc = _run_py(repo, check)
    if proc.returncode != 0:
        return False, f"snake_to_camel behaves wrong: {proc.stderr[-300:]}"
    test_tree = ast.parse(_read(repo, "test_stringutils.py"))
    has_new_test = any(
        isinstance(n, ast.FunctionDef)
        and n.name.startswith("test")
        and any(
            isinstance(m, ast.Name) and m.id == "snake_to_camel"
            for m in ast.walk(n)
        )
        for n in test_tree.body
    )
    if not has_new_test:
        return False, "no unit test referencing snake_to_camel was added"
    return True, "ok"


def m1_assert(repo: str):
    geo = ast.parse(_read(repo, "geometry.py"))
    geo_fns = {n.name for n in geo.body if isinstance(n, ast.FunctionDef)}
    if "rect_area" not in geo_fns:
        return False, "geometry.py has no rect_area"
    if "area" in geo_fns:
        return False, "geometry.py still defines area"
    if "perimeter" not in geo_fns:
        return False, "perimeter was lost from geometry.py"
    rep = ast.parse(_read(repo, "report.py"))
    used = {n.id for n in ast.walk(rep) if isinstance(n, ast.Name)}
    imported = {
        alias.name
        for node in rep.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    if "area" in used or "area" in imported:
        return False, "report.py still references bare 'area'"
    if "rect_area" not in (used | imported):
        return False, "report.py does not use rect_area"
    return True, "ok"


def m2_assert(repo: str):
    if _read(repo, "test_stats.py") != load_files("M2")["test_stats.py"]:
        return False, "test_stats.py was modified (tests must stay untouched)"
    check = (
        "from stats import median\n"
        "assert median([7, 1, 5]) == 5\n"
        "assert median([1, 2, 3, 4]) == 2.5\n"
        "assert median([9, 3]) == 6.0\n"
        "assert median([2]) == 2\n"
        "print('M2-OK')\n"
    )
    proc = _run_py(repo, check)
    if proc.returncode != 0:
        return False, f"median still wrong: {proc.stderr[-300:]}"
    return True, "ok"


def l1_assert(repo: str):
    models = ast.parse(_read(repo, "models.py"))
    item_methods = {
        m.name
        for n in models.body
        if isinstance(n, ast.ClassDef) and n.name == "Item"
        for m in n.body
        if isinstance(m, ast.FunctionDef)
    }
    if "restock" not in item_methods:
        return False, "Item.restock missing in models.py"
    store = ast.parse(_read(repo, "store.py"))
    store_methods = {
        m.name
        for n in store.body
        if isinstance(n, ast.ClassDef) and n.name == "Store"
        for m in n.body
        if isinstance(m, ast.FunctionDef)
    }
    if "restock_item" not in store_methods:
        return False, "Store.restock_item missing in store.py"
    if "restock" not in _read(repo, "cli.py"):
        return False, "cli.py has no restock command"
    check = (
        "from store import Store\n"
        "from cli import run_command\n"
        "s = Store()\n"
        "run_command(s, ['add', 'apple', '2.5', '4'])\n"
        "out = run_command(s, ['restock', 'apple', '3'])\n"
        "assert out == 'restocked apple to 7', repr(out)\n"
        "assert s.get_item('apple').quantity == 7\n"
        "try:\n"
        "    s.restock_item('pear', 1)\n"
        "    raise SystemExit('expected KeyError')\n"
        "except KeyError:\n"
        "    pass\n"
        "try:\n"
        "    s.get_item('apple').restock(-2)\n"
        "    raise SystemExit('expected ValueError')\n"
        "except ValueError:\n"
        "    pass\n"
        "print('L1-OK')\n"
    )
    proc = _run_py(repo, check)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout)[-300:]
        return False, f"restock feature behaves wrong: {err}"
    return True, "ok"


def l2_assert(repo: str):
    trans = ast.parse(_read(repo, "transforms.py"))
    if not any(
        isinstance(n, ast.ClassDef) and n.name == "TitleCase"
        for n in trans.body
    ):
        return False, "TitleCase class missing in transforms.py"
    pipe = ast.parse(_read(repo, "pipeline.py"))
    if not any(
        isinstance(n, ast.FunctionDef) and n.name == "titlecase_lines"
        for n in pipe.body
    ):
        return False, "titlecase_lines missing in pipeline.py"
    test_tree = ast.parse(_read(repo, "test_transforms.py"))
    has_new_test = any(
        isinstance(n, ast.FunctionDef)
        and n.name.startswith("test")
        and ("TitleCase" in ast.dump(n) or "titlecase" in ast.dump(n))
        for n in test_tree.body
    )
    if not has_new_test:
        return False, "no unit test for TitleCase in test_transforms.py"
    check = (
        "from transforms import REGISTRY, get_transform\n"
        "from pipeline import Pipeline, titlecase_lines\n"
        "assert 'title' in REGISTRY, 'not registered'\n"
        "assert get_transform('title')().apply(['hello world']) == "
        "['Hello World']\n"
        "assert Pipeline(['title']).run(['a b']) == ['A B']\n"
        "assert titlecase_lines(['x y']) == ['X Y']\n"
        "print('L2-OK')\n"
    )
    proc = _run_py(repo, check)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout)[-300:]
        return False, f"TitleCase feature behaves wrong: {err}"
    return True, "ok"


# --------------------------------------------------------------------------
# the ladder
# --------------------------------------------------------------------------

FIXTURES = {
    "S1": {
        "task": (
            "In calc.py, rename the local variable `tmp` to `total` inside "
            "the function `sum_list` only (leave `product_list` and every "
            "other function untouched), and add a one-line docstring to "
            "`sum_list`. Change nothing else in the repository."
        ),
        "expected_changed": {"calc.py"},
        "allowed_deletions": set(),
        "assert": s1_assert,
        "inject_failing_tests": False,
    },
    "S2": {
        "task": (
            "Add a function `snake_to_camel(text)` to stringutils.py that "
            "converts snake_case to camelCase (e.g. 'hello_world' -> "
            "'helloWorld', 'foo_bar_baz' -> 'fooBarBaz', 'single' -> "
            "'single'), and add at least one unit test for it in "
            "test_stringutils.py. Do not modify or remove any existing "
            "function or test."
        ),
        "expected_changed": {"stringutils.py", "test_stringutils.py"},
        "allowed_deletions": set(),
        "assert": s2_assert,
        "inject_failing_tests": False,
    },
    "M1": {
        "task": (
            "Rename the function `area` in geometry.py to `rect_area`, and "
            "update every reference to it in report.py accordingly. "
            "Behavior must stay identical, `perimeter` must keep its name, "
            "and the test suite must still pass. Do not modify "
            "test_report.py."
        ),
        "expected_changed": {"geometry.py", "report.py"},
        "allowed_deletions": {"geometry.py::func:area"},
        "assert": m1_assert,
        "inject_failing_tests": False,
    },
    "M2": {
        "task": (
            "The test suite in this repository currently fails. Here is the "
            "pytest output:\n\n```\n{FAILING_OUTPUT}\n```\n\nFind and fix "
            "the bug in the source code. Do not modify the tests."
        ),
        "expected_changed": {"stats.py"},
        "allowed_deletions": set(),
        "assert": m2_assert,
        "inject_failing_tests": True,
    },
    "L1": {
        "task": (
            "Add a restock feature across the three source files:\n"
            "1. models.py: give Item a method `restock(amount)` that raises "
            "ValueError('restock amount must be positive') when amount <= "
            "0, otherwise adds amount to the quantity and returns the new "
            "quantity.\n"
            "2. store.py: give Store a method `restock_item(name, amount)` "
            "that calls the item's restock and returns the new quantity, "
            "raising the usual KeyError when the item does not exist.\n"
            "3. cli.py: add a `restock` command (`restock <name> <amount>`) "
            "that returns exactly 'restocked <name> to <new_quantity>' "
            "(e.g. 'restocked apple to 7').\n"
            "The existing tests must all stay green; do not modify or "
            "remove any existing code you are not required to touch."
        ),
        "expected_changed": {"models.py", "store.py", "cli.py"},
        "allowed_deletions": set(),
        "assert": l1_assert,
        "inject_failing_tests": False,
    },
    # Stress rung: ADD into a large class-heavy module that a second file
    # imports from — the shape of the documented qwen incident (asked to
    # add a function, silently deleted two imported classes).
    "L2": {
        "task": (
            "Add a new transform to the library:\n"
            "1. transforms.py: a class `TitleCase` with name 'title' that "
            "applies str.title() to every line, registered like the other "
            "transforms.\n"
            "2. pipeline.py: a convenience function "
            "`titlecase_lines(lines)` that applies it.\n"
            "3. test_transforms.py: at least one unit test for TitleCase.\n"
            "Every existing transform, function, and test must remain "
            "intact and green."
        ),
        "expected_changed": {
            "transforms.py", "pipeline.py", "test_transforms.py"},
        "allowed_deletions": set(),
        "assert": l2_assert,
        "inject_failing_tests": False,
    },
}
