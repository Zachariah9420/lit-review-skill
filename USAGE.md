# lit-review Usage Guide

**English** | [繁體中文](USAGE.zh-TW.md)

> For first-time users: reading this guide top to bottom takes about ten minutes, from installation to reading your first report.

## What this is (30-second version)

A skill that turns an LLM agent (Claude, Codex, or others) into a rigorous literature assistant. It does three things:

1. **Find literature**: paste a draft paragraph — it extracts your claims, searches academic databases, and recommends real papers with abstract evidence attached
2. **Audit citations**: hand it a document with references — it verifies every one: does the paper exist? is the bibliography correct? does it actually support the sentence you attached it to?
3. **Grounded writing**: give it a topic — it retrieves first, then writes, producing an article where every cited claim is checkable

The core selling point is **honesty**: not-found is reported as not-found, unjudgeable is marked ❓, and every bibliographic field comes from an API rather than model memory. Its job is not to make your paper *look* well-cited — it's to make every citation survive a committee's follow-up question.

## Installation

**Claude Code** (desktop or CLI) — as a plugin. The first command registers the marketplace, the second installs from it; running the second alone fails:

```bash
claude plugin marketplace add Zachariah9420/claude-research-skills
```

```bash
claude plugin install lit-review@zachariah-research-skills
```

Or clone it as a skill instead:

```bash
git clone https://github.com/Zachariah9420/lit-review-skill ~/.claude/skills/lit-review
```

Works immediately, no restart. The helper script is pure Python 3.8+ standard library — nothing to pip-install.

**Codex / other agents**: clone anywhere, then add one line to your project's `AGENTS.md`:

> For literature search and citation auditing, read `<clone-path>/SKILL.md` and follow its workflow.

## Five-minute start: three examples

**Example 1 — find literature for a paragraph.** Just paste your draft:

> Here's a paragraph from my thesis: "University students' smartphone use may affect sleep quality and academic performance…" — find literature for this.

