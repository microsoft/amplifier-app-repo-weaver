# repo-weaver — design draft (v0)

A sibling to `wiki-weaver` that builds a queryable knowledge corpus from a
collection of git repos (contents + commits + PRs), usable as a **personal** or
**team** view of what you have across your repos and how they evolve.

Status: **BUILT + PROVEN** (was: draft for council review). Author: bkrabach (+ Amplifier).
The original draft and dated build-result logs below are preserved as history; the
final as-built reconciliation is the last section (2026-06-24).

---

## The call (ROB lens)

This is **not a big build**, and we should refuse to let it become one. The two
investigations proved the pattern is real and the integration seam is trivially
clean:

- `wiki-weaver` is the **engine** (a tool-module bundle): drop UTF-8 markdown
  into `_inbox/`, run ingest, get a cross-linked cited wiki out. The inbox is
  source-agnostic — "a pre-processor that writes markdown into `_inbox/`
  requires zero pipeline changes."
- `team-pulse` is the **app** that wraps the engine for *conversations*. Its
  whole synthesis call collapses to one contract: `run_synthesis(sources_dir,
  wiki_dir)` — any `.md` in, grounded wiki out. There is a `Synthesizer`
  protocol you can inject.

So the entire novel surface of "repo-weaver" is **one brick**: a thing that
turns repos → markdown source docs. Everything downstream (synthesis,
reconciliation, validation, citation, query) we **reuse verbatim**. That is the
plumbing. We build that pipe end-to-end on ONE repo before polishing anything.

## What we are NOT building (parked, named, with reasons)

- ❌ A web app / lens API / frontend. That's team-pulse's job; parked until the
  corpus itself is proven useful from the CLI `ask`. (Critical-path discipline.)
- ❌ Durable jobs, OneDrive-style sync, token refresh. No external auth surface
  here — `gh`/`git` are already on the box. Park the job machinery until
  multi-repo scale actually hurts.
- ❌ Team redaction/projection (private→team-safe) as a *first* step. Borrow
  team-pulse's projection later; the personal corpus needs no redaction.
- ❌ Incremental re-sync / "evolution over time" as live diffs. Real and
  wanted — but it's a v2 capability on top of a working v1 corpus.

## The one decision that actually matters (for the council)

**Granularity / decomposition: what is a "source document" for a repo?**

This is where corpus quality lives. Candidates (not mutually exclusive):

| Source doc | Captures | Risk |
|---|---|---|
| One doc per **commit** | fine-grained evolution, rationale | volume explosion, noise |
| One doc per **PR** (title+body+diffstat+review) | decisions, intent | misses non-PR history |
| One doc per **module/dir** (current snapshot + README) | structure, "what exists" | static, loses time axis |
| Rolled-up **changelog windows** (e.g. per-week per-repo) | trend, "how it's evolving" | summarization lossy |

ROB read: **PRs + module snapshots first** — they carry the most
signal-per-token (intent + current shape). Commit-level and time-window rollups
are the "evolution over time" v2. But this is exactly the call I want the
council to pressure-test, because over-specifying it locks the corpus shape.

## Thinnest provable slice (the plumbing, end-to-end)

1. Pick **one** repo I own.
2. Materializer emits markdown into `_inbox/`: one `pr-<n>.md` per PR
   (title, body, author, merged-at, files-changed, review summary) + one
   `module-<path>.md` per top-level dir (README + file inventory + purpose).
   Provenance = real commit SHA / PR number / author / date (never fabricated).
3. Run `wiki-weaver` ingest unchanged, with a code-fit `policy/schema.md`
   (page types: `module`, `decision`, `contributor`, `pattern`,
   `dependency`) + a code-fit convergence rubric.
4. `wiki-weaver ask` the corpus. Prove it answers real questions —
   "what does module X do", "why did we change Y", "what's evolving in repo Z" —
   with citations that trace to real commits/PRs.

If the answers are real and grounded, the pipe works. THEN we widen to N repos
and add the time axis. If they're hollow, that's the finding — fix the
materializer/schema, not the engine.

