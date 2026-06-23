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
