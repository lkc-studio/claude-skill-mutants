#!/usr/bin/env python3
"""mutate: diff-scoped mutation testing for Python.

Mutation testing measures what a test suite actually catches. It edits the
source in small, plausible ways -- a `<` becomes `<=`, a `+` becomes `-`, a
`return x` becomes `return None` -- and reruns the tests. A mutant that the
suite fails to notice is a bug the suite would also fail to notice.

Verdicts:

    killed    tests failed  -> the suite catches this class of bug
    SURVIVED  tests passed  -> nothing asserts on this behaviour
    timeout   tests hung    -> counted as killed (the change was detectable)

The default scope is `git diff` rather than the whole repository. Whole-repo
runs take hours and get run once, if ever; a diff-scoped run takes minutes and
fits into code review, which is the only way this technique gets used in
practice.

Usage:
    mutate.py --since main --test-command 'pytest -q -x'
    mutate.py --paths src/algorithms/binary_search.py --test-command 'pytest -q'
    mutate.py --since HEAD~3 --test-command 'pytest -q' --timeout 60 --max-mutants 40

Requires only the standard library.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Mutation", "collect_mutations", "changed_lines", "parse_diff"]

# --------------------------------------------------------------------------
# Mutation operators
# --------------------------------------------------------------------------

# Boundary and negation errors are by far the most common real defects, so the
# default operator set targets them. Numeric-literal mutation is available via
# --numbers but is off by default: it produces many equivalent mutants and
# drowns the signal.

COMPARE_SWAPS: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}

BINOP_SWAPS: dict[type[ast.operator], type[ast.operator]] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
    ast.FloorDiv: ast.Div,
    ast.Mod: ast.Mult,
    ast.Pow: ast.Mult,
    ast.LShift: ast.RShift,
    ast.RShift: ast.LShift,
    ast.BitOr: ast.BitAnd,
    ast.BitAnd: ast.BitOr,
}

BOOLOP_SWAPS: dict[type[ast.boolop], type[ast.boolop]] = {
    ast.And: ast.Or,
    ast.Or: ast.And,
}

OPERATOR_SYMBOLS: dict[type[ast.AST], str] = {
    ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
    ast.Eq: "==", ast.NotEq: "!=", ast.Is: "is", ast.IsNot: "is not",
    ast.In: "in", ast.NotIn: "not in",
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
    ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**",
    ast.LShift: "<<", ast.RShift: ">>", ast.BitOr: "|", ast.BitAnd: "&",
    ast.And: "and", ast.Or: "or",
}


@dataclass
class Mutation:
    """One mutation: how to apply it, how to undo it, and how to describe it."""

    path: Path
    line: int
    operator: str
    description: str
    _apply: Callable[[], None] = field(repr=False)
    _revert: Callable[[], None] = field(repr=False)

    def apply(self) -> None:
        self._apply()

    def revert(self) -> None:
        self._revert()


def _symbol(node: ast.AST) -> str:
    return OPERATOR_SYMBOLS.get(type(node), type(node).__name__)


def _iter_parents(tree: ast.AST) -> Iterator[tuple[ast.AST, str, int | None, ast.AST]]:
    """Yield (parent, field_name, index_or_None, child) for every child node."""
    for parent in ast.walk(tree):
        for name, value in ast.iter_fields(parent):
            if isinstance(value, ast.AST):
                yield parent, name, None, value
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, ast.AST):
                        yield parent, name, i, item


def _replacer(
    parent: ast.AST, name: str, index: int | None, old: ast.AST, new: ast.AST
) -> tuple[Callable[[], None], Callable[[], None]]:
    """Build (apply, revert) closures that swap a child node in its parent."""

    def put(node: ast.AST) -> None:
        if index is None:
            setattr(parent, name, node)
        else:
            getattr(parent, name)[index] = node

    return (lambda: put(new)), (lambda: put(old))


def collect_mutations(
    tree: ast.AST, path: Path, target_lines: set[int] | None, *, numbers: bool = False
) -> list[Mutation]:
    """Find every mutation site in `tree`, restricted to `target_lines` if given."""
    found: list[Mutation] = []

    def in_scope(node: ast.AST) -> bool:
        line = getattr(node, "lineno", None)
        if line is None:
            return False
        return target_lines is None or line in target_lines

    for parent, name, index, child in _iter_parents(tree):
        anchor = parent if not hasattr(child, "lineno") else child
        if not in_scope(anchor):
            continue
        line = getattr(anchor, "lineno", 0)

        # Comparison / arithmetic / boolean operator swaps.
        swap_table: dict[type, type] | None = None
        if isinstance(child, ast.cmpop):
            swap_table = COMPARE_SWAPS  # type: ignore[assignment]
        elif isinstance(child, ast.operator):
            swap_table = BINOP_SWAPS  # type: ignore[assignment]
        elif isinstance(child, ast.boolop):
            swap_table = BOOLOP_SWAPS  # type: ignore[assignment]

        if swap_table is not None:
            replacement = swap_table.get(type(child))
            if replacement is not None:
                new_node = replacement()
                apply_fn, revert_fn = _replacer(parent, name, index, child, new_node)
                found.append(
                    Mutation(
                        path, line, "operator",
                        f"{_symbol(child)} -> {_symbol(new_node)}",
                        apply_fn, revert_fn,
                    )
                )
            continue

        # `not x` -> `x`
        if isinstance(child, ast.UnaryOp) and isinstance(child.op, ast.Not):
            apply_fn, revert_fn = _replacer(parent, name, index, child, child.operand)
            found.append(
                Mutation(path, line, "not", "remove `not`", apply_fn, revert_fn)
            )
            continue

        # True <-> False
        if isinstance(child, ast.Constant) and isinstance(child.value, bool):
            new_const = ast.Constant(value=not child.value)
            apply_fn, revert_fn = _replacer(parent, name, index, child, new_const)
            found.append(
                Mutation(
                    path, line, "constant",
                    f"{child.value} -> {not child.value}", apply_fn, revert_fn,
                )
            )
            continue

        # n -> n + 1  (opt-in: noisy, many equivalent mutants)
        if (
            numbers
            and isinstance(child, ast.Constant)
            and isinstance(child.value, int)
            and not isinstance(child.value, bool)
        ):
            new_const = ast.Constant(value=child.value + 1)
            apply_fn, revert_fn = _replacer(parent, name, index, child, new_const)
            found.append(
                Mutation(
                    path, line, "number",
                    f"{child.value} -> {child.value + 1}", apply_fn, revert_fn,
                )
            )
            continue

    # `return x` -> `return None`
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Return)
            and node.value is not None
            and in_scope(node)
            and not (
                isinstance(node.value, ast.Constant) and node.value.value is None
            )
        ):
            original = node.value
            none_const = ast.Constant(value=None)

            def make(target: ast.Return, old: ast.expr, new: ast.expr) -> tuple:
                return (
                    lambda: setattr(target, "value", new),
                    lambda: setattr(target, "value", old),
                )

            apply_fn, revert_fn = make(node, original, none_const)
            found.append(
                Mutation(
                    path, node.lineno, "return",
                    "return <expr> -> return None", apply_fn, revert_fn,
                )
            )

    found.sort(key=lambda m: (m.line, m.description))
    return found


# --------------------------------------------------------------------------
# Diff scoping
# --------------------------------------------------------------------------

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_diff(diff: str) -> dict[str, set[int]]:
    """Map each file in a unified diff to the set of its added/changed lines."""
    result: dict[str, set[int]] = {}
    current: str | None = None
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                current = None
            else:
                current = target[2:] if target.startswith("b/") else target
                result.setdefault(current, set())
            continue
        match = HUNK_RE.match(raw)
        if match and current is not None:
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            result[current].update(range(start, start + count))
    return {path: lines for path, lines in result.items() if lines}


def changed_lines(ref: str, paths: Sequence[str]) -> dict[str, set[int]]:
    """Ask git which lines changed since `ref`."""
    command = ["git", "diff", "--unified=0", ref, "--", *(paths or ["*.py"])]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise SystemExit(f"mutate: git diff failed:\n{completed.stderr.strip()}")
    return parse_diff(completed.stdout)


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

KILLED, SURVIVED, TIMEOUT = "killed", "SURVIVED", "timeout"

# A hanging mutant must be cut off. The ceiling is derived from the measured
# baseline rather than fixed: too high and every hang costs the full ceiling,
# too low and a slow suite reports false timeouts.
BASELINE_TIMEOUT = 600.0
TIMEOUT_FACTOR = 5.0
MIN_TIMEOUT = 10.0


# CPython decides a cached .pyc is current by comparing the source's mtime
# (whole seconds) and size. Mutants are written in rapid succession and an
# operator swap usually preserves length, so two different mutants can share
# both -- and the second silently runs the first one's bytecode. That shows up
# as a SURVIVED verdict for a mutant that was never actually executed: a false
# gap, which is the most damaging way this tool could be wrong. Disable writing
# bytecode, and delete any cache that already exists.
TEST_ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def invalidate_bytecode(path: Path) -> None:
    """Remove any cached bytecode for `path`."""
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        for stale in cache.glob(f"{path.stem}.*.pyc"):
            stale.unlink(missing_ok=True)


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the whole process group and reap it."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def run_tests(command: str, timeout: float) -> str:
    """Run the suite. Returns KILLED / SURVIVED / TIMEOUT.

    The timeout path must kill the entire process tree, not just the shell.
    `shell=True` means the command runs as a child of /bin/sh; killing only the
    shell orphans the test runner, which keeps executing. Since a timeout here
    usually means a mutant turned a loop condition around, that orphan spins at
    100% CPU forever. A handful of them will bring a machine to its knees --
    which is exactly what happened before `start_new_session` was added.
    """
    process = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=TEST_ENV,
        start_new_session=True,  # own process group, so killpg reaches every child
    )
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        return TIMEOUT
    # Tests passing on mutated source means the mutant went unnoticed.
    return SURVIVED if process.returncode == 0 else KILLED


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mutate.py", description="Diff-scoped mutation testing for Python."
    )
    parser.add_argument(
        "--test-command", required=True, help="command that runs the suite"
    )
    parser.add_argument(
        "--since", help="git ref; mutate only lines changed since it (recommended)"
    )
    parser.add_argument(
        "--paths", nargs="*", default=[], help="files to mutate (whole file if no --since)"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        help="per-mutant seconds (default: 5x the measured baseline, min 10)",
    )
    parser.add_argument("--max-mutants", type=int, default=0, help="0 = no cap")
    parser.add_argument(
        "--numbers", action="store_true", help="also mutate integer literals (noisy)"
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if not args.since and not args.paths:
        raise SystemExit("mutate: give --since REF or --paths FILE...")

    # Work out what to mutate.
    scopes: dict[Path, set[int] | None] = {}
    if args.since:
        for name, lines in changed_lines(args.since, args.paths).items():
            if name.endswith(".py") and Path(name).is_file():
                scopes[Path(name)] = lines
    else:
        for name in args.paths:
            path = Path(name)
            if path.is_file() and path.suffix == ".py":
                scopes[path] = None

    if not scopes:
        print("mutate: nothing in scope -- no changed .py lines found.")
        return 0

    # Collect sites.
    plans: list[tuple[Path, ast.Module, list[Mutation], str]] = []
    for path, lines in sorted(scopes.items()):
        source = path.read_text()
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            print(f"mutate: skipping {path}: {exc}", file=sys.stderr)
            continue
        found = collect_mutations(tree, path, lines, numbers=args.numbers)
        if found:
            plans.append((path, tree, found, source))

    total = sum(len(p[2]) for p in plans)
    if total == 0:
        print("mutate: no mutation sites in scope.")
        return 0

    # Baseline: the suite must pass before any of this means anything.
    if not args.quiet:
        print(f"mutate: {total} mutants across {len(plans)} file(s)")
        print("mutate: checking baseline...", end=" ", flush=True)
    started = time.monotonic()
    baseline = run_tests(args.test_command, args.timeout or BASELINE_TIMEOUT)
    baseline_seconds = time.monotonic() - started
    if baseline != SURVIVED:
        print()
        raise SystemExit(
            "mutate: baseline FAILED -- the suite must pass on unmodified source.\n"
            "  Every mutant would be reported as killed for the wrong reason.\n"
            f"  Command: {args.test_command}"
        )
    # A mutant that hangs must be cut off, but a fixed ceiling is either far
    # longer than the suite needs (every hang costs it in full) or shorter than
    # a slow suite takes (every mutant is a false timeout). Derive it instead.
    timeout = args.timeout or max(MIN_TIMEOUT, baseline_seconds * TIMEOUT_FACTOR)
    if not args.quiet:
        capped = min(total, args.max_mutants) if args.max_mutants else total
        estimate = int(baseline_seconds * capped)
        print(
            f"passes in {baseline_seconds:.1f}s "
            f"(~{estimate // 60}m {estimate % 60}s for {capped} mutants, "
            f"timeout {timeout:.0f}s)"
        )

    survivors: list[Mutation] = []
    counts = {KILLED: 0, SURVIVED: 0, TIMEOUT: 0}
    done = 0

    for path, tree, found, source in plans:
        backup = path.with_suffix(path.suffix + ".mutants-backup")
        shutil.copy2(path, backup)
        try:
            for mutation in found:
                if args.max_mutants and done >= args.max_mutants:
                    break
                done += 1
                mutation.apply()
                try:
                    path.write_text(ast.unparse(tree) + "\n")
                    invalidate_bytecode(path)
                    verdict = run_tests(args.test_command, timeout)
                finally:
                    mutation.revert()
                    shutil.copy2(backup, path)
                    invalidate_bytecode(path)

                counts[verdict] += 1
                if verdict == SURVIVED:
                    survivors.append(mutation)
                if not args.quiet:
                    mark = {KILLED: ".", SURVIVED: "S", TIMEOUT: "T"}[verdict]
                    print(
                        f"  [{done:>3}/{total}] {path}:{mutation.line} "
                        f"{mutation.description:<28} {mark} {verdict}"
                    )
        finally:
            shutil.copy2(backup, path)
            backup.unlink(missing_ok=True)

    # Report.
    tested = sum(counts.values())
    detected = counts[KILLED] + counts[TIMEOUT]
    score = 100.0 * detected / tested if tested else 0.0

    print()
    print(f"  mutation score: {score:.0f}%  ({detected}/{tested} detected)")
    print(f"  killed {counts[KILLED]}   timeout {counts[TIMEOUT]}   "
          f"SURVIVED {counts[SURVIVED]}")

    if survivors:
        print()
        print("  Survivors -- each is a change your suite does not notice:")
        for mutation in survivors:
            print(f"    {mutation.path}:{mutation.line}  {mutation.description}")
        print()
        print("  Triage each one: a real gap needs a test; an equivalent mutant")
        print("  (same behaviour after the change) does not. See references/triage.md")

    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
