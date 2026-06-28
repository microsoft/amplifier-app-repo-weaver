# repo-weaver

repo-weaver turns one or more git repositories' **commits and merged PRs** into
markdown source documents, then feeds them to **wiki-weaver** (an LLM synthesis
engine) to build a queryable, cited knowledge corpus that is woven together over
time. repo-weaver is the git-history materializer and orchestration layer;
wiki-weaver does the synthesis, reconciliation, citation, and query.

**Personal vs team is just which repos are in the corpus.** A *personal* corpus
tracks your own repos; a *team* corpus tracks a shared set. There is no separate
mode or config — you point `init` at one repo or several, and the corpus is
"personal" or "team" purely by virtue of what you put in it.

---

## Ways to use repo-weaver — four leverage levels

repo-weaver is consumable **four** ways; the CLI documented here is just one of
them. All four are built and proven. See [ARCHITECTURE.md](ARCHITECTURE.md#6-portability--the-four-leverage-levels)
for detail.

| Level | Form | How you use it |
|-------|------|----------------|
| **L1** | `.dot` attractor pipelines | Standalone DOT pipelines in `pipelines/` that run under the Amplifier attractor engine / the Resolve dot-graph resolver — each pipeline shells out to the repo-weaver CLI. Proven end-to-end in the Resolve dot-graph resolver. |
| **L2** | Python library | `import repo_weaver` and call the public API — `init`, `weave`, `weave_multi`, `replay_windows`, `ask`, `materialize` — to embed repo-weaver in another codebase. |
| **L3** | Amplifier tool modules | `modules/tool-repo-weaver/` exposes agent-callable tools (`repo_weaver_init` / `repo_weaver_weave` / `repo_weaver_ask`) for use inside Amplifier bundles. |
| **L4** | CLI | The `repo-weaver` commands documented in this README — the rest of the doc covers this level. |

---

## How it works

- **Materialize git history** — for each time window, repo-weaver emits a per-repo
  change digest (merged PRs split into substantive vs routine, plus a
  commit-volume summary) and up to `--max-modules` per-module snapshots (purpose,
  file inventory at window-end, and what changed). The target repo is never
  mutated.
- **wiki-weaver ingest** — the generated documents are written to `<corpus>/_inbox/`
  and handed to `wiki-weaver ingest`, the synthesis engine repo-weaver depends on.
- **A cited wiki** — wiki-weaver compiles the sources into wiki pages where every
  claim cites a source id. You query it with `repo-weaver ask`.
- **`replay` weaves updates in over time** — running successive windows adds each
  period's new history into the *existing* pages (modules accrue a History
  section) rather than overwriting, so the corpus reads as the repo's evolution.

---

## Requirements

Run `repo-weaver doctor` **first** — it checks every item below and exits non-zero
if anything is missing. A green doctor means the long ingest run will not die
partway through on a missing key.

| Requirement | How to satisfy | Notes |
|-------------|----------------|-------|
| **wiki-weaver** on PATH | `uv tool install git+https://github.com/microsoft/amplifier-bundle-wiki-weaver` | The install fix landed in **PR #3 (now merged to main)**, so a fresh install works. |
| **git** on PATH | system package manager | repo-weaver shells out to `git`; it is never imported. |
| **gh** authenticated | install from <https://cli.github.com/>, then `gh auth login` | Verify with `gh auth status`. PR data is fetched via `gh`. |
| **At least one LLM provider key** | export any one of `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, or `OPENAI_API_KEY` | `doctor` accepts any of the three — it no longer hard-requires a specific provider. Large runs have been done on Anthropic. |

`doctor` also confirms the packaged `policy/schema.md` is present (it ships inside
the repo-weaver package). All commands except `doctor` refuse to run if
wiki-weaver is not on PATH.

```bash
repo-weaver doctor
```

---

## Install

repo-weaver is an **unpublished project** — there is no PyPI release. Install it
directly from the GitHub repo:

```bash
uv tool install git+https://github.com/microsoft/amplifier-app-repo-weaver
```

Or, if you want a local checkout to hack on, install editable from a clone:

```bash
git clone https://github.com/microsoft/amplifier-app-repo-weaver
cd amplifier-app-repo-weaver
uv tool install --editable .
```

The packaged `policy/schema.md` ships inside the wheel, so the non-editable
install works too — the editable-from-clone path is just for local development.
After installing, verify everything:

```bash
repo-weaver doctor
```

---

## Quickstart (single repo, small and cheap)

Start with one repo and a short window so the first run is fast and inexpensive.

```bash
# 1. Verify dependencies (do this first)
repo-weaver doctor

