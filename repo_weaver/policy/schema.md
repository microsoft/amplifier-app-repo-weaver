# repo-weaver corpus schema (concept-primary)

This wiki captures **durable knowledge about a software codebase** extracted from its
git history (commits and pull requests). It is a knowledge base, **NOT a changelog**.
Organize by **evergreen concept/entity**, never by source event. A PR or commit is an
*event that updates many concept pages* — it is never a page of its own.

## Core principle: concept-primary, accrete in place

- The durable pages are **concepts, modules/entities, and capabilities** — things that
  persist and evolve. When new history arrives, **UPDATE the existing concept page in
  place**: revise the current-state, and append a dated line to its History section
  with a citation. Do NOT mint a new page per PR/commit/window.
- A single PR typically touches **several** concept pages (the modules it changed, the
  capability it advanced). Fan its facts out into those pages. Its lasting trace is:
  (a) updated concept/module/capability pages, (b) one line in `log`, and (c) a
  `decision` page **only if** it records a genuine architectural decision.
- **Titles are the concept/idea**, e.g. "Recording ingestion", "Speaker attribution",
  "Frontend toolchain" — **never** "Change: PR #8".

## Page types (durable)

### module
A code module, directory, or subsystem.
Required sections: Purpose, Responsibilities, Key files, History.

### concept
A domain idea, mechanism, or cross-cutting behaviour (e.g. "retry backoff",
"projection filter", "deterministic enforcement"). Required sections: What it is,
How it works, History.

### capability
A user-facing feature or capability that evolves across PRs (e.g. "/ask endpoint",
"M365 recording sync"). Required sections: What it does, Current state, History.

### decision
An architectural decision (ADR), proposed only when a PR/commit records a real
decision or trade-off. Required sections: Decision, Context, Consequences. Event-shaped
by nature — this is the ONE place rationale may read chronologically.

### overview
Top-level navigation. One sentence per concept/module/capability, linking to detail
pages. When the corpus spans multiple repos, entries MUST be grouped by repo (a
dedicated section per repo listing its pages). Kept minimal.

### index
Auto-managed list of all pages by type. When the corpus spans multiple repos, entries
MUST be grouped by repo (a dedicated section per repo listing its pages), so a reader
scanning the index immediately sees what exists per repo.

### log
A single, append-only chronological audit (`log`). One line per ingested PR:
`YYYY-MM-DD — PR #N (author) — https://github.com/<org>/<repo>/pull/N — one-phrase summary — [[pages touched]]`.
Include the full GitHub pull-request URL so any merge is one click away from its
forensic context (diff, review thread, CI results).
This is the ONLY page where change-history accretes linearly. It is a sidecar, not the
spine. Do not put knowledge here that belongs on a concept page.

## History sections (how evolution is shown)

Every module/concept/capability page has a **History** section that accretes dated,
cited entries as later windows are ingested:
`- 2026-06-17 — advanced by PR #63 (samueljklee): vitest 3→4 (source 4)`
The body above History always reflects the **current** state; History records how it
got there. This is what "weave updates over time" means — pages mutate, they don't
multiply.

## Frontmatter contract (all pages)

```yaml
title: <concept/entity name — NOT "PR #N">
type: module | concept | capability | decision | overview | index | log
repos: [<repo-qualifier>, ...]   # every repo whose cited sources contribute to this page; derive from the **Repository:** of each cited source
sources: [1, 4, 7]              # integer source ids that support this page
last_updated: YYYY-MM-DD
```

## REPO-ATTRIBUTION instrument

### Frontmatter: `repos:` field

`repos:` MUST list every repo that contributes a cited source to this page:

- Derive each entry from the `**Repository:**` line of each source cited on the page.
- **Single-repo corpus:** always a one-element list,
  e.g. `repos: [amplifier-bundle-repo-weaver]`.
- **Multi-repo corpus:** list EVERY repo qualifier whose sources are cited here.
- For `overview`, `index`, and `log` pages that span the full corpus, list every
  repo present in the corpus.
- It is the page-level, machine-checkable record of which repo(s) this page covers.

### Body rendering: repo visible near the top

Every module/concept/capability/decision page MUST name its repo(s) in the opening
line or a short "Repo(s):" note near the top (e.g. `Repo: amplifier-bundle-repo-weaver`).
A reader must not have to resolve citations to determine which repo a page describes.

### Cross-repo pages: per-claim attribution

When a page is genuinely cross-repo (covers a concept that exists in 2+ repos as one
shared thing), it MUST attribute each claim to its repo — extend the citation convention
so a reader can tell which repo a fact came from. Acceptable forms:
`(amplifier-core, source 7)`, `(amplifier-bundle-wiki-weaver, source 3)`, or a
per-repo subsection.

### Page-scoping policy

- **Default: repo-scoped pages.** A concept that exists in only one repo is that
  repo's page. Do NOT merge a same-named concept from a DIFFERENT repo into an
  existing page just because the slug matches. If two repos each have their own "CLI"
  or "synthesis pipeline", they are DIFFERENT subjects and MUST be SEPARATE pages.
  Disambiguate the title/slug by repo
  (e.g. `Synthesis pipeline (amplifier-bundle-repo-weaver)`).
