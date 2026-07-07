# repo-weaver Architecture

> Audience: a developer new to repo-weaver. Read this first.

## 1. What repo-weaver is

repo-weaver is a **deterministic, git-aware front-end** that turns a collection of
git repositories (commits + merged PRs) into a queryable, cited knowledge wiki.

It does **not** do the wiki synthesis itself. It **shells out** to the external
`wiki-weaver` engine, which performs all the LLM work. **repo-weaver makes ZERO
direct LLM / model API calls** — it is deterministic plumbing plus an authored schema.

## 2. The boundary: wiki-weaver vs repo-weaver

| | wiki-weaver | repo-weaver |
|---|---|---|
| Role | Generic LLM wiki-synthesis **engine** (mechanism) | Git-aware **front-end** (policy / content) |
| Input | A folder of source docs + a `policy/schema.md` | Git repos (commits + merged PRs) |
| Does | Ingests, reconciles, cites, weaves concept pages, answers queries, builds HTML dashboards | Adapts git → source docs, orchestrates weaves, grades output, builds repo-flavoured dashboards |
| Knows about git? | No — domain-agnostic | Yes — this is its whole job |

The boundary is **two-layered**:

1. **LLM boundary (subprocess)** — repo-weaver makes zero direct LLM calls.  All
   synthesis, query, and dashboard work crosses via subprocess:
   - Every synthesis: `subprocess.run(["wiki-weaver", "ingest", ...])` — `weave.py:~533`
   - Every query: `subprocess.run(["wiki-weaver", "ask", ...])` — `cli.py:~284`
   - Every dashboard: `subprocess.run(["wiki-weaver", "build-dashboard", ..., "--group-by", "repos", "--group-link-template", "https://github.com/{group}"])` — `cli.py:cmd_build_dashboard`

2. **Path-constants import** — `wiki_weaver.lib` path helpers (`wiki_ledger`, `wiki_failed`,
   `wiki_sources`, `wiki_inbox`, `wiki_dashboard`) are imported directly so the two repos
   share a single source of truth for corpus layout.  This import is **non-LLM**: it only
   accesses pure-Python string constants and `pathlib.Path` arithmetic.  All heavy deps
   (model clients, attractor engine) are never touched at import time.

![Architecture](docs/architecture.png)

## 3. Layers / modules

repo-weaver adds ~3,100 LOC (package) + ~1,350 LOC (eval) on top of the engine.

### `repo_weaver/materialize.py` + `gitio.py` (~1,500 LOC) — the git → source-docs adapter

Per window, per repo it emits:

- A `<owner>__<repo>-<until>-changes.md` **change digest** — merged-PR sections, a
  **"Notable Commits"** section for commit-only repos, and a commit-volume summary.
- Optional `module-<owner>__<repo>-<slug>-<until>.md` **module snapshots**.

Org-scoped `owner__repo` filename qualifiers prevent same-basename collisions.
Never fabricates provenance — all data comes from git / gh plumbing.

### `repo_weaver/policy/schema.md` — the authored synthesis schema

repo-weaver's **content/policy** fed into wiki-weaver's externalized-schema **mechanism**:

- Concept-primary pages with `repos:` attribution frontmatter.
- Repo-identity rules: same-name ≠ same-repo; no inferred rename without cited lineage;
  fail loud on ambiguity.
- Per-repo index/overview grouping; append-only `log` page.

### `repo_weaver/weave.py` + `cli.py` (~1,600 LOC) — weave orchestration + dashboard

Multi-repo, windowed, incremental/staggered weave orchestration: raised digest cycle
budget, retry with backoff, strand-rescue when the engine crashes mid-run, archive-skip
dedup, fetch-or-warn staleness check.

`cli.py` also exposes `build-dashboard` — a thin subprocess shell that calls
`wiki-weaver build-dashboard` with two repo-flavoured policy injections:
`--group-by repos` (multi-membership grouping on the `repos:` list field) and
`--group-link-template 'https://github.com/{group}'` (repo group headers become
GitHub links). A packaged default theme (`repo_weaver/themes/default.json`,
GitHub-flavoured slate accent) is seeded idempotently into the corpus's
`.wiki/dashboard/theme.json` on first run.

### `repo_weaver/sync.py` (~380 LOC) — deterministic change-detection glue