## Personal vs team (cheap, built-in)

It's just **which repos** and **where the corpus lives** — not two codebases:

- **Personal**: the repos *you* have access to (public + private via `gh`),
  corpus on your disk. No redaction.
- **Team**: a shared repo set, corpus committed to a shared vault; add
  team-pulse's projection pass only if private→team-safe redaction is needed.

Same materializer, same engine, same schema. Scope is a config, not a fork.

## Shape of the bundle

`amplifier-app-repo-weaver` (sibling to wiki-weaver):

- A **tool module** providing: `repo_weaver_materialize` (repos → `_inbox/` md)
  and thin pass-throughs to wiki-weaver's `ingest`/`ask`, OR simply *depend on*
  wiki-weaver and ship only the materializer + policy + awareness context.
- `policy/schema.md` + `policy/convergence-rubric.md` tuned for code knowledge.
- Provenance extractor for git (SHA/author/PR#/date) replacing wiki-weaver's
  document-frontmatter extractor.

Open: bundle-that-depends-on-wiki-weaver vs. app-like-team-pulse. ROB leans
"thin bundle + reuse," but the council should weigh it.

## Questions for the council

1. Is PR + module-snapshot the right *first* decomposition, or are we already
   over-thinking the source shape?
2. Thin bundle (depend on wiki-weaver) vs. app (own the job/product surface) —
   which is the honest v1?
3. Where's the hidden cost? (COE) Re-ingest churn, private-repo data landing in
   a corpus, multi-repo identity/attribution collisions?
4. Is "evolution over time" a property of the *corpus* or of *queries against
   it*? (i.e. do we need temporal pages, or just dated provenance + ask?)
5. What's the one thing that makes this NOT worth building? (COSam)

---

## v0 BUILD RESULTS (2026-06-22) — PROVEN END-TO-END

Built `repo-weaver` (own git repo at `./repo-weaver/`, installed via `uv tool`).
Thin bundle path chosen: it shells out to the installed `wiki-weaver` CLI (the
engine), mirroring its shape (`init` / `weave` / `ask` / `replay` / `doctor`),
usable unattended (exit codes, `--dry-run`, `--json`) and interactively.

Demo subject: **amplifier-app-team-pulse** (the repo only goes back to 2026-06-05,
so a literal "month ago" predates it — used the repo's early state as window 1
instead and stepped forward).

**What's real (verified by me, not asserted):**
- **W1** (`≤2026-06-16`): materialized 3 source docs (1 change-digest + 2 module
  snapshots) → `wiki-weaver ingest` → **all 3 converged**, ~10 cited pages built.
  Real grounded `ask`: described PR #72 (m365 projection, author bkrabach, 34
  files, merged 2026-06-16) — traces to the actual PR.
