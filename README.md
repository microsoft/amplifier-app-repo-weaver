# repo-weaver

Turn a git repo's commits and PR history into markdown source documents, then feed them to **wiki-weaver** to build a queryable, cited knowledge corpus.

`wiki-weaver` is the engine (LLM synthesis pipeline). `repo-weaver` is the git-source materializer and orchestration layer on top. Everything downstream — synthesis, reconciliation, validation, citation, query — is handled by wiki-weaver unchanged.

---

## Install

**Requirements:** Python ≥ 3.11, `wiki-weaver` on PATH, `git` on PATH, `gh` on PATH with `gh auth login` done, `ANTHROPIC_API_KEY` set.

```bash
# From the repo-weaver directory:
pip install -e .
# or
uv tool install --editable .
```

Run `repo-weaver doctor` after install to verify all dependencies.

---

## Commands

### `repo-weaver doctor`

Checks all hard requirements and prints a status table. Exits 1 if anything is missing.

```bash
repo-weaver doctor
```

---

### `repo-weaver init <corpus_dir> [--repo PATH]`

Scaffolds a wiki corpus via `wiki-weaver init --plain`, then installs the code-fit `policy/schema.md`. Records the repo path for future commands.

```bash
repo-weaver init ~/corpora/my-project --repo ~/src/my-project
```

---

### `repo-weaver weave --corpus DIR [options]`

Materializes source documents for the given time window, writes them to `<corpus>/_inbox/`, and calls `wiki-weaver ingest` (unless `--dry-run`).

```bash
# Dry run: inspect generated source docs before spending LLM time
repo-weaver weave --corpus ~/corpora/my-project \
  --repo ~/src/my-project \
  --since 2026-06-01 --until 2026-06-16 \
  --dry-run

# Real ingest
repo-weaver weave --corpus ~/corpora/my-project \
  --since 2026-06-01 --until 2026-06-16
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--corpus DIR` | required | Corpus directory (from `init`) |
| `--repo PATH` | from init config | Git repo to materialize from |
| `--since YYYY-MM-DD` | repo's first commit | Window start (exclusive) |
| `--until YYYY-MM-DD` | today | Window end (inclusive) |
| `--max-prs N` | 15 | Max merged PRs to include |
| `--max-modules N` | 5 | Max module snapshot docs |
| `--dry-run` | false | Write _inbox but skip ingest |

**What gets generated per window:**

1. **`<until>-changes.md`** — a change digest: merged PRs with titles, authors, trimmed bodies, file counts; plus a commit-volume summary per top-level directory.
2. **Up to `--max-modules` `module-<slug>.md` files** — snapshots of the most-changed top-level directories in the window, each with: inferred or README-derived purpose, file inventory at window-end, and a summary of what changed.

Module snapshots are regenerated each window. If the module changed, its content changes, so wiki-weaver re-ingests and updates the page. Unchanged modules produce identical bytes and are correctly skipped by dedup.

---

### `repo-weaver ask "<question>" --corpus DIR [--json]`

Pass-through to `wiki-weaver ask`. Returns a cited answer from the compiled corpus.

```bash
repo-weaver ask "What does the synthesis-pipeline module do?" --corpus ~/corpora/my-project
repo-weaver ask "Why was the M365 sync added?" --corpus ~/corpora/my-project --json
```

---

### `repo-weaver replay --corpus DIR --windows "D1,D2,..." [options]`

**The over-time demo.** Given sorted cutoff dates, weaves successive non-overlapping windows so each run adds only that window's new material. wiki-weaver merges each batch into the existing corpus pages, so the final corpus reads as a temporal history of the repo.

```bash
repo-weaver replay \
  --corpus ~/corpora/my-project \
  --repo ~/src/my-project \
  --windows "2026-04-01,2026-05-01,2026-06-01,2026-06-16"
```

Each window `(d_i, d_{i+1}]` is ingested in order. On first failure, replay stops (fail loud).

---

## The over-time replay idea

A single `weave` call materializes a snapshot. `replay` is what turns the corpus into a living history:

- Each replay window adds only that period's PRs and module state.
- wiki-weaver's page reconciler sees a module page already exists and adds the new state with a citation, rather than overwriting.
- Ask questions like "How did the frontend module evolve between April and June?" and get a cited, time-aware answer.

This is the intended mechanism for tracking a repo's evolution over time without re-ingesting everything on each run.

---

## Architecture

```
repo-weaver/
  cli.py          # argparse subcommand dispatch; main() -> exit code
  gitio.py        # read-only git + gh subprocess helpers
  materialize.py  # window -> list of (filename, markdown) source docs
  weave.py        # orchestrate: materialize -> _inbox -> wiki-weaver ingest
  policy/
    schema.md     # code-fit page taxonomy (copied into corpus on init)
```

repo-weaver shells out to `wiki-weaver`, `git`, and `gh` — it never imports them as Python libraries and never mutates a repo's working tree.
