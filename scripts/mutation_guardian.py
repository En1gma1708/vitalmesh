"""Mutation testing of the Safety Guardian (Phase 7, D93). Nothing on disk is ever edited.

    uv run python scripts/mutation_guardian.py [--jobs 6] [--limit N] [--json out.json]

How it works: the Guardian's source is parsed, and for each place where a small change could alter
behaviour (a comparison flipped, `and` swapped for `or`, a condition negated, a number moved by
one, an arithmetic sign flipped) ONE mutant is made in memory. A fresh Python process imports the
mutated module through an import hook (so every importer sees the mutant, not a stale original)
and runs the tests that cover the Guardian. If the tests still pass, the mutant SURVIVED: either
a test is missing or the change is behaviourally equivalent. Survivors are listed with their
line, the original code and the change; each one must be killed by a new test or recorded in
`scripts/mutation_guardian_equivalent.json` with a written reason. The script exits 1 while any
survivor is neither.

Why not mutmut: it needs `fork` and does not run on Windows; this is the same idea in ~150 lines.
"""

from __future__ import annotations

import argparse
import ast
import copy
import importlib.abc
import importlib.machinery
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TARGET = "vitalmesh_wellness.guardian"
TESTS = [
    "libs/wellness/tests/test_claims_and_guardian.py",
    "libs/wellness/tests/test_mealplan.py",
    "libs/wellness/tests/test_guardian_boundaries.py",
    "libs/wellness/tests/test_engine.py",
    "libs/wellness/tests/test_knowledge.py",
    "libs/specialists/tests/test_tools.py",
    "services/agents/tests/test_guardian_gate.py",
    "services/agents/tests/test_release.py",
    "services/t0/tests/test_handler.py",
    "evals/tests/test_harness.py",
]
EQUIVALENT_FILE = ROOT / "scripts" / "mutation_guardian_equivalent.json"

CMP = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
}  # fmt: skip
ARITH = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}


class Mutator(ast.NodeTransformer):
    """Counts mutation sites; changes only the one whose number is ``target``."""

    def __init__(self, target: int | None) -> None:
        self.target, self.count = target, 0
        self.description = ""

    def _site(self, description: str) -> bool:
        index = self.count
        self.count += 1
        if self.target == index:
            self.description = description
            return True
        return False

    # docstrings and f-strings (messages) are not behaviour: leave them alone
    def visit_Expr(self, node: ast.Expr) -> ast.AST:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node
        return self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        return node

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        for i, op in enumerate(node.ops):
            new = CMP.get(type(op))
            if new and self._site(f"{type(op).__name__} -> {new.__name__}"):
                node.ops[i] = new()
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        new = ast.Or if isinstance(node.op, ast.And) else ast.And
        if self._site(f"{type(node.op).__name__} -> {new.__name__}"):
            node.op = new()
        return node

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._site("remove `not`"):
            return node.operand
        return node

    def _negate(self, test: ast.expr) -> ast.expr:
        return ast.UnaryOp(op=ast.Not(), operand=test)

    def visit_If(self, node: ast.If) -> ast.AST:
        self.generic_visit(node)
        if self._site("negate the if-condition"):
            node.test = self._negate(node.test)
        return node

    def visit_IfExp(self, node: ast.IfExp) -> ast.AST:
        self.generic_visit(node)
        if self._site("negate the conditional expression"):
            node.test = self._negate(node.test)
        return node

    def visit_comprehension(self, node: ast.comprehension) -> ast.AST:
        self.generic_visit(node)
        for i, cond in enumerate(node.ifs):
            if self._site("negate a comprehension filter"):
                node.ifs[i] = self._negate(cond)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        v = node.value
        if isinstance(v, bool):
            if self._site(f"{v} -> {not v}"):
                return ast.copy_location(ast.Constant(value=not v), node)
        elif isinstance(v, int | float):
            if self._site(f"{v!r} -> {v + 1!r}"):
                return ast.copy_location(ast.Constant(value=v + 1), node)
            if self._site(f"{v!r} -> {v - 1!r}"):
                return ast.copy_location(ast.Constant(value=v - 1), node)
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        new = ARITH.get(type(node.op))
        if new and self._site(f"{type(node.op).__name__} -> {new.__name__}"):
            node.op = new()
        return node


