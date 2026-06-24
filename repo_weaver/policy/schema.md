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
pages. Kept minimal.

### index
Auto-managed list of all pages by type.

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
sources: [1, 4, 7]          # integer source ids that support this page
last_updated: YYYY-MM-DD
```

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
