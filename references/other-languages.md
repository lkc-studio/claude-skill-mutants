# Mutation testing outside Python

The method in `SKILL.md` is language-independent: scope to the diff, verify the
baseline, triage survivors, write the test the gap deserves. Only the runner
changes.

Prefer the established tool for the language over writing a mutator. These
projects have solved compilation caching, parallelism, and equivalent-mutant
heuristics that a hand-rolled script will not.

## Choosing by language

| Language | Tool | Diff scoping |
| --- | --- | --- |
| JS / TS | `stryker` | `--since main` (built in) |
| C# / .NET | `stryker-net` | `--since` |
| Scala | `stryker4s` | `--since` |
| Java / Kotlin | PIT (`pitest`) | `scmMutationCoverage` goal |
| Go | `go-mutesting`, `gremlins` | none built in; pass changed files |
| Rust | `cargo-mutants` | `--in-diff <file.diff>` |
| Ruby | `mutant` | `--since main` |
| PHP | `infection` | `--git-diff-lines` |
| C / C++ | `mull`, `dextool mutate` | none built in; scope by file |

## JS / TS — Stryker

Diff scoping is first-class and the reason Stryker is usable in CI:

```bash
npx stryker run --since main
```

```jsonc
// stryker.conf.json
{
  "testRunner": "vitest",
  "reporters": ["clear-text", "progress"],
  "since": { "ignoreStatic": true },
  "thresholds": { "high": 80, "low": 60, "break": null }
}
```

Set `"break": null` while adopting. A build that fails on mutation score before
the team can triage survivors gets the whole thing switched off within a week.

`ignoreStatic` skips mutants in code that runs only at module load, which are
disproportionately equivalent.

## Java / Kotlin — PIT

```xml
<plugin>
  <groupId>org.pitest</groupId>
  <artifactId>pitest-maven</artifactId>
  <configuration>
    <targetClasses><param>com.example.billing.*</param></targetClasses>
    <mutators><value>DEFAULTS</value></mutators>
  </configuration>
</plugin>
```

```bash
mvn org.pitest:pitest-maven:scmMutationCoverage \
    -Dinclude=ADDED,MODIFIED -Dorigin=main
```

PIT mutates bytecode rather than source, so it is fast and needs no rebuild per
mutant. Its `DEFAULTS` mutator set is conservative; `STRONGER` adds operators
with more equivalent mutants.

## Rust — cargo-mutants

```bash
git diff -U0 main > /tmp/changes.diff
cargo mutants --in-diff /tmp/changes.diff
```

`-U0` matters: with context lines, unchanged code gets mutated too.

## Go — gremlins

No diff mode, so scope by hand:

```bash
gremlins unleash --dry-run=false $(git diff --name-only main -- '*.go' | xargs -n1 dirname | sort -u)
```

## Ruby, PHP

```bash
bundle exec mutant run --since main -- 'Billing*'   # Ruby
vendor/bin/infection --git-diff-lines --git-diff-base=main   # PHP
```

## Wiring into CI

Two rules keep this sustainable:

1. **Diff-scoped, always.** Whole-repo mutation runs belong in a manual audit,
   never in per-PR CI.
2. **Report, do not gate — at first.** Post survivors as a PR comment and let
   people triage. Gate on the score only once the team has been triaging for a
   few months and equivalent mutants in the codebase are known.

```yaml
# GitHub Actions sketch
- name: Mutation test changed code
  run: npx stryker run --since origin/${{ github.base_ref }}
  continue-on-error: true
```

`continue-on-error` is deliberate during adoption. Remove it when survivors are
routinely triaged rather than ignored.

## When a runner does not exist

Writing a mutator is reasonable only when no maintained tool covers the
language, and the language has a usable parser. `scripts/mutate.py` is about 400
lines because Python ships `ast` and `ast.unparse`.

Regardless of language, a hand-rolled runner must:

- **Verify the baseline passes** before mutating anything, or every mutant is
  "killed" for the wrong reason.
- **Restore sources on every exit path**, including interrupts.
- **Defeat build and bytecode caches.** Same-length edits written within the
  same second are the norm in mutation testing, and most caches key on mtime and
  size. This produces false survivors — see `triage.md`.
- **Bound each run.** A mutant that turns a loop condition around will hang;
  derive the timeout from the measured baseline rather than fixing it.
- **Sanity-check itself.** Apply one mutant by hand and confirm the suite fails.
