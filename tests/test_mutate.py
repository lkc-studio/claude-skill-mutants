"""Tests for mutate. Standard library only -- run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MUTATE_PATH = ROOT / "scripts" / "mutate.py"

_spec = importlib.util.spec_from_file_location("mutate", MUTATE_PATH)
assert _spec and _spec.loader
mutate = importlib.util.module_from_spec(_spec)
# Register before exec: @dataclass resolves annotations via sys.modules.
sys.modules["mutate"] = mutate
_spec.loader.exec_module(mutate)


def sites(source: str, lines: set[int] | None = None, **kw: object) -> list:
    tree = ast.parse(textwrap.dedent(source))
    return mutate.collect_mutations(tree, Path("x.py"), lines, **kw)  # type: ignore[arg-type]


class TestParseDiff(unittest.TestCase):
    def test_extracts_added_line_ranges(self) -> None:
        diff = textwrap.dedent(
            """\
            diff --git a/src/a.py b/src/a.py
            --- a/src/a.py
            +++ b/src/a.py
            @@ -10,0 +11,3 @@
            +one
            +two
            +three
            """
        )
        self.assertEqual(mutate.parse_diff(diff), {"src/a.py": {11, 12, 13}})

    def test_hunk_without_count_means_one_line(self) -> None:
        diff = "--- a/f.py\n+++ b/f.py\n@@ -4 +4 @@\n+x\n"
        self.assertEqual(mutate.parse_diff(diff), {"f.py": {4}})

    def test_multiple_files_and_hunks(self) -> None:
        diff = textwrap.dedent(
            """\
            +++ b/a.py
            @@ -1 +1,2 @@
            @@ -20,0 +30,1 @@
            +++ b/b.py
            @@ -5,0 +6,2 @@
            """
        )
        self.assertEqual(mutate.parse_diff(diff), {"a.py": {1, 2, 30}, "b.py": {6, 7}})

    def test_deleted_file_is_ignored(self) -> None:
        diff = "--- a/gone.py\n+++ /dev/null\n@@ -1,3 +0,0 @@\n"
        self.assertEqual(mutate.parse_diff(diff), {})


class TestCollectMutations(unittest.TestCase):
    def test_finds_comparison_swap(self) -> None:
        found = sites("def f(a, b):\n    return a < b\n")
        self.assertIn("< -> <=", [m.description for m in found])

    def test_finds_arithmetic_and_boolean_swaps(self) -> None:
        descriptions = [m.description for m in sites("x = (a + b) and c\n")]
        self.assertIn("+ -> -", descriptions)
        self.assertIn("and -> or", descriptions)

    def test_finds_not_removal(self) -> None:
        self.assertIn("remove `not`", [m.description for m in sites("y = not x\n")])

    def test_finds_bool_constant_flip(self) -> None:
        self.assertIn("True -> False", [m.description for m in sites("flag = True\n")])

    def test_finds_return_none(self) -> None:
        found = sites("def f():\n    return 42\n")
        self.assertIn("return <expr> -> return None", [m.description for m in found])

    def test_bare_return_is_not_mutated(self) -> None:
        found = sites("def f():\n    return\n")
        self.assertEqual([m for m in found if m.operator == "return"], [])

    def test_numbers_are_off_by_default(self) -> None:
        self.assertEqual([m for m in sites("x = 5\n") if m.operator == "number"], [])

    def test_numbers_can_be_enabled(self) -> None:
        found = sites("x = 5\n", numbers=True)
        self.assertIn("5 -> 6", [m.description for m in found])

    def test_line_scope_excludes_other_lines(self) -> None:
        source = "a = 1 < 2\nb = 3 < 4\nc = 5 < 6\n"
        found = sites(source, lines={2})
        self.assertEqual([m.line for m in found], [2])

    def test_empty_scope_finds_nothing(self) -> None:
        self.assertEqual(sites("a = 1 < 2\n", lines=set()), [])


class TestApplyRevert(unittest.TestCase):
    def test_apply_then_revert_restores_source(self) -> None:
        source = "def f(a, b):\n    return a < b\n"
        tree = ast.parse(source)
        original = ast.unparse(tree)
        found = mutate.collect_mutations(tree, Path("x.py"), None)
        for mutation in found:
            mutation.apply()
            self.assertNotEqual(ast.unparse(tree), original, mutation.description)
            mutation.revert()
            self.assertEqual(ast.unparse(tree), original, mutation.description)

    def test_mutation_actually_changes_semantics(self) -> None:
        tree = ast.parse("def f(a, b):\n    return a < b\n")
        found = [m for m in mutate.collect_mutations(tree, Path("x.py"), None)
                 if m.description == "< -> <="]
        self.assertEqual(len(found), 1)
        found[0].apply()
        self.assertIn("a <= b", ast.unparse(tree))


class TestEndToEnd(unittest.TestCase):
    """Run the real script against a throwaway project."""

    # Deliberately not binary search: boundary mutants there tend to produce
    # infinite loops, which count as killed and cost a full timeout each.
    SOURCE = textwrap.dedent(
        """\
        def apply_discount(price, percent, member=False):
            if percent < 0 or percent > 100:
                raise ValueError("percent out of range")
            discount = price * percent / 100
            if member:
                discount = discount + 5
            return price - discount
        """
    )

    # One happy-path assertion: never touches the bounds or the member branch.
    WEAK_TEST = textwrap.dedent(
        """\
        from billing import apply_discount

        def test_typical_case():
            assert apply_discount(100, 10) == 90
        """
    )

    STRONG_TEST = textwrap.dedent(
        """\
        import pytest
        from billing import apply_discount

        def test_typical_case():
            assert apply_discount(100, 10) == 90

        def test_accepts_the_inclusive_bounds():
            assert apply_discount(100, 0) == 100
            assert apply_discount(100, 100) == 0

        def test_rejects_out_of_range():
            for bad in (-1, 101):
                with pytest.raises(ValueError):
                    apply_discount(100, bad)

        def test_members_get_the_extra_five():
            assert apply_discount(100, 10, member=True) == 85
        """
    )

    def _run(self, test_source: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "billing.py").write_text(self.SOURCE)
            (project / "test_billing.py").write_text(test_source)
            return subprocess.run(
                [
                    sys.executable, str(MUTATE_PATH),
                    "--paths", "billing.py",
                    "--test-command", f"{sys.executable} -m pytest -q -x",
                    "--timeout", "30",
                ],
                cwd=project, capture_output=True, text=True,
            )

    @unittest.skipUnless(
        importlib.util.find_spec("pytest"), "pytest not installed"
    )
    def test_weak_suite_leaves_survivors(self) -> None:
        result = self._run(self.WEAK_TEST)
        self.assertIn("mutation score", result.stdout, result.stdout + result.stderr)
        self.assertIn("Survivors", result.stdout)
        self.assertEqual(result.returncode, 1)

    @unittest.skipUnless(
        importlib.util.find_spec("pytest"), "pytest not installed"
    )
    def test_no_false_survivor_from_stale_bytecode(self) -> None:
        """Regression: `price - discount` -> `price + discount` must be killed.

        Same source length as its neighbours and written within the same second,
        so a cached .pyc would be reused and the mutant would never run --
        reporting a gap that does not exist.
        """
        result = self._run(self.WEAK_TEST)
        for line in result.stdout.splitlines():
            if "billing.py:7" in line and "- -> +" in line:
                self.assertIn("killed", line, f"false survivor: {line}")
                break
        else:
            self.fail(f"mutant not found in output:\n{result.stdout}")

    @unittest.skipUnless(
        importlib.util.find_spec("pytest"), "pytest not installed"
    )
    def test_strong_suite_kills_more(self) -> None:
        weak = self._run(self.WEAK_TEST).stdout
        strong = self._run(self.STRONG_TEST).stdout

        def survived(output: str) -> int:
            for line in output.splitlines():
                if "SURVIVED" in line and "killed" in line:
                    return int(line.split("SURVIVED")[1].strip())
            raise AssertionError(f"no summary line in:\n{output}")

        self.assertLess(survived(strong), survived(weak))

    def test_baseline_failure_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "billing.py").write_text(self.SOURCE)
            result = subprocess.run(
                [
                    sys.executable, str(MUTATE_PATH),
                    "--paths", "billing.py",
                    "--test-command", "exit 1",
                    "--timeout", "30",
                ],
                cwd=project, capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("baseline FAILED", result.stderr)

    def test_source_file_is_restored_afterwards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            target = project / "billing.py"
            target.write_text(self.SOURCE)
            subprocess.run(
                [
                    sys.executable, str(MUTATE_PATH),
                    "--paths", "billing.py",
                    "--test-command", "true",
                    "--timeout", "30", "--quiet", "--max-mutants", "3",
                ],
                cwd=project, capture_output=True, text=True,
            )
            self.assertEqual(target.read_text(), self.SOURCE, "source not restored")
            self.assertEqual(list(project.glob("*.mutants-backup")), [])


if __name__ == "__main__":
    unittest.main()
