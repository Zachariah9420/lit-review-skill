# Evals

Two scripts, no network, seconds to run. Run both before shipping any change to `scripts/`.

```bash
python evals/test_regression.py     # 50 frozen cases
python evals/mutation_check.py      # 9 mutations; each must be caught
python evals/make_fixtures.py       # regenerate fixtures/ (only when adding a case)
```

## test_regression.py

Every case corresponds to a defect that actually occurred. The id prefix records where it was found:

| Prefix | Source |
|---|---|
| `TS-*` | black-box stress test (6 parallel agents, 38 cases) |
| `CX-*` | independent source review by an unrelated model (24 findings) |
| `DR-*` | design review of the matching logic |
| `REG-*` | guards normal behaviour against over-correction |

It calls the production functions (`rank_candidates`, `decide_verdict`, `norm_title`, …) with frozen candidate data, plus the CLIs against fixture files. No API keys, no network, no flakiness.

## mutation_check.py

A green suite proves nothing about detection power. This script re-introduces each fixed bug one at a time and requires that a named case fails. If a mutation survives, the corresponding protection has no test behind it.

It found two tests of mine that had re-implemented the ranking logic instead of calling it — they would have stayed green forever while the real code rotted. **Tests must call production functions.**

## doc_scan.py

Docs drift faster than code in this project — commands get added and the counts,
tables, and feature lists quietly fall behind. This script compares what the code
actually exposes against what every document claims: CLI subcommands vs the command
table, the real regression-case count vs every "N cases" claim, mutation count,
the diagram's numbers, whether new commands reached all four docs, and whether the
English and Chinese versions still have the same section count.

```bash
python evals/doc_scan.py
```

It has caught, in one pass: five documents claiming 41 test cases when there were
50, a diagram advertising 21 commands when the table listed 20, and `fulltext`
missing from both usage guides.

## zip_check.py

Before sharing a packaged skill (uploading to ChatGPT, sending to a colleague):

```bash
python evals/zip_check.py lit-review.zip
```

Scans the archive for API keys, personal email addresses, machine-specific absolute
paths, and stray `.env` / `.git` / `__pycache__` entries. Manual review misses these;
a regex does not.

## Adding a case

1. Add the assertion to `test_regression.py` with a prefixed id and a one-line description of the defect.
2. Add the matching mutation to `mutation_check.py` (the smallest edit that reintroduces the bug).
3. Run both. The regression case must pass; the mutation must be caught.
4. If the case needs a file, generate it in `make_fixtures.py` — never hand-place binaries in `fixtures/`.

## Not covered here

Anything needing the network: rate-limit backoff, provider fallback under real 429s, live retraction lookups. Those belong in a manual live smoke test — see the limitations section of the README.