`sync_corpus()` (wired to `repo-weaver sync`) needs **no manual repo list**: it
recovers each tracked repo's OWN last-sync date (and the tracked `(owner,
repo)` set) directly from the `owner__repo-YYYY-MM-DD-changes.md` filenames
already in `_sources/` -- **per repo**, not a single corpus-wide watermark, so
one repo's more recent digest can never mask another repo's older one and
hide its activity. An explicit `--since` still applies globally as an
intentional caller override. `sync` asks GitHub (`gh repo list`, via
`gitio.gh_list_repos`, which returns `(repos, error)` so a genuine `gh`
failure -- auth, rate-limit, network, unparsable output -- is distinguishable
from "gh succeeded, zero matching repos") which tracked repos pushed since
their own last-sync date, ensures a local clone under `--clones-dir` (`gh repo
clone` or `git fetch`, via `gitio.gh_clone_repo`), and re-weaves each changed
repo through the existing single-repo `weave()` path over its own window.
`gh` CLI calls (`gh_merged_prs`, `gh_list_repos`, `gh_clone_repo`) retry with
exponential back-off (1s/2s/4s, 3 attempts) via `gitio._run_gh_with_retry` to
harden unattended/scheduled runs against transient network blips. `sync
--json` emits the structured result (changed repos, per-owner counts, errors,
`discovery_failed`) for programmatic callers; a genuine discovery failure for
any owner makes the CLI exit non-zero even if other owners succeeded, since
the CHANGED list is incomplete for that owner (silent-stale trap otherwise).
It adds no new orchestration engine and no `.dot` pipeline — it is glue over
`weave.py` and `gitio.py`, matching repo-weaver's zero-direct-LLM-calls stance.

### `gitio.discover_repos()` + `repo-weaver discover` — discovery MECHANISM, not policy

`discover_repos(rules)` (`gitio.py`) and the `repo-weaver discover
--rules-file PATH [--json]` CLI wrapper let a caller find repos across
multiple owners/orgs via `gh`, WITHOUT repo-weaver owning or parsing a
discovery-config schema. Each rule is a plain dict (`owner`, `match` glob,
`include_forks`, `visibility`) supplied by the CALLER each invocation (e.g.
loaded from the caller's own JSON file) -- policy (which owners, which
match patterns, per-source fork/visibility rules) stays entirely with the
caller. `gh_list_repos()` gained `include_forks` / `visibility` parameters so
`discover_repos()` can apply different rules per owner (e.g. exclude forks
for personal accounts, include them for an org) in one pass. A failing rule
does not abort discovery of the others -- errors are collected and returned
alongside the matched, deduplicated repo list, mirroring `sync_corpus()`'s
"keep going, report failures" pattern.

### `eval/` (~1,350 LOC) — deterministic eval harness

- `grade.py` — ontology / weave metrics.
- `trace_grounding.py` — groundedness tracer (string/version/quote normalization, **no LLM**).
- `coverage_check.py` — coverage metrics.
- `run_questions.py` — shells out to `repo-weaver ask`.
- Question sets.

## 4. Data flow

```
git repos
  → gitio (git / gh)
  → materialize (source docs)
  → corpus/_inbox/*.md
  → wiki-weaver ingest   (loop-pipeline orchestrator runs synthesize.dot
                          using repo-weaver's policy/schema.md)
  → concept pages + _archive
  → wiki-weaver ask
  → cited answers

eval/ grades the corpus deterministically (out of band).
```

## 5. Why no LLM calls here

All synthesis and query LLM work is delegated to the wiki-weaver subprocess.
repo-weaver stays deterministic plumbing + an authored schema. Benefits:

- **Cheap & fast** — no model API cost on the repo-weaver side.
- **Unit-testable** — deterministic output is gradeable and diffable.
- **Free improvement** — the engine improves underneath repo-weaver for free.

## 6. Portability — the four leverage levels

wiki-weaver offers four leverage levels: standalone `.dot` files, a Python library,
Amplifier tool modules, and a thin CLI. **All four are now built and proven for
repo-weaver:**

| Level | Form | Status | Proof |
|---|---|---|---|
| **L1** | Standalone `.dot` files | ✅ | Proven end-to-end in the Amplifier Resolve **dot-graph resolver (DTU worker)**: `pipelines/repo-weaver-smoke.dot` + `repo-weaver-smoke.resolver.yaml` ran `start→doctor→weave→ask→done` to `completed`, producing a real wiki-weaver corpus + a real cited answer. Nodes **shell out to the repo-weaver CLI**; wiki-weaver stays behind its CLI/subprocess boundary (no engine fold — low coupling). |
| **L2** | Python library | ✅ | `repo_weaver/__init__.py` exports the clean public API: `init`, `weave`, `weave_multi`, `replay_windows`, `ask`, `materialize`. |
| **L3** | Amplifier tool modules | ✅ | `bundle.md` + `modules/tool-repo-weaver/` expose 3 agent-callable tools (`repo_weaver_init` / `repo_weaver_weave` / `repo_weaver_ask`); `mount()` is Iron-Law compliant; `execute()` proven to return a real `ToolResult` with a real cited answer. |
| **L4** | Thin CLI | ✅ | Ran across 30 repos this session. |

![Leverage levels](docs/leverage-levels.png)

The 0-LLM stance and the subprocess boundary hold at **every** level: even the L1
`.dot` nodes and the L3 tools are deterministic plumbing that shell out to the
repo-weaver CLI, which in turn shells out to the wiki-weaver engine. No level
imports or folds wiki-weaver's private engine.

### Roadmap — the four levels are complete

The portability work is **done**. The only remaining surfaces are deliberately
**parked** as future leverage levels — not built, because there is no consumer yet:

- **MCP / REST service wrappers** — PARKED. Add when a real consumer needs
  repo-weaver over a network boundary rather than a subprocess one.
- **Self-hosted web UIs** — PARKED. Add when an interactive product surface is
  concretely wanted (today the CLI `ask` + the L3 tools cover every known need).

Rule: add a level when a consumer for it is real, not speculatively. Building
either now would be a service/UI with zero callers — dead complexity.

## 7. The five fixes shipped (2026-06-23/24)

All on repo-weaver's side of the boundary — **the engine was untouched**.

1. **Commit-detail "Notable Commits"** — coverage for commit-only repos.
2. **Org-scoped `owner__repo` qualifier** — fixes same-name collision / overwrite.
3. **Incremental-weave qualifier** — fixes one-at-a-time weave collisions.
4. **Repo-identity rules** — same-name ≠ same-repo.
5. **Eval tracer version-normalize + trailer-strip** — groundedness tracing accuracy.


## 8. Corpus directory layout (co-tenancy)

A caller may want to point `--corpus` at a directory that also holds unrelated
content (e.g. a shared vault also used for other purposes). This section lists
EXACTLY what `weave` / `sync` / `build-dashboard` write directly under
`<corpus>/`, verified against the code (not guessed), so a caller can safely
co-locate other files without collision.

Written by repo-weaver / wiki-weaver, directly under `<corpus>/`:

| Path | Written by | Purpose |
|---|---|---|
| `_sources/` | wiki-weaver ingest (via repo-weaver's materialize step) | Archived, qualified change-digest + module-snapshot markdown files (`<owner>__<repo>-<until>-changes.md`, `module-<owner>__<repo>-<slug>-<until>.md`). `sync` and `weave`'s archive-skip / per-repo-watermark logic both read this directory's filenames as their only state. |
| `_inbox/` | repo-weaver `materialize()` (via `weave()` / `weave_multi()`) | Transient staging: source documents written here before `wiki-weaver ingest` consumes them. Empty between runs in the steady state; non-empty only mid-run or after a crash (see `.wiki/failed/` below). |
| `.repo-weaver.json` | repo-weaver `init()` | Corpus config: the list of registered local repo paths (`repos: [...]`, or legacy `repo: "..."` for pre-multi-repo corpora). |
| `.replay-progress.json` | repo-weaver `replay_windows()` | Resume-from-checkpoint state for `repo-weaver replay` (set of completed `(since, until)` window keys). Safe to delete: deletion is equivalent to `--restart`. |
| `policy/schema.md` | repo-weaver `init()` | The code-fit synthesis schema copied from the packaged `repo_weaver/policy/schema.md`, read by `wiki-weaver ingest`. |
| `.wiki/` | wiki-weaver (all subpaths below) | wiki-weaver's own hidden state directory -- repo-weaver never writes here directly except via the `wiki_weaver.lib` path helpers it imports for consistency. |
| `.wiki/.processed.jsonl` | wiki-weaver ingest | Append-only ledger of processed sources (read by repo-weaver's `_read_ledger_for_source()` to classify `.wiki/failed/` retries). |
| `.wiki/.sources.json` | wiki-weaver ingest | wiki-weaver's own source registry. |
| `.wiki/failed/` | wiki-weaver ingest | Sources that failed to converge; repo-weaver's `_retry_failed_sources()` drains this directory with classified retries (transient / not-converged / permanent). |
| `.wiki/runs/` | wiki-weaver ingest | wiki-weaver's own per-run bookkeeping. |
| `.wiki/policy/` | wiki-weaver (via `wiki_policy_dir`) | wiki-weaver's own policy storage (distinct from the top-level `policy/` written by repo-weaver's `init()`, above). |
| `.wiki/dashboard/theme.json` | repo-weaver `_ensure_corpus_theme()` (via `build-dashboard`) | Idempotently seeded default theme (title + accent colour); never overwritten once present. Overridable per-run with `build-dashboard --theme PATH`. |

**Not written under `--corpus` at all:**

- The dashboard HTML output -- always caller-specified via `build-dashboard
  --out PATH`, which may point anywhere (inside or outside the corpus dir).
- Local repo clones managed by `sync` -- written under `--clones-dir`
  (default `~/dev/amplifier-corpus-clones`), a SEPARATE directory from the
  corpus entirely.

**Practical guidance:** a caller who wants to co-locate unrelated content in
the same directory as `--corpus` should avoid creating files/directories at
the top level named `_sources`, `_inbox`, `.wiki`, `.repo-weaver.json`,
`.replay-progress.json`, or `policy/` -- everything else at the corpus root is
untouched by repo-weaver / wiki-weaver.