# 2. Scaffold a corpus pointed at a local clone
repo-weaver init ~/corpora/team-pulse --repo ~/src/amplifier-app-team-pulse

# 3. Weave one short window, capping modules to keep it cheap
repo-weaver weave --corpus ~/corpora/team-pulse \
  --since 2026-06-15 --until 2026-06-19 \
  --max-modules 1

# 4. Ask a cited question against the corpus
repo-weaver ask "What did PR #96 add?" --corpus ~/corpora/team-pulse
```

`--since` is exclusive, `--until` is inclusive (through 23:59:59). If you omit
`--since`, repo-weaver defaults to one day before the repo's first commit; if you
omit `--until`, it defaults to today. Add `--dry-run` to `weave` to write the
`_inbox/` files and inspect them **without** spending any LLM time.

---

## Multi-repo (personal vs team)

Register several repos at `init` by repeating `--repo`:

```bash
repo-weaver init ~/corpora/my-team \
  --repo ~/src/amplifier-app-team-pulse \
  --repo ~/src/amplifier-bundle-wiki-weaver
```

Then `weave` / `replay` **with no `--repo` flag** cover every repo recorded in the
corpus config:

```bash
repo-weaver weave --corpus ~/corpora/my-team --since 2026-06-15 --until 2026-06-22
```

In multi-repo mode, every filename and document body is **repo-qualified** (e.g.
`module-amplifier-app-team-pulse-frontend-2026-06-22.md`) so wiki-weaver never
merges pages from different repos and citations always point back to the right
source. Passing an explicit `--repo` overrides the config and uses the historic
single-repo, unqualified-filename behavior. An invalid or unreachable repo aborts
the whole multi-repo run loudly rather than producing phantom-empty entries.

---

## Over time (replay)

`replay` tiles a list of cutoff dates into **gapless** windows and weaves them in
order, so each window adds only its new history into the existing pages:

```bash
repo-weaver replay --corpus ~/corpora/team-pulse \
  --windows "2026-06-15,2026-06-19,2026-06-22"
```

The start is computed automatically as one day before the earliest first commit
across the configured repos, producing windows `(start, 2026-06-15]`,
`(2026-06-15, 2026-06-19]`, `(2026-06-19, 2026-06-22]`. Cutoff dates must be
`YYYY-MM-DD` and ascending. Re-running `replay` weaves newer history into the
pages that already exist. On the first window that fails, replay stops (fail
loud).

---

## Cost and time expectations (be honest)

- **Ingest is sequential.** wiki-weaver processes one source at a time; budget
  roughly **5–10 minutes per source**. A *source* is a per-repo change digest
  **or** a single module snapshot — so one window of one repo with
  `--max-modules 5` is up to 6 sources.
- **LLM calls cost money.** Every source is one or more synthesis calls against
  your provider key.
- **Many-repo or long-window runs can take HOURS.** A multi-repo `replay` over
  many windows multiplies sources by windows by repos.

**Recommendation:** start with **one repo, a short window, and `--max-modules 1`**.
Scale up only once you have seen the corpus quality and the per-source timing.

Two knobs tune the ingest behavior:

| Flag | Default | What it does |
|------|---------|--------------|
| `--max-cycles N` | 4 | Digest cycle budget passed to `wiki-weaver ingest`. Raise for dense repos that do not converge in the default budget. |
| `--max-retries N` | 3 | Per-source retry attempts after a `.wiki/failed/` event. Transient provider errors (overloaded, rate limit, timeout, 429/500/503/504/529) auto-retry with exponential back-off; not-converged sources retry with a bumped cycle budget; permanent errors (auth, 404, unrecognised text) are **not** retried and fail loud. |

`--max-prs` (default 15) and `--max-modules` (default 5) cap how much history each
window materializes.

---

## Dashboard (browse the corpus as HTML)

Once a corpus has pages, render it into a single **self-contained HTML dashboard**:

```bash
repo-weaver build-dashboard ~/corpora/my-team --out ~/my-team.html
```

| Argument | Required | What it does |
|----------|----------|--------------|
| `<corpus>` | yes | The wiki corpus directory to render. |
| `--out PATH` | yes | Destination `.html` file. |
| `--theme PATH` | no | A `theme.json` to override the corpus's `.wiki/dashboard/theme.json`. |

`build-dashboard` shells out to `wiki-weaver build-dashboard` (the same subprocess
boundary as `weave` and `ask` — all synthesis, query, and dashboard LLM work stays
behind the wiki-weaver CLI, with no engine imports or vendoring). The two
repo-specific touches it adds are:

- **Grouped by repo** — the sidebar is grouped by each page's `repos:` frontmatter
  list. A page that touches several repos appears under **every** repo it touches
  (multi-membership), so you can read the corpus one repo at a time.
- **GitHub group links** — each repo group header is a live link to
  `https://github.com/<owner/repo>`.

