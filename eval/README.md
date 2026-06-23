# eval/ — Quality Harness for repo-weaver Corpora

Five tools for validating corpus quality after an incremental build or replay.

## Pieces

- **`coverage_check.py`** — Deterministic integrity gate. Checks that every source
  registered in `.sources.json` was actually ingested (regression gate for the
  "silent skip on filename collision" bug), has a converged ledger entry, and that
  `_failed/` is empty. Run this after **each** ingest window.

- **`questions.yaml`** — 7 curated ground-truth Q&A pairs covering attribution,
  content, temporal, evolution, and fabrication-probe questions against the
  `microsoft/amplifier-app-team-pulse` corpus. Facts verified via `gh pr view`.

- **`run_questions.py`** — Fires each question against a built corpus via
  `repo-weaver ask --json`, saves raw `{answer, pages_used, refused}` payloads to
  `--out/answers/<id>.json`, and writes an `answers.index.json`. **Grading is done
  by a judge agent, not by this script.** Accepts `--questions` so it can run
  either `questions.yaml` or `held_back.yaml` (or any compatible file).

- **`held_back.yaml`** — 4 held-back Q&A pairs for the overfitting guard (see below).

- **`trace_grounding.py`** — Per-token grounding tracer (see below).

## Scenario

Build the corpus incrementally over three windows (e.g. a 3-window replay), run the
gate after each window, then run the questions once against the final corpus.

## Commands

```bash
# (a) Gate a corpus after each ingest window:
python -m eval.coverage_check --corpus /path/to/corpus

# (b) Run the tuning question set against a finished corpus:
python -m eval.run_questions \
    --corpus   /path/to/corpus \
    --questions eval/questions.yaml \
    --out       ./eval-out

# (c) Run the held-back (overfitting-guard) question set:
python -m eval.run_questions \
    --corpus   /path/to/corpus \
    --questions eval/held_back.yaml \
    --out       ./eval-out-held-back

# (d) Trace grounding for a single answer:
python -m eval.trace_grounding \
    --corpus /path/to/corpus \
    --answer ./eval-out/answers/<id>.json

# (e) Gate on grounded_rate (exit 1 if below threshold):
python -m eval.trace_grounding \
    --corpus /path/to/corpus \
    --answer ./eval-out/answers/<id>.json \
    --fail-under 0.5

# All tools also accept --json for machine-readable output.
```

Outputs land in `--out` (default `./eval-out`), which is git-ignored.

---

## Reproducing the eval (the 11/11 scorecard)

The eval proves three things about a corpus built from a known repo's history:

1. **Accuracy** — answers state the correct PR numbers, titles, authors, and merge
   dates (verified against `gh pr view`).
2. **Groundedness** — concrete claims trace back to the materialized source docs
   (`trace_grounding.py`).
3. **Fabrication-refusal** — out-of-scope probes (GraphQL, Kubernetes, message
   queues) are refused or marked "not covered" rather than answered with invented
   detail.

The scorecard is **11 questions**: 7 curated (`questions.yaml`) + 4 held-back
(`held_back.yaml`). The pieces:

- `questions.yaml` — 7 curated ground-truth Q&A pairs (facts verified via `gh`).
- `held_back.yaml` — 4 held-back Q&A pairs covering areas NOT used to tune the
  materializer (overfitting guard).
- `coverage_check.py` — deterministic integrity gate (every source ingested,
  converged, `_failed/` empty).
- `run_questions.py` — fires the asks and saves raw answers.
- `trace_grounding.py` — classifies each concrete claim GROUNDED /
  SYNTHESIZED_ONLY / UNGROUNDED.

### Pin the dependency

The score is only reachable when **wiki-weaver is the current `main`** — it must
include the import fix merged in **PR #3**. Install/refresh it before building the
corpus:

```bash
uv tool install --force git+https://github.com/microsoft/amplifier-bundle-wiki-weaver
repo-weaver doctor   # confirm wiki-weaver + git + gh + provider key are all green
```

### Step 1 — clone the subject repo

The subject the eval was built and verified against is
`microsoft/amplifier-app-team-pulse`:

```bash
gh repo clone microsoft/amplifier-app-team-pulse ~/src/amplifier-app-team-pulse
```

### Step 2 — build the corpus with `replay`

Scaffold a corpus pointed at the clone, then weave the windows that cover the PRs
the questions reference (all merged 2026-06-15 through 2026-06-22):

```bash
repo-weaver init ~/corpora/team-pulse --repo ~/src/amplifier-app-team-pulse

repo-weaver replay --corpus ~/corpora/team-pulse \
  --windows "2026-06-15,2026-06-19,2026-06-22"
```

`replay` tiles gapless windows starting one day before the repo's first commit, so
`(start, 2026-06-15]`, `(2026-06-15, 2026-06-19]`, `(2026-06-19, 2026-06-22]`
cover every referenced PR. **Ingest is sequential and can take several minutes per
source** — see the cost/time note in the top-level README.

