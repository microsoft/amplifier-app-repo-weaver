# eval/ — Quality Harness for repo-weaver Corpora

Three tools for validating corpus quality after an incremental build or replay.

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
  by a judge agent, not by this script.**

## Scenario

Build the corpus incrementally over three windows (e.g. a 3-window replay), run the
gate after each window, then run the questions once against the final corpus.

## Commands

```bash
# (a) Gate a corpus after each ingest window:
python -m eval.coverage_check --corpus /path/to/corpus

# (b) Run the question set against a finished corpus:
python -m eval.run_questions \
    --corpus   /path/to/corpus \
    --questions eval/questions.yaml \
    --out       ./eval-out

# Both also accept --json for machine-readable output.
```

Outputs land in `--out` (default `./eval-out`), which is git-ignored.
