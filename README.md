# mutants

A [Claude Code](https://claude.com/claude-code) skill that measures what a test
suite actually catches — and triages the results into real gaps and noise.

Line coverage answers *"was this line executed?"*, a question nobody cares
about. A test that calls a function and asserts nothing scores 100%.

Mutation testing answers the question that matters: **if this code were wrong,
would a test fail?**

```
$ mutate.py --paths billing.py --test-command 'pytest -q -x'
mutate: 9 mutants across 1 file(s)
mutate: checking baseline... passes in 0.7s (~0m 6s for 9 mutants, timeout 10s)
  [  1/9] billing.py:1 False -> True                . killed
  [  2/9] billing.py:2 < -> <=                      S SURVIVED
  [  3/9] billing.py:2 > -> >=                      S SURVIVED
  [  4/9] billing.py:2 or -> and                    S SURVIVED
  [  5/9] billing.py:4 * -> /                       . killed
  ...
  mutation score: 56%  (5/9 detected)

  Survivors -- each is a change your suite does not notice:
    billing.py:2  < -> <=
    billing.py:2  > -> >=
    billing.py:2  or -> and
    billing.py:6  + -> -
```

Four concrete edits to production code that the entire suite waves through. The
bounds check is never tested at its bounds, and the member branch is never taken.

## Why this exists

Mutation testing was described in the 1970s and has been the accepted measure of
suite quality ever since. It goes unused because of two costs:

1. **Whole-repo runs take hours**, so they happen once and never again.
2. **Triaging survivors is tedious** — many are *equivalent mutants* that no test
   could ever kill, and separating those from real gaps is slow judgement work.

This skill addresses both: scope to the diff so a run takes minutes, and give an
agent the triage procedure, which is the part people quit on.

## What's in it

| File | Purpose |
| --- | --- |
| `SKILL.md` | The method: scope, verify, triage, write the right test |
| `scripts/mutate.py` | Diff-scoped mutation testing for Python, standard library only |
| `references/triage.md` | Classifying survivors; equivalent mutants; false survivors |
| `references/other-languages.md` | Stryker, PIT, cargo-mutants, and CI wiring |

## Install

Part of the **evidence** plugin by LKC:

```
/plugin marketplace add lkc-studio/claude-plugins
/plugin install evidence@lkc-plugins
```

Then start a new session and ask something like *"are the tests for this module actually any good?"*.

## Using the tool directly

`scripts/mutate.py` is standalone and needs nothing but Python 3.9+.

```bash
# Only mutate what changed -- minutes instead of hours.
mutate.py --since main --test-command 'pytest -q -x'

# A specific file, whole.
mutate.py --paths src/billing.py --test-command 'pytest -q tests/test_billing.py'

# Cap the run on a slow suite.
mutate.py --since HEAD~3 --test-command 'pytest -q' --max-mutants 40
```

Exits non-zero when survivors remain, so it works as a CI check.

**Default operators**: comparison swaps (`<`↔`<=`, `==`↔`!=`), arithmetic
(`+`↔`-`, `*`↔`/`), bitwise, `and`↔`or`, `not` removal, `True`↔`False`, and
`return x` → `return None`. Integer-literal mutation is behind `--numbers`;
it is mostly equivalent mutants and drowns the signal.

### Safety

- Refuses to run unless the suite passes on unmodified source.
- Restores every file on all exit paths, including after an interrupt.
- Derives the per-mutant timeout from the measured baseline, so a mutant that
  turns a loop condition around is cut off quickly rather than after minutes.
- Deletes cached bytecode around every run — without this, a mutant can silently
  execute the previous mutant's `.pyc` and be reported as a survivor it is not.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

21 tests covering diff parsing, operator collection, apply/revert integrity, and
end-to-end runs against a throwaway project — including a regression test for
the stale-bytecode false survivor.

The two end-to-end tests need `pytest` on the interpreter running them and skip
cleanly without it.

## The rule that makes this worth anything

> **Never write a test whose purpose is to kill a mutant.** Write the test that
> asserts the behaviour actually worth guaranteeing.

Mutation score is exactly as gameable as line coverage. A test reverse-engineered
from a mutant asserts an implementation detail and will fight the next refactor.
For boundary survivors, one property test against a slow reference
implementation kills whole families at once — and keeps killing them.

## License

MIT