The output is one self-contained `.html` file — open it directly in a browser or
in Obsidian; **no server required**.

**Theming.** On the first run, repo-weaver seeds the corpus with a default
`<corpus>/.wiki/dashboard/theme.json` (title **"Repo Weaver"** plus a slate
accent). It **never clobbers** an existing `theme.json`, so your customizations
survive re-runs. Pass `--theme PATH` to override per-build; theming (color /
typography / shape tokens, optional title, and `custom.css`) flows through to
wiki-weaver, which reads the theme file.

> [!IMPORTANT]
> `build-dashboard` requires a **current** wiki-weaver — one that has the
> `build-dashboard` subcommand. Verify with:
> ```bash
> wiki-weaver build-dashboard --help
> ```
> If that fails, your installed wiki-weaver is too old. Update it (the `--force`
> reinstalls over the existing tool):
> ```bash
> uv tool install --force git+https://github.com/microsoft/amplifier-bundle-wiki-weaver
> ```
> repo-weaver also probes this for you and fails loud with the same hint rather
> than producing a broken dashboard.

---

## Troubleshooting

- **`doctor` reports a failure.** Fix the named row before any long run:
  - *No LLM provider key set* → export at least one of `ANTHROPIC_API_KEY`,
    `GOOGLE_API_KEY`, or `OPENAI_API_KEY`. `doctor` accepts any of them.
  - *gh not authenticated* → `gh auth login`, confirm with `gh auth status`.
  - *wiki-weaver not found on PATH* → install it (see Requirements).
- **A source lands in `.wiki/failed/`.** weave auto-retries transient provider errors
  (back-off) and not-converged sources (more cycles), but fails loud on permanent
  errors and leaves them in `.wiki/failed/` with a named summary on stderr. Inspect
  `.wiki/failed/` and `.wiki/.processed.jsonl` for the diagnostic text.
- **Check corpus integrity** with the deterministic gate:
  ```bash
  python -m eval.coverage_check --corpus ~/corpora/team-pulse
  ```
  It PASSes only when every registered source was ingested, has a converged
  ledger entry, and `.wiki/failed/` is empty.

---

## Known limitations

- **Unpublished / local install only.** No PyPI release; editable-from-clone is
  the supported path.
- **Single-subject eval.** The eval harness (`eval/`) is built and verified
  against one subject repo (`microsoft/amplifier-app-team-pulse`).
- **No resume-from-checkpoint.** A partial multi-hour run cannot resume from where
  it stopped; re-running re-weaves the windows (already-ingested identical sources
  are deduped by wiki-weaver, but the run restarts from the top).
- **Corpus quality depends on PR/commit density** in the window. A window with few
  or no merged PRs yields a thin digest; areas without READMEs get an inferred
  (clearly marked) module purpose.

---

## Architecture

```
repo-weaver/
  repo_weaver/
    __init__.py     # public library API (init/weave/ask/...)  [L2]
    cli.py          # argparse subcommand dispatch; main() -> exit code
    gitio.py        # read-only git + gh subprocess helpers
    materialize.py  # window -> list of (filename, markdown) source docs
    weave.py        # orchestrate: materialize -> _inbox -> wiki-weaver ingest (+ retry)
    policy/
      schema.md     # code-fit page taxonomy (copied into the corpus on init)
  modules/
    tool-repo-weaver/  # Amplifier tool module (L3)
  pipelines/        # .dot attractor pipelines (L1)
  eval/             # quality harness (see eval/README.md)
```

repo-weaver shells out to `wiki-weaver`, `git`, and `gh` for all synthesis, query,
git, and `gh` work — it never folds in their engines and never mutates a repo's
working tree. The one import is **non-LLM**: it pulls wiki-weaver's corpus-layout
path constants (`wiki_ledger`, `wiki_failed`, `wiki_sources`, `wiki_inbox`,
`wiki_dashboard`) directly, so the two repos share a **single source of truth** for
where corpus files live (the `.wiki/` layout). See [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Contributing

> [!NOTE]
> This project is not currently accepting external contributions, but we're actively working toward opening this up. We value community input and look forward to collaborating in the future. For now, feel free to fork and experiment!

Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