You get: 2–3 recommendations per claim (with the abstract evidence sentence, why it's relevant, and where to cite it) plus a `new_refs.ris` importable into EndNote/Zotero.

**Example 2 — audit a chapter's citations.** Hand over a file:

> Check the references in chapter2.docx for problems.

You get an audit report: an overview table (existence / bibliography / support for each entry), per-entry details (which field is wrong and what it should be, with database evidence), and a manual-check list.

**Example 3 — write with references:**

> Write a detailed explanation of the Transformer model, with references.

It retrieves first, writes second, and self-audits before delivery. The article ships with a claim-evidence table — every cited sentence lists its abstract evidence, and expository passages are explicitly marked uncited.

## Command reference

Commands are optional — the agent infers the mode from your input. For precise control, use `/lit-review <cmd>` in Claude Code or the same words in plain chat with Codex:

| Command | Does |
|---|---|
| `check <file>` | Audit citations (adds find-mode automatically when a reference list exists) |
| `find <paragraph>` | Find supporting literature |
| `write <topic>` | Literature-grounded writing |
| `verify <one citation>` | Quick single-citation existence + bibliography check |
| `map <topic>` | Field map of the **literature**: seminal papers / key authors / recent directions (for a map of the **techniques**, see research-gap-hunter's `landscape`) |
| `gap <X and Y>` | Research-gap detection for an intersection **you name** (with honesty disclaimers); does not generate topic candidates |
| `matrix <list or topic>` | Literature matrix (method/sample/findings/limitations table) |
| `notes <DOI>` | Single-paper reading note card |
| `integrity <file>` | In-text vs. reference-list three-way check (instant, zero cost) |
| `glossary <file>` | Terminology-consistency check (EN term ↔ translation) |
| `rehearse <file>` | Committee/reviewer question rehearsal |
| `watch <DOI list>` | New-literature watch (schedulable in Claude Code) |
| `annotate <file>` | Mark which sentences need citations |
| `counter <claim>` | Deliberately search for null/contrary evidence |
| `strength <paper+claim>` | Evidence-strength grading (HIGH/MEDIUM/LOW/UNKNOWN) |
| `claims <file>` | Claim–evidence ledger: support/oppose counts and strength per claim |
| `retract <DOI>` | Retraction/correction lookup (already part of every `check`) |
| `fulltext <DOI>` | Locate legally available full text; names the institutional route when there is none |
| `versions <arXiv or DOI>` | Preprint ↔ published-version resolver, with a switch recommendation |
| `export-xml <selected>` | EndNote XML export — audit verdicts travel into the Research Notes field |

**Modifiers** (append to a command): `deep` escalate to open-access full text when abstracts can't settle a verdict / `quick` cheapest mode / `thorough` per-claim adversarial verification / `bibtex` want BibTeX / `no-ris` skip the reference file. Example: `check deep chapter2.docx`.

## Optional API keys (recommended)

Works without keys, but the Semantic Scholar free pool jams at peak times. When it does, `search` and `snowball` (for DOI inputs) auto-fall-back to OpenAlex — results stay trustworthy, ranking gets slightly noisier. Citation verification (`verify` / `verify-batch`, and therefore `check`) queries only Crossref and Semantic Scholar and has **no** OpenAlex fallback: if one of the two fails, the verdict is flagged as single-source (`single_source_degraded`); if a source failed and nothing plausible was found, it is `partial_failure` (lookup incomplete, not "not found"). Create `.env` in your **project directory**, or put a copy in `~/.env` as a global fallback that works from any working directory (the script reads the current directory's `.env` first, then `~/.env`):

```
# free at semanticscholar.org/product/api, issued in a few business days
S2_API_KEY=your-key
# joins the Crossref/OpenAlex polite pools; also required by fulltext (Unpaywall needs a contact email; without it only Semantic Scholar's OA field is used)
CROSSREF_MAILTO=you@example.com
# optional: English script messages (default zh); --lang en does the same for a single run
LIT_LANG=en
```

**Never commit `.env`** (this repo's .gitignore already blocks it). `LIT_LANG` is not a key — it only picks the language of the script messages; see "Can I get English output?" in the FAQ.

## Cost control (token spend)

The expensive part is LLM judging, not retrieval (API calls and the deterministic integrity script cost zero tokens). Three verification tiers:

| Tier | Cost | For |
|---|---|---|
| `quick` | 1× | Everyday drafts. Skips the independent audit; report is labeled "no independent audit" |
| default | ~1.5–2× | Normal deliveries. Main agent + one independent skeptic |
| `thorough` | 5–10× | Final pre-defense / pre-submission checks, per-claim adversarial verification |

Principle: **verification intensity follows the cost of being wrong.** Internally agents also use the brief/pick two-stage funnel (browse one-line listings, then read only shortlisted abstracts), cutting the biggest cost line by half or more.

## Reading the report

**Support symbols:**

- ✅ Supported: the abstract (or full text) clearly covers the claim
- ⚠️ Partially supported: related, but your wording is half a grade stronger than the evidence (most common — e.g. the paper says "associated", you wrote "causes"). The report tells you *which half of the sentence* each paper can carry
- ❌ Likely unsupported: topic mismatch — citing this will get caught
- ❓ Cannot judge: no abstract available (paywall, publisher withholding). **This is not a red flag about the paper** — it's the honest limit of automated checking; a link is attached for manual review
- 🚫 Not found: no plausible match in the sources actually queried. The report names them (`verify` queries Crossref + Semantic Scholar) and says "X + Y both found nothing" — not a vague "all databases", and never "fabricated": book chapters and very recent papers legitimately escape indexing

**Evidence levels** (matrix and deep-check reports): `[abstract]`, `[full text p.X]`, `[bibliographic]`, `[synthesis]`, `[❓]` — so you know how reliable each verdict is.

**Quality red flags** (`quality_warnings`): zero citations (age-gated so new papers aren't punished), venue not in DOAJ/CORE, etc. A flag is not a veto — it means "supporting evidence only, and the report must disclose it".

## FAQ

**Q: It's slow?**
Without a key, Semantic Scholar rate-limits and the script backs off and retries (normal). A free key fixes it; meanwhile `search` and `snowball` auto-fall-back to OpenAlex (citation verification has no such fallback).

**Q: Chinese-language references?**
The academic APIs barely index them. With a Google Scholar tool available, the skill searches by the original title (marked "medium confidence"); otherwise the entry goes to the manual-check list. It will never translate a title into English and force-match — that fabricates pairings.

**Q: Can I get English output?**
From the scripts, yes. Set `LIT_LANG=en` in `.env` or in your environment, or add `--lang en` (or `--lang=en`, before or after the sub-command) to a single run. Precedence: `--lang` > environment variable `LIT_LANG` > `.env` (current directory first, then `~/.env`) > default `zh`. An empty `LIT_LANG` counts as unset (`LIT_LANG` only: any other key already set in the environment, even to an empty string, is not replaced from `.env`); in `.env`, a space and `#` after an unquoted value start an inline comment, which is ignored (`LIT_LANG=en   # note` reads as `en`; a value in matching quotes keeps its `#`); case and a region suffix are ignored (`EN`, `en-US` work); any other value falls back to Chinese and prints one warning on stderr. Both `lit_api.py` and `cite_integrity.py` honour it.

What changes is the explanatory text: `message`, `note` / `_note`, `recommendation`, `confidence_note`, `status`, `hint`, `absence_note`, `single_source_degraded`, `quality_warnings`, `downgrade_reasons`, `derivative_item_risk`, `journal_doi_status`, `s2_note`, `source_discrepancy`, `coverage_caveat`, `how_to_get` and `unsupported_style_note`, the `-h` help, and the `integrity` text report; also three values that look like data — `citations_source`, `label` and `published_version.source` (see `references/api-notes.md`), which a program should not match on. What stays the same in both languages: JSON field names, `verdict_hint` values, error codes, `basis` / `version_link` values, the status emoji (✅❌❓⚠️🚨🔒) and exit codes.

Three limits. `SKILL.md` and the report templates are Chinese, so the report the agent writes for you will usually still be Chinese unless you ask for English — the switch is working, the instructions are simply Chinese. Error text from an upstream service or the operating system (`*_error` / `error` fields) is passed through verbatim and can be in your OS language. And `quality_warnings` keep the language they were written in: a JSON or EndNote file produced under Chinese still shows Chinese flags when read under English.

**Q: A book can't be found?**
APIs index pre-2010 books poorly and often only hit the book's *reviews* — which ironically proves the book exists. The report marks it as indirect evidence and tells you to check the ISBN.

**Q: It marked my citation "partially supported" — should I delete it?**
Usually not. Partial support is usually a wording problem: change "significantly outperforms" to "can improve", "causes" to "is associated with", and the citation stands. The report suggests the exact wording.

**Q: The year differs from mine by one?**
Online-first vs. print-issue years differ by one all the time; the report treats it as harmless. Two or more years off is a real bibliographic error. Same for arXiv preprint year vs. conference year — the report states which one is authoritative.

**Q: I cited the arXiv version and it tells me to change?**
If a formally published version exists (journal/proceedings), academic convention prefers it; the report attaches the published DOI so you can swap directly (`versions` does this on demand).

## Honest limitations (read before relying on it)

1. Default judgments are abstract-based; "the abstract doesn't say it" ≠ "the paper doesn't contain it". Escalate key citations with `deep` (open-access full text); paywalled papers stay ❓
2. Verdicts are made by an LLM. The isolation discipline (evidence/role/injection firewalls) lowers the error rate but doesn't zero it — **before a defense or submission, walk through the ❓ and ⚠️ items yourself**
3. The integrity check supports numeric citation styles ([1], [2]) only; author-year (APA) needs manual checking, and it says so
4. Citation counts are day-of-check snapshots
5. This tool makes bad citations harder to produce, but **the selection and interpretation of literature remains the author's responsibility** — it's a guardrail, not a chauffeur