def mutate(source: str, target: int | None) -> tuple[ast.Module, Mutator]:
    tree = copy.deepcopy(ast.parse(source))
    m = Mutator(target)
    out = m.visit(tree)
    ast.fix_missing_locations(out)
    return out, m


class _Loader(importlib.machinery.SourceFileLoader):
    mutant: int = -1

    def get_code(self, fullname: str) -> Any:  # never use a cached .pyc: it would be the original
        source = self.get_data(self.path).decode("utf-8")
        tree, _ = mutate(source, self.mutant)
        return compile(tree, self.path, "exec")


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, mutant: int) -> None:
        self.mutant = mutant

    def find_spec(self, name: str, path: Any, target: Any = None) -> Any:
        if name != TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            return None
        loader = _Loader(spec.loader.name, spec.loader.path)
        loader.mutant = self.mutant
        spec.loader = loader
        return spec


def run_one(mutant: int) -> int:
    import pytest

    sys.dont_write_bytecode = True
    sys.meta_path.insert(0, _Finder(mutant))
    return int(
        pytest.main(
            ["-q", "-x", "-p", "no:cacheprovider", "--no-header", "-m", "not integration", *TESTS]
        )
    )


def guardian_source() -> tuple[Path, str]:
    path = ROOT / "libs" / "wellness" / "src" / "vitalmesh_wellness" / "guardian.py"
    return path, path.read_text(encoding="utf-8")


def test_mutant(n: int, timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(Path(__file__)), "--one", str(n)],
            cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return n, "killed (timeout)"
    return n, "SURVIVED" if proc.returncode == 0 else "killed"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--one", type=int, help="internal: run the tests under mutant N")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="only the first N mutants")
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    if args.one is not None:
        raise SystemExit(run_one(args.one))

    path, source = guardian_source()
    _, counter = mutate(source, None)
    total = counter.count if not args.limit else min(counter.count, args.limit)
    print(
        f"{counter.count} mutation sites in {path.name}; testing {total} with {args.jobs} workers"
    )
    base = subprocess.run(
        [sys.executable, "-I", str(Path(__file__)), "--one", "-1"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if base.returncode != 0:
        raise SystemExit(
            "the tests fail WITHOUT a mutation; fix that first:\n" + base.stdout[-1500:]
        )

    equivalent: dict[str, str] = {}
    if EQUIVALENT_FILE.exists():
        equivalent = json.loads(EQUIVALENT_FILE.read_text(encoding="utf-8"))
    started = time.monotonic()
    results: dict[int, str] = {}
    with ThreadPoolExecutor(args.jobs) as pool:
        for n, verdict in pool.map(lambda i: test_mutant(i, args.timeout), range(total)):
            results[n] = verdict
            if len(results) % 20 == 0:
                print(f"  {len(results)}/{total} done, {time.monotonic() - started:.0f}s")

    lines = source.splitlines()
    rows = []
    for n in range(total):
        _, m = mutate(source, n)
        node_line = 0
        tree, _ = mutate(source, n)
        for a, b in zip(ast.walk(ast.parse(source)), ast.walk(tree), strict=False):
            if ast.dump(a) != ast.dump(b) and getattr(a, "lineno", 0):
                node_line = a.lineno  # keep the LAST difference: the walk is breadth first, so
                # that is the innermost changed node, not the module or the enclosing function
        rows.append({
            "mutant": n, "result": results[n], "line": node_line, "change": m.description,
            "code": lines[node_line - 1].strip() if node_line else "",
            "recorded_equivalent": equivalent.get(str(n))
            or equivalent.get(f"{node_line}:{m.description}"),
        })  # fmt: skip
    survivors = [r for r in rows if r["result"] == "SURVIVED"]
    open_ = [r for r in survivors if not r["recorded_equivalent"]]
    killed = sum(1 for r in rows if r["result"] != "SURVIVED")
    print(
        f"\n{killed} of {len(rows)} mutants killed; {len(survivors)} survived "
        f"({len(survivors) - len(open_)} recorded as equivalent, {len(open_)} open)"
    )
    for r in survivors:
        tag = "equivalent" if r["recorded_equivalent"] else "OPEN"
        print(f"  [{tag}] #{r['mutant']} line {r['line']}: {r['change']}   {r['code']}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    raise SystemExit(1 if open_ else 0)


if __name__ == "__main__":
    main()