- **Cross-repo page only for genuinely shared concepts.** Create a single cross-repo
  page only when the same concept spans repos as one shared thing (e.g. a
  platform-wide capability several repos implement/extend) — and then attribute every
  claim per-repo (see above).
- **Index and overview group by repo.** See the `index` and `overview` type
  descriptions above for the required per-repo grouping.

## REPO-IDENTITY rules

These rules guard against a class of synthesis error: two repos with the same basename but
**different owners** silently merged into one entity, or a fictional rename/transfer narrated
from plausible-but-unsourced clues. See also Quality rule 9.

### RI-1 — SAME NAME ≠ SAME REPO

`owner/repo` is the canonical identity — **never** the bare repo name alone. Two repos that
share a basename but have **different owners** (e.g. `bkrabach/amplifier-bundle-skills` and
`microsoft/amplifier-bundle-skills`) are **DISTINCT repositories** and MUST be represented
as **SEPARATE subjects and SEPARATE pages**. Never merge them into one entity because their
names match. Apply the org-scoped qualifier everywhere — page titles, slugs, body text,
frontmatter `repos:` — so every reference is unambiguous.

### RI-2 — No inferred lineage (rename, transfer, fork-merge, "formerly known as")

NEVER assert that two repos are the same repo, were renamed, were transferred, were
fork-merged, or are "formerly known as" each other UNLESS there is **explicit lineage
evidence in an ingested source** — e.g. an actual commit message, PR body, or redirect
notice that **states** the rename or transfer. The following are **NOT** sufficient evidence:

- A shared basename
- Similar or identical README text
- Overlapping early commit history
- Plausibility ("it looks like a fork of…")

If you cannot cite the lineage claim to a **specific ingested source id**, do not make it.

### RI-3 — Fail loud on ambiguity

When two same-named-different-owner repos appear in the corpus and you cannot confirm their
relationship from the sources, state the ambiguity plainly rather than narrating a clean
rename or silently merging them. Required form (adapt as needed):

> "Two repos share this name: `owner-a/foo` and `owner-b/foo`. They are tracked as
> distinct repos; the ingested sources do not establish that they are the same repo."

This mirrors the cite-or-omit discipline: do not fabricate a relationship to produce a
tidy story.

### RI-4 — Identity consistency across all pages

A repo's identity MUST be represented **consistently** across all pages in the corpus.
Do not describe two repos as "the same repository" on one page while treating them as
distinct on another. Contradiction between pages on repo identity is a synthesis defect.

## Linking convention

- `[[ConceptName]]` / `[[ModuleName]]` / `[[CapabilityName]]` to cross-reference.
- Source citations inline: "(source 3)", "(sources 2, 5)".
- Prefer an associative web of links over hierarchy.

## Quality rules

1. **Cite or omit.** Every non-trivial claim cites a source id. If a fact has no
   source, omit it — never fabricate provenance. Never describe behaviour not supported
   by an ingested source.
2. Prefer exact quotes or file paths over paraphrase when available.
3. When sources conflict, keep both claims with their respective citations.
4. A page must not assert facts from sources it does not cite.
5. **No standalone change/PR pages.** A PR is never a page. It updates concept pages +
   the `log` (+ a `decision` page if warranted). If you are about to title a page
   "Change: PR #N", STOP — find the concept/module/capability it advances and update
   that instead.
6. Attribute each PR to the single author in that PR's `Author (PR opener)` field — the
   GitHub login of the opener. Never infer authorship from window-level contributor
   lists, co-authors, reviewers, or bots.
7. Cross-repo relationships: when a page covers a change or module that references,
   depends on, promotes, or is referenced by another repo also present in this corpus,
   the page MUST state that relationship (naming both repos and direction) and add a
   `[[wikilink]]` to the related repo's page so both sides are navigable.
8. **Consult `log` for dates, authors, and PR numbers.** When a question asks for a
   specific merge date, author, or PR number that is not present in the relevant
   concept/module/capability page, CONSULT the `log` page — it is the authoritative
   chronological index. Never answer "unknown" for a date or PR number that may be
   recorded in `log`. The `log` line format includes the full GitHub URL so every
   merge event is traceable.
9. **REPO-IDENTITY** (see the REPO-IDENTITY rules section for full detail). `owner/repo`
   is the canonical identity — never the bare repo name alone. Two repos sharing a basename
   but different owners are **DISTINCT** and must be **SEPARATE pages**; never merge them.
   Never infer a rename, transfer, fork-merge, or "formerly known as" relationship without
   explicit cited evidence in an ingested source. When same-named-different-owner repos
   appear and their relationship cannot be sourced, state the ambiguity plainly (see RI-3)
   rather than narrating a clean identity. A repo's identity must be consistent across
   all pages — contradiction between pages is a synthesis defect.