### Step 3 — gate corpus integrity

```bash
python -m eval.coverage_check --corpus ~/corpora/team-pulse
```

Must PASS (every registered source ingested + converged, `_failed/` empty) before
the answers mean anything.

### Step 4 — run both question sets

```bash
# Curated (7)
python -m eval.run_questions \
  --corpus    ~/corpora/team-pulse \
  --questions eval/questions.yaml \
  --out       ./eval-out

# Held-back (4)
python -m eval.run_questions \
  --corpus    ~/corpora/team-pulse \
  --questions eval/held_back.yaml \
  --out       ./eval-out-held-back
```

### Step 5 — trace grounding on the answers

Run the tracer on each answer file produced above, e.g.:

```bash
python -m eval.trace_grounding \
  --corpus ~/corpora/team-pulse \
  --answer ./eval-out/answers/pr72-projection-synthesis.json
```

Add `--fail-under 0.5` to turn it into a hard gate on `grounded_rate`.

### Reading the result

Results are **nondeterministic** — wiki-weaver synthesis is LLM-driven, so exact
wording varies run to run. The PASS criteria are about **correct facts and
citations** and **refusing out-of-scope questions**, NOT verbatim string match.
`trace_grounding.py` is a heuristic (exact-substring) first pass that surfaces
candidates; the final accuracy/fabrication verdict is adjudicated by the judge
agent against the `expected:` items in each YAML entry.

---

## Overfitting Guard — `held_back.yaml`

The materialiser was tuned against `questions.yaml` (frontend-toolchain evolution,
PR #72, PR #96, PR #128). `held_back.yaml` contains **4 questions on areas that were
NOT used during tuning**:

| id | area | kind |
|----|------|------|
| `hb-m365-sync-evolution` | M365 ingestion arc: PRs #72 → #96 → #97 | evolution |
| `hb-pr-review-loop` | Self-improving PR-review loop: PRs #104/#112/#122/#125/#127 | content |
| `hb-ask-endpoint-and-lens` | /ask endpoint (#73) and lens content-collection (#109) | content |
| `fabrication-probe-message-queue` | "Which message queue does team-pulse use?" | fabrication-probe |

Run the held-back set with the same `run_questions.py` command, just pointing
`--questions` at `eval/held_back.yaml`.  If the corpus answers these correctly, the
pipeline generalises beyond its tuning set.  If it fails, that's the overfitting
signal — not a sign that the corpus is wrong.

---

## Grounding Tracer — `trace_grounding.py`

An independent judge flagged that corpus answers sometimes add concrete specifics
(file counts, test counts, commit hashes, percentages) that can't be verified against
ground truth — they could be corpus-grounded detail OR ask-time confabulation.
`trace_grounding.py` gives the grounding tracer **teeth**: it extracts every
checkable concrete token from an answer and traces each against the corpus.

### What it checks

Checkable tokens extracted from the answer text:

| Category | Examples |
|----------|---------|
| `backtick_id` | `` `routes_lens.py` ``, `` `synthesize.dot` `` |
| `iso_date` | `2026-06-22` |
| `pr_ref` | `#72`, `#128` |
| `version` | `v7`, `8.0.16` |
| `noun_count` | `34 files`, `78 tests`, `29 pages` |
| `hex_hash` | `d7167f9a`, `2a9d938f` |

### The three classifications

| Classification | Meaning |
|----------------|---------|
| **GROUNDED** | Token found as exact substring in `_archive/*.md` (the source inputs fed to the materialiser). Traceable to real ground truth. |
| **SYNTHESIZED_ONLY** | Token found in a wiki page (`*.md`) but **not** in any source doc. The wiki synthesis introduced this phrasing — softer signal; may be faithful paraphrase or may have drifted. |
| **UNGROUNDED** | Token found in **neither** source docs nor wiki pages. Present only in the ask-time answer — the strongest confabulation candidate. |

### Heuristic caveat

Matching is **exact-substring, case-sensitive**.  A real claim phrased differently
than the source (e.g. `"78 passing tests"` vs `"78 pass"`) may show as
`SYNTHESIZED_ONLY` or `UNGROUNDED` even if the underlying fact is grounded.
This tool **surfaces candidates** for a judge to adjudicate; it does not convict.

### Pair with the judge

`trace_grounding.py` is the mechanical first pass.  After it flags UNGROUNDED or
SYNTHESIZED_ONLY tokens, the judge agent verifies whether the underlying claim is
substantively correct — just rephrased — or actually invented.

### `--fail-under` gate

```bash
python -m eval.trace_grounding \
    --corpus /path/to/corpus \
    --answer ./eval-out/answers/<id>.json \
    --fail-under 0.5   # exit 1 if grounded_rate < 0.5
```

Use this to gate a CI step: if `grounded_rate` falls below the threshold the command
exits 1, blocking the pipeline until a human reviews the flagged tokens.
