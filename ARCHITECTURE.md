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

repo-weaver **never imports** wiki-weaver (`dependencies = []`). The boundary is a
process boundary, crossed only via subprocess:

- Every synthesis is `subprocess.run(["wiki-weaver", "ingest", ...])` — `weave.py:~533`
- Every query is `subprocess.run(["wiki-weaver", "ask", ...])` — `cli.py:~284`
- Every dashboard build is `subprocess.run(["wiki-weaver", "build-dashboard", ..., "--group-by", "repos", "--group-link-template", "https://github.com/{group}"])` — `cli.py:cmd_build_dashboard`

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
`.wiki-dashboard/theme.json` on first run.

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
