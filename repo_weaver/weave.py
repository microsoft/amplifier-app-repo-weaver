"""Orchestrate: materialise source docs → _inbox → wiki-weaver ingest.

The only side effects are:
  1. Writing markdown files into ``<corpus>/_inbox/`` (idempotent).
  2. Spawning ``wiki-weaver ingest`` (only when ``dry_run=False``).

The target git repo is never mutated.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from . import gitio
from . import materialize as mat


def weave(
    corpus: str,
    repo: str,
    since: Optional[str],
    until: Optional[str],
    max_prs: int = 15,
    max_modules: int = 5,
    dry_run: bool = False,
) -> int:
    """Materialise source documents and optionally run wiki-weaver ingest.

    Args:
        corpus:      Path to the wiki corpus directory (must be initialised).
        repo:        Path to the local git repository.
        since:       Window start (exclusive). Defaults to one day before the
                     repo's first commit so that the first commit is included.
        until:       Window end (inclusive). Defaults to today.
        max_prs:     Maximum PRs to include in the change digest.
        max_modules: Maximum module snapshot documents to emit.
        dry_run:     If True, write _inbox files but skip the ingest step.

    Returns:
        Exit code: 0 = success, non-zero = failure.
    """
    # ---- Resolve date defaults ----
    today = date.today().isoformat()
    if until is None:
        until = today

    if since is None:
        first = gitio.get_first_commit_date(repo)
        if first:
            # Move back one day so the first commit falls inside the window.
            first_date = date.fromisoformat(first)
            since = (first_date - timedelta(days=1)).isoformat()
        else:
            since = "2000-01-01"

    print(f"[repo-weaver] Window: {since} \u2192 {until}")
    print(f"[repo-weaver] Repo:   {repo}")
    print(f"[repo-weaver] Corpus: {corpus}")
    if dry_run:
        print("[repo-weaver] Mode:   dry-run (skipping ingest)\n")

    # ---- Materialise ----
    docs = mat.materialize(repo, since, until, max_prs=max_prs, max_modules=max_modules)

    if not docs:
        print(
            "[repo-weaver] No source documents generated for this window.",
            file=sys.stderr,
        )
        return 0

    # ---- Write to _inbox ----
    inbox = Path(corpus) / "_inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    print(f"[repo-weaver] Writing {len(docs)} source document(s) to {inbox}/")
    for filename, content in docs:
        out_path = inbox / filename
        out_path.write_text(content, encoding="utf-8")
        print(f"  -> {out_path}")

    if dry_run:
        print(
            f"\n[repo-weaver] dry-run complete — {len(docs)} file(s) written to _inbox/.\n"
            "[repo-weaver] Inspect the files above, then run without --dry-run to ingest."
        )
        return 0

    # ---- Ingest via wiki-weaver ----
    print(f"\n[repo-weaver] Running: wiki-weaver ingest --wiki {corpus}")
    result = subprocess.run(
        ["wiki-weaver", "ingest", "--wiki", corpus],
        # Inherit stdin/stdout/stderr so output streams directly to the terminal.
    )
    return result.returncode
