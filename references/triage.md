# Triaging survivors

A survivor is a concrete edit to production code that the whole suite waves
through. Deciding what each one means is the work; the tool only finds them.

## Contents

- [The four classes](#the-four-classes)
- [Proving a mutant is equivalent](#proving-a-mutant-is-equivalent)
- [Recognising equivalent mutants by shape](#recognising-equivalent-mutants-by-shape)
- [Writing the test a real gap deserves](#writing-the-test-a-real-gap-deserves)
- [The operator set](#the-operator-set)
- [False survivors: when the tool is wrong](#false-survivors-when-the-tool-is-wrong)
- [Reporting](#reporting)

## The four classes

Read the mutated line in context, then decide:

| Class | Test | Action |
| --- | --- | --- |
| **Real gap** | The mutated behaviour is observably wrong to a caller | Write a test |
| **Equivalent** | No input can distinguish original from mutant | Ignore, record why |
| **Unreachable** | No input reaches the mutated line at all | Delete the dead code |
| **Don't-care** | Distinguishable, but not a promise to anyone | Ignore, record why |

The distinction between *equivalent* and *don't-care* matters. Equivalent means
no test could ever kill it. Don't-care means a test could, but writing one would
freeze behaviour that should stay free to change.

Unreachable survivors are the most valuable of the three non-gaps: mutation
testing has just found dead code. That is a deletion, not an ignore.

## Proving a mutant is equivalent

Do not assume equivalence because a mutant looks harmless. Test the claim:

1. **Find a distinguishing input on paper.** State an input where original and
   mutant differ. If one exists, it is a real gap — write that test.
2. **If none comes to mind, look harder at the edges.** Empty collections, zero,
   negative numbers, exactly-at-the-boundary values, `None`, unicode, duplicates.
   Boundary mutants (`<` to `<=`) differ on exactly one input; that input is
   usually the boundary itself.
3. **Only then call it equivalent**, and write down the argument.

Equivalence is undecidable in general, so this is judgement rather than proof.
The written argument is what makes it reviewable.

## Recognising equivalent mutants by shape

These recur across codebases:

- **Performance-only constants** — cache sizes, chunk widths, buffer
  pre-allocation, batch sizes. Changing them alters speed, not results.
- **Timeouts and retry counts** — when the contract is "eventually succeeds",
  not "succeeds within exactly N attempts".
- **Log levels, log text, metric names** — unless a test asserts on them, and
  usually it should not.
- **Ordering on unordered collections** — swapping a comparison in a sort whose
  output feeds a `set`, or where ties are genuinely arbitrary.
- **Redundant defensive checks** — a guard that duplicates something the caller
  already guarantees. Consider deleting it instead of keeping it untested.
- **Short-circuit reordering** — `a and b` to `b and a` when both are pure and
  neither can fail.

That last one has a trap: if either operand has a side effect, can raise, or is
expensive, the reorder is *not* equivalent. Check purity before waving it
through.

## Writing the test a real gap deserves

The rule that matters:

> Never write a test whose purpose is to kill a mutant. Write the test that
> asserts the behaviour actually worth guaranteeing.

A test reverse-engineered from a mutant asserts an implementation detail. It
will pass, it will raise the score, and it will fight the next refactor. Mutation
score is exactly as gameable as line coverage; the discipline is what makes it
worth anything.

Practically, for each real gap ask: *what promise does this code make to its
caller?* Test that promise. The mutant dies as a side effect.

**Boundary survivors are best killed by properties, not examples.** A survivor
on `<` versus `<=` means the boundary is unasserted. One property test comparing
against a slow, obviously-correct reference kills the whole family at once:

```python
from hypothesis import given, strategies as st

@given(st.lists(st.integers()), st.integers())
def test_matches_reference(items, target):
    assert lower_bound(sorted(items), target) == bisect.bisect_left(sorted(items), target)
```

That single test kills every comparison mutant in the function, and keeps
killing them as the implementation changes.

## The operator set

`mutate.py` applies these by default. Each targets a defect class that occurs in
real code.

| Operator | Mutation | Detects |
| --- | --- | --- |
| Comparison | `<`↔`<=`, `>`↔`>=` | Off-by-one at boundaries |
| Comparison | `==`↔`!=`, `is`↔`is not`, `in`↔`not in` | Inverted conditions |
| Arithmetic | `+`↔`-`, `*`↔`/`, `//`→`/`, `%`→`*` | Wrong formula, sign errors |
| Bitwise | `<<`↔`>>`, `|`↔`&` | Flag and mask errors |
| Boolean | `and`↔`or` | Wrong combination of conditions |
| Not | `not x` → `x` | Inverted guard clauses |
| Constant | `True`↔`False` | Wrong default, wrong flag |
| Return | `return x` → `return None` | Return value never asserted |

Off by default, via `--numbers`:

| Operator | Mutation | Why it is opt-in |
| --- | --- | --- |
| Number | `n` → `n+1` | Hits every literal, including sizes and timeouts; mostly equivalent mutants, which drown the signal |

`return x` → `return None` is the single highest-yield operator. A survivor
there means a function's return value is never asserted anywhere — usually a
test that calls the function and checks only that it did not raise.

## False survivors: when the tool is wrong

Before triaging a survivor as a real gap, rule out these. A false survivor sends
you to write a test for a gap that does not exist.

- **Stale bytecode.** CPython treats a cached `.pyc` as current when the source's
  mtime (whole seconds) and size both match. Mutants are written in rapid
  succession and operator swaps usually preserve length, so a mutant can silently
  run the previous mutant's bytecode. `mutate.py` handles this by setting
  `PYTHONDONTWRITEBYTECODE=1` and deleting cached bytecode around every run. Any
  home-grown mutation script must do the same.
- **Test caching.** Tools that skip "unaffected" tests (`pytest-testmon`, build
  caches, `--lf`) will skip the very tests the mutant should fail. Disable them
  in the mutation command.
- **The suite does not import the mutated module.** If the tests import an
  installed copy rather than the working tree, mutants never take effect. Check
  by mutating one line by hand and confirming a test fails.
- **Nondeterministic tests.** A flaky suite produces random verdicts. Stabilise
  it first; mutation results over a flaky suite are noise.

A quick sanity check for any of these: take one killed mutant and one survivor,
apply each by hand, and run the suite. If a "survivor" fails by hand, the harness
is wrong, not the tests.

## Reporting

For each run, report:

- mutation score before and after,
- every survivor that became killed, with the test that kills it,
- every survivor deliberately left alive, with its class and the reason.

That last list is the deliverable most worth keeping. A survivor with a written
reason is a decision on record; a survivor with no reason is an unexamined hole
that the next run will surface again.

Weight the summary by what the code does. Three survivors in a pricing
calculation matter more than thirty in a logging helper — say so, rather than
reporting a bare percentage.