- **W2** (`2026-06-17..06-22`): re-ran → the new change-digest **wove into the
  existing pages over time**: `module-frontend.md` went `sources:[1,2]→[1,2,4]`
  and gained dated sections; new change pages appeared (lens collections #128,
  the improve-pr-review cluster #112/#114/#122/#123/#125/#126/#127). A
  cross-window `ask` answered the full June timeline with real PR#/author/date
  citations. **This is the "running it over time weaves updates in" proof.**

**One real bug found (fail-loud worthy — tester-breaker called this exactly):**
The change-digest carries evolution perfectly (window-unique filename →
re-ingested each window). But **module snapshots reuse the same filename every
window** (`module-frontend.md`). On W2, wiki-weaver assigned them new source IDs
(5,6) for the new content, then **silently SKIPPED them** as "already ingested as
source id [5/6]" — `ingested=False`, never woven, no loud failure. So the W2
module-snapshot deltas were dropped (the digest still covered the same ground, so
the corpus stayed correct — but that's luck, not design).
- **Fix (repo-weaver):** emit window-unique module filenames
  (`module-frontend-2026-06-22.md`) so each window's snapshot is its own dated
  source — OR make module snapshots a one-time baseline and let the change-digest
  carry all evolution. Leaning the latter (simpler, the digest already works).
- **Fix (wiki-weaver, upstream):** a new-hash source that collides on filename
  with an archived one should NOT silently skip — it should fail loud or
  disambiguate. Same silent-skip class ROB bans.

**Also fixed along the way (upstream wiki-weaver bug, blocking):**
`pipeline/normalize_links.py` did `from pipeline.validate_wiki import ...`, which
breaks in the installed wheel (the dir is force-included as `wiki_weaver_pipeline`)
— every ingest failed at the `normalize` stage with `ModuleNotFoundError: No
module named 'pipeline'`. Patched to resolve under both names. Worth a PR back to
`microsoft/amplifier-bundle-wiki-weaver`.

---

## Post-readiness work (2026-06-23) — published + S1/S2/A-B

Published: **microsoft/amplifier-app-repo-weaver (PRIVATE)**. Commits e12a1bf (prod-readiness), 7718435 (README+eval-repro), fdabf3c (resume + --no-classify), b740f3f (multi_repo.yaml).

**S1 — resume-from-checkpoint (fdabf3c):** `replay` records completed windows in `<corpus>/.replay-progress.json`; re-run skips completed, resumes at first incomplete; `--restart` forces full; `_failed/` sources re-attempted on resume; `weave_multi` skips already-archived repos. Marks complete ONLY on rc=0 (fail-loud). 32 tests pass.

**S2 — formal multi-repo eval (JUDGED):** 6 cross-repo questions vs the 10-repo curated TEAM corpus. **4 PASS / 2 PARTIAL / 0 FAIL; groundedness 6/6; ZERO cross-repo bleed; zero fabrication; fabrication-probe PASS** (correctly reported dot-graph + core had 0 in-window activity, invented nothing). The 2 PARTIALs are retrieval-depth completeness gaps (missed a companion PR page), not correctness/attribution errors. **Multi-repo claim formally proven.**

**A/B — classifier dissent (cranky-old-sam) RESOLVED with data:** same repo/window, classify-ON vs `--no-classify`, asked the frontend-toolchain question on each synthesized corpus.
- classify-ON: complete + correct — all 3 toolchain PRs (#63 Vite7→8/plugin-react4→6/vitest3→4, #61 test migration, #65 CI), authors, version deltas, intent. Routine PRs collapsed.
- classify-OFF: WRONG for the question — led with PR #65 + a list of dependabot bumps, **omitted the core #63 upgrade and #61 entirely** (pulled into the noise). This is the original FAIL behavior.
→ The classifier **earns its keep**; without it the synthesizer buries the substantive cluster under routine-PR volume. Keep classification (default on); `--no-classify` retained as an escape hatch.

---

## #1 SHIPPED + RE-PROVEN (2026-06-23) — concept-primary is now the default

Commit `944e1a4` (pushed to microsoft/amplifier-app-repo-weaver), 64 tests:
- **Concept-primary schema is now repo-weaver's DEFAULT** (`policy/schema.md`): no standalone PR pages;
  module/concept/capability/decision are durable types; History sections accrete in place; chronology
  quarantined to one append-only `log`; titles are concepts. Cite-or-omit, author-attribution, cross-repo
  rules preserved.
- **Guardrails:** (a) grounding tracer normalizes faithful reformatting (`v8.0.16` == `8.0.16`, smart quotes,
  case, whitespace) — never merges distinct numbers; (b) `log` lines carry PR#+URL and a rule to consult
  `log` for dates/authors; digest now emits each PR's GitHub URL; (c) `--no-fetch` + fetch-or-warn before
  materialize so a stale clone can't silently yield an empty window.

**RE-PROVE (fresh build, SHIPPED default schema, fresh clone, 3 windows on team-pulse):**
- Ontology: 19 pages, **16 concept / 0 change / 0% source-titled** — evergreen, as designed.
- Weave: W1→W2 ratio **0.82** (+2 new, 9 mutated), W2→W3 ratio **1.0** (+0 new, 6 mutated) — weaves in place.
- Groundedness (normalized tracer): **questions.yaml = 0 ungrounded** (the v-normalize fix cleared the prior
  `v8.0.16` false-flags). held_back = 2 flagged, both inspected = a page-name (`index.md`, the "Pages used"
  trailer) + a generic notation placeholder (`--since=<date>`); **zero real fabrication** — the forensic PR
  #126 answer is fully correct and sourced.

Verdict: shipped and proven on substance. Parked polish (good idea, not now): exclude the answer's
"Pages used:" trailer from grounding analysis so the metric reflects only factual claims.

---

## AS-BUILT RECONCILIATION (2026-06-24) — all four leverage levels BUILT + PROVEN

This section reconciles the draft above with what actually shipped. The early
draft framed repo-weaver as "one brick" (repos → markdown) reusing wiki-weaver
verbatim. That held — and it has now been carried across **all four leverage
levels**, each built and proven this session.

### The four leverage levels (reality, not intention)

| Level | Form | Status | Proof |
|---|---|---|---|
| **L1** | Standalone `.dot` pipeline | ✅ DONE + PROVEN | `pipelines/repo-weaver-smoke.dot` + `repo-weaver-smoke.resolver.yaml` ran **end-to-end through the Amplifier Resolve dot-graph resolver in a DTU worker** — instance reached `completed`, path `start→doctor→weave→ask→done`, producing a real wiki-weaver corpus + a real cited answer. |
| **L2** | Python library | ✅ DONE | `repo_weaver/__init__.py` exports the clean public API: `init`, `weave`, `weave_multi`, `replay_windows`, `ask`, `materialize`. (Supersedes the earlier "ask()/init() trapped in cli.py" state.) |
| **L3** | Amplifier tool modules | ✅ DONE + PROVEN | `bundle.md` + `modules/tool-repo-weaver/` expose 3 agent-callable tools (`repo_weaver_init` / `repo_weaver_weave` / `repo_weaver_ask`); `mount()` is Iron-Law compliant; `execute()` proven to return a real `ToolResult` with a real cited answer. |
| **L4** | Thin CLI | ✅ DONE | Ran across 30 repos this session. |

### The decision that matters: keep the subprocess boundary (no fold) — and WHY

repo-weaver's `.dot` nodes (L1) and tools (L3) **shell out to the repo-weaver
CLI**, which in turn shells out to the **wiki-weaver CLI**. We deliberately did
**not** fold wiki-weaver's private synthesis engine into repo-weaver at any level.

Why keep the boundary:

- **Low coupling.** repo-weaver still `dependencies = []` on wiki-weaver. The
  engine improves underneath for free; repo-weaver never tracks its internals.
- **0-LLM stance intact at every level.** Even the L1 `.dot` nodes and L3 tools
  are deterministic plumbing — they don't call models directly; all LLM work
  stays inside the wiki-weaver subprocess. The draft's "no LLM here" property is
  preserved end-to-end.
- **Regeneratable bricks.** Each level is a thin, replaceable shell over the same
  CLI contract — no level reaches past the public `wiki-weaver` command surface.

This is the honest answer to the draft's open question ("thin bundle vs. app"):
**thin bundle + reuse won, and held across all four surfaces.**

### Other fix reconciled this session

- **`doctor` provider-key check generalized.** `doctor` previously hard-required
  `GOOGLE_API_KEY`. It now accepts **any** supported provider key
  (`ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `OPENAI_API_KEY`) — required so the
  L1 `.dot` pipeline passes `doctor` in a Resolve/DTU worker provisioned with a
  non-Google provider.

### What stays parked (named, with reason — unchanged from the draft's discipline)

The four leverage levels are the complete portability story. Two further surfaces
remain deliberately **PARKED as future levels**, consistent with the draft's
"What we are NOT building":

- **MCP / REST service wrappers** — no consumer needs repo-weaver over a network
  boundary yet. Add when one is real.
- **Self-hosted web UIs** — the CLI `ask` + the L3 agent tools cover every known
  need. Add when an interactive product surface is concretely wanted.

Add a level when a consumer for it exists, not speculatively. (See
`ARCHITECTURE.md` §6 for the same roadmap stated from the architecture side.)
