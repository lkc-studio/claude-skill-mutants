---
name: mutants
description: This skill should be used when the question is whether a test suite is actually any good — when the user says "are these tests any good", "what am I not testing", "coverage is 100% but I still shipped a bug", "improve the tests for this", "is this well tested", "find gaps in my tests", "mutation testing", or asks for tests to be written for existing code and the useful ones need identifying. Measures which bugs the suite would actually catch by mutating the source, then triages the survivors into real gaps and equivalent mutants.
---

# Mutants: measure what the tests actually catch

Line coverage answers "was this line executed?" — a question no one cares
about. A test that calls a function and asserts nothing gives 100% coverage and
catches nothing.

Mutation testing answers the question that matters: **if this code were wrong,
would a test fail?** It introduces a small, plausible defect — `<` becomes
`<=`, `return x` becomes `return None` — and reruns the suite.

```
mutant killed    -> a test failed  -> the suite catches this class of bug
mutant SURVIVED  -> tests passed   -> nothing asserts on this behaviour
```

A survivor is not a hypothetical. It is a concrete edit to production code that
the entire suite waves through.

## The reason nobody does this

Mutation testing was described in the 1970s and has been the academically
accepted measure of suite quality ever since. It stays unused because of two
costs:

1. **Whole-repo runs take hours.** So they get run once, in an audit, and never
   again.
2. **Triaging survivors is tedious.** Many survivors are *equivalent mutants* —
   the code changed but the behaviour did not, so no test could ever kill them.
   Separating those from real gaps is slow, unglamorous judgement work.

Both are addressable. Fix the first by scoping to the diff. The second is the
work this skill exists to do.

## Step 1: scope to the diff, not the repo

Mutate only what changed. This turns hours into minutes and makes the technique
usable during code review, which is the only place it survives contact with
real work.

```bash
scripts/mutate.py --since main --test-command 'pytest -q -x'
```

`mutate.py` needs only the standard library. It refuses to start unless the
suite passes on unmodified source — without that, every mutant is "killed" for
the wrong reason and the score is meaningless.

Control cost when the suite is slow:

- `--max-mutants N` caps the run.
- Narrow `--test-command` to the relevant tests; total time is
  roughly `mutants × suite_time`.
- `-x` (fail fast) makes killed mutants return sooner. Most mutants are killed,
  so this is usually the largest single saving.

For other languages, use the mature tool with its diff flag — `stryker --since`
for JS/TS/C#, PIT with `scmMutationCoverage` for Java. See
`references/other-languages.md`. The method below is identical.

## Step 2: triage every survivor

Before treating any survivor as real, rule out a broken harness. A false
survivor is worse than no result: it sends you to write a test for a gap that
does not exist.

The usual cause in Python is **stale bytecode** — CPython considers a cached
`.pyc` current when the source's mtime (whole seconds) and size both match, and
operator swaps preserve length. `mutate.py` defeats this, but any home-grown
script or caching test runner (`pytest-testmon`, `--lf`, build caches) will
produce phantom gaps. Cheap check: apply one survivor by hand and run the suite.
If it fails by hand, the harness is wrong, not the tests.

Then classify. This is judgement, not mechanics — read the mutated line in
context:

| Class | Meaning | Action |
| --- | --- | --- |
| **Real gap** | The mutated behaviour is wrong, and nothing noticed | Write a test |
| **Equivalent mutant** | Behaviour is genuinely unchanged | Ignore, record why |
| **Unreachable** | The mutated branch cannot be triggered | Delete the dead branch |
| **Don't-care** | Real change, but the behaviour is not a contract | Ignore, record why |

Common equivalent mutants — recognise these rather than testing them:

- Performance-only changes: a cache size, a chunk width, a pre-allocation hint.
- Timeouts and retry counts, when only "eventually" is contractual.
- Log levels, log message content, metric names.
- Ordering changes on a collection where order is not part of the contract.
- Defensive checks that duplicate a guarantee the caller already enforces.

`references/triage.md` covers the harder calls, including how to test that a
mutant is equivalent instead of assuming it.

## Step 3: write the test the gap deserves

The strong temptation here is to write a test that kills the mutant. Resist it.

> **Never write a test whose purpose is to kill a mutant.** Write the test that
> asserts the behaviour actually worth guaranteeing. If it kills the mutant,
> good — that was evidence the behaviour was unguarded.

A test reverse-engineered from a mutant tends to assert an implementation
detail, and it will fight every future refactor. That failure mode is exactly
what gave coverage-chasing its bad name; mutation score is just as gameable.

For boundary survivors (`<` vs `<=`), the highest-value test is usually not a
new example but a **property**: compare against a slow, obviously-correct
reference implementation across generated inputs. One property test kills whole
families of boundary mutants at once. With `hypothesis` available, prefer it.

## Step 4: verify

Rerun `mutate.py` on the same scope. The gaps that were addressed should now be
killed. Report:

- the mutation score before and after,
- each survivor that was fixed, and the test that now kills it,
- each survivor deliberately left alive, and why it is equivalent or don't-care.

That last list matters. A survivor with a written reason is a decision; a
survivor with no reason is an unexamined hole.

## Reading the score

Do not chase 100%. A healthy diff-scoped score is high because the diff is
small and freshly considered; a low score on new code is a genuine warning.

The score is a prompt for attention, not a target. Three survivors on a payment
calculation matter more than thirty on a logging module — weight by what the
code does, and say so in the report rather than reporting a bare percentage.

## Resources

- **`scripts/mutate.py`** — diff-scoped mutation testing for Python; AST-based,
  standard library only, restores sources on any exit path.
- **`references/triage.md`** — classifying survivors, recognising equivalent
  mutants, and the operator set with what each one detects.
- **`references/other-languages.md`** — diff-scoped mutation testing in JS/TS,
  Java, Go, Rust, C#, and how to wire it into CI on changed files only.
