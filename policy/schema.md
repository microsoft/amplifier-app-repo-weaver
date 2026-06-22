# repo-weaver corpus schema

This wiki captures knowledge about a software codebase extracted from its git
history (commits and pull requests). Keep pages small and factual. Every claim
must cite a source id. Cross-link related pages with [[wikilinks]].

## Page types

### module
A code module, directory, or subsystem.
Required sections: Purpose, Responsibilities, Key files, History.
**Temporal rule:** when a module's behaviour or interface changes across ingested
sources, ADD the new state with its citation and note the evolution in the History
section — do NOT silently overwrite prior dated claims. The page should read as
the module's history, not just its current snapshot.

### change
A notable decision, feature, fix, or PR and its rationale.
Required sections: Summary, What changed, Why, Affected modules.
Use `[[wikilinks]]` to the relevant module pages.

### overview
Top-level navigation page. One sentence per module or change area, linking to
the detail pages. Kept minimal.

### index
Auto-managed list of all pages by type.

## Frontmatter contract (all pages)

```yaml
title: <human-readable title>
type: module | change | overview | index
sources: [1, 4, 7]          # list of integer source ids that support this page
last_updated: YYYY-MM-DD
```

## Linking convention

- `[[ModuleName]]` to cross-reference a module page.
- `[[Change: PR #42]]` to reference a change page.
- Source citations inline: "(source 3)", "(sources 2, 5)".

## Quality rules

1. No fabricated provenance. If a fact has no source, omit it.
2. Prefer exact quotes or file paths over paraphrase when available.
3. When sources conflict, keep both claims with their respective citations.
4. A module page with only source-1 facts should not assert things from source-2.
