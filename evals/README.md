# Evals

Two scripts, no network, seconds to run. Run both before shipping any change to `scripts/`.

```bash
python evals/test_regression.py     # 133 frozen cases
python evals/mutation_check.py      # 76 mutations; each must be caught
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
| `XF-*` | fresh-session cross-domain testing: journal book reviews, letters and editorials that copy the original title and get "corrected" into the user's citation (XF-01–03); two earlier fixes that were too narrow, each leaving a sibling unfixed (XF-04/05) |
| `MED-*` | fresh-session medical-literature testing: candidates with an empty author field slipped past the author gate (MED-01–04); a single surviving source was not flagged as weaker evidence (MED-05/06) |
| `PH-*` | fresh-session pharmacy testing: Vancouver-style numbered reference lists parsed as zero entries (PH-01–03); pre-2010 retractions with no Crossref `updates` link (PH-04–06) |
| `NS-*` | fresh-session testing: the same paper from Crossref and S2 misread as an ambiguous match (NS-01–03); HTML entities such as `&amp;` leaking from Crossref into exports (NS-04) |
| `PERF-*` | see case docstrings (covers rate-limiter behaviour under concurrency, and `verify` / `verify-batch` sharing one decision path) |
| `FT-*` | see case docstrings (covers the `fulltext` boundary: no paywall circumvention, an institutional route when no free copy exists, source disagreements stated) |
| `PUNCT-*` | see case docstrings (covers full-width punctuation in the Chinese output strings of `lit_api.py` and `cite_integrity.py`) |
| `NET-*` | the suite's own no-network gate (`NET-01`); added after MED-05/06 turned out to be really connecting to Semantic Scholar |
| `I18N-*` | the English output mode (`LIT_LANG=en`): both `MESSAGES` tables complete and consistent (same keys, placeholders, identifier tokens and emoji in zh and en; en ASCII-only apart from the emoji), the call-site contract, the Chinese output unchanged (`I18N-03a` checks every pre-change Chinese string against the inventory `fixtures/zh_literals.json`, and every key's zh text against the per-key golden `fixtures/zh_messages.json`; both change only on purpose, in the same commit as the zh text they record (the inventory only when a zh string is changed on purpose, as with the two citation-age flags in `quality_warnings`); the exceptions are the two citation-age flags in `quality_warnings` (reworded on purpose: 0被引(發表年份早於今年) / 低被引(≤3)且發表年份距今3年以上) and `-h` / usage, which gains the `--lang` line and whose columns argparse re-aligns), no CJK in English output, `LIT_LANG` / `--lang` / `.env` resolution, `-h` in English, and the packaged ZIP running in English (and `package_skill.py` refusing to leave a ZIP that lacks a required file) |

The suite pins `LIT_LANG=zh` for the whole run (at the start of `main()`, inherited by the CLI subprocesses), so a `LIT_LANG=en` in your shell, your cwd's `.env` or `~/.env` cannot turn the existing cases red; the `I18N-*` cases switch to `en` themselves where they need it.

It calls the production functions (`rank_candidates`, `decide_verdict`, `norm_title`, …) with frozen candidate data, plus the CLIs against fixture files. No API keys, no network, no flakiness.

"No network" is enforced by the suite itself: it blocks socket connections in-process and fails case `NET-01` if anything tries to connect (CLI subprocesses started by `run_cli` are outside that block). `-k` now only selects which suites run; it no longer hides failing results.

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

It has caught, in one pass: five documents claiming 50 test cases when there were 60, a diagram advertising 21 commands when the table listed 20, and `fulltext`
missing from both usage guides.

## zip_check.py

Before sharing a packaged skill (uploading to ChatGPT, sending to a colleague):

```bash
python evals/zip_check.py lit-review.zip
```

Scans the archive for API keys, personal email addresses, machine-specific absolute
paths, and stray `.env` / `.git` / `__pycache__` entries. Manual review misses these;
a regex does not. It also fails when a file the scripts or the bundled tests need is
missing (`缺必要檔`; the same list as `REQUIRED` in `scripts/package_skill.py`, which
already refuses to leave such a ZIP behind) — that covers archives zipped by hand.

## A flaky test is a broken test

PERF-01 once failed while six agents were loading the machine, then passed four
times in a row. The cause was in the test, not the code: it compared the
*relative* wait each thread received, but each thread reads the clock
separately, so scheduling drift under load distorted the comparison. It now
checks two things that scheduling cannot affect — sequential reservations are
exactly one interval apart (pure arithmetic), and after N threads the reserved
slot has advanced by at least (N-1) intervals.

A regression suite that goes red at random teaches people to ignore red. If a
case fails once and passes on retry, treat the test as the defect until proven
otherwise.

## Adding a case

1. Add the assertion to `test_regression.py` with a prefixed id and a one-line description of the defect.
2. Add the matching mutation to `mutation_check.py` (the smallest edit that reintroduces the bug).
3. Run both. The regression case must pass; the mutation must be caught.
4. If the case needs a file, generate it in `make_fixtures.py` — never hand-place binaries in `fixtures/`.
5. If the change adds a user-visible string to `lit_api.py` or `cite_integrity.py`, it goes into that script's `MESSAGES` table with both a `zh` and an `en` text (same `{placeholders}`, identifier tokens and leading emoji; en ASCII-only apart from the emoji) — `I18N-01` / `I18N-02` fail on a missing language or a Chinese literal outside the table. `fixtures/zh_literals.json` is the inventory of the Chinese strings as they were before the English mode; it is updated only when a zh string is changed on purpose (in the same commit as that change, as with the two citation-age flags), never to make a case pass. A new key also needs its zh text in the per-key golden `fixtures/zh_messages.json` (`{script: {key: zh}}`): `I18N-03a` compares the table and the golden in both directions, so adding a key without updating the golden fails loudly. That is intended — the Chinese output never changes without someone writing the change into the golden in the same commit; changing an existing zh text likewise means changing its golden entry on purpose, not to make the case pass.

## Not covered here

Anything needing the network: rate-limit backoff, provider fallback under real 429s, live retraction lookups. Those belong in a manual live smoke test — see the limitations section of the README.
