"""Orchestrate: materialise source docs → _inbox → wiki-weaver ingest.

The only side effects are:
  1. Writing markdown files into ``<corpus>/_inbox/`` (idempotent).
  2. Spawning ``wiki-weaver ingest`` (only when ``dry_run=False``).

The target git repo is never mutated.

**Multi-repo usage** — use ``weave_multi()`` when a corpus spans several repos.
``weave()`` remains the single-repo primitive; ``weave_multi()`` with one repo
delegates to it so single-repo behaviour is bit-for-bit identical.
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


def weave_multi(
    corpus: str,
    repos: list[str],
    since: Optional[str],
    until: Optional[str],
    max_prs: int = 15,
    max_modules: int = 5,
    dry_run: bool = False,
) -> int:
    """Materialise source documents for multiple repos and optionally ingest.

    When *repos* contains exactly one entry this delegates to ``weave()``
    so single-repo behaviour is bit-for-bit identical (unqualified filenames,
    same log output).

    When *repos* contains more than one entry:
      * Each repo is materialised with a ``repo_qualifier`` equal to
        ``Path(repo).name`` (the directory base-name, e.g.
        ``"amplifier-app-team-pulse"``).  This qualifier is injected into
        every filename *and* into the body of every document so the
        synthesiser never merges pages from different repos.
      * All docs from all repos are collected and written to ``_inbox/`` in
        one pass before the single ``wiki-weaver ingest`` call.
      * If *since* is ``None`` each repo derives its own start date
        (one day before that repo's first commit) independently.

    Args:
        corpus:      Path to the wiki corpus directory (must be initialised).
        repos:       Ordered list of absolute paths to local git repositories.
        since:       Window start (exclusive).  ``None`` → per-repo auto-detect.
        until:       Window end (inclusive).  ``None`` → today.
        max_prs:     Maximum PRs to include in each repo's change digest.
        max_modules: Maximum module snapshot documents to emit per repo.
        dry_run:     If True, write _inbox files but skip the ingest step.

    Returns:
        Exit code: 0 = success, non-zero = failure.
    """
    if not repos:
        print("ERROR: weave_multi called with empty repos list.", file=sys.stderr)
        return 1

    # Single-repo: delegate to weave() for exact backward compatibility.
    if len(repos) == 1:
        return weave(
            corpus=corpus,
            repo=repos[0],
            since=since,
            until=until,
            max_prs=max_prs,
            max_modules=max_modules,
            dry_run=dry_run,
        )

    # ---- Multi-repo path ----
    today = date.today().isoformat()
    effective_until = until if until is not None else today

    print(f"[repo-weaver] Corpus:     {corpus}")
    print(f"[repo-weaver] Repos:      {len(repos)} repo(s)")
    print(f"[repo-weaver] Window end: {effective_until}")
    if dry_run:
        print("[repo-weaver] Mode:       dry-run (skipping ingest)\n")

    all_docs: list[tuple[str, str]] = []

    for idx, repo in enumerate(repos, 1):
        repo_qualifier = Path(repo).name  # e.g. "amplifier-app-team-pulse"

        # Per-repo since resolution.
        if since is not None:
            effective_since = since
        else:
            first = gitio.get_first_commit_date(repo)
            if first:
                first_date = date.fromisoformat(first)
                effective_since = (first_date - timedelta(days=1)).isoformat()
            else:
                effective_since = "2000-01-01"

        print(f"\n[repo-weaver] Repo {idx}/{len(repos)}: {repo}")
        print(f"[repo-weaver] Window:   {effective_since} \u2192 {effective_until}")

        docs = mat.materialize(
            repo=repo,
            since=effective_since,
            until=effective_until,
            max_prs=max_prs,
            max_modules=max_modules,
            repo_qualifier=repo_qualifier,
        )

        if not docs:
            print(f"[repo-weaver] No documents generated for {repo_qualifier}.")
        else:
            print(f"[repo-weaver] {len(docs)} document(s) from {repo_qualifier}.")
            all_docs.extend(docs)

    if not all_docs:
        print(
            "\n[repo-weaver] No source documents generated for any repo in this window.",
            file=sys.stderr,
        )
        return 0

    # ---- Write to _inbox ----
    inbox = Path(corpus) / "_inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    print(f"\n[repo-weaver] Writing {len(all_docs)} source document(s) to {inbox}/")
    for filename, content in all_docs:
        out_path = inbox / filename
        out_path.write_text(content, encoding="utf-8")
        print(f"  -> {out_path}")

    if dry_run:
        print(
            f"\n[repo-weaver] dry-run complete \u2014 {len(all_docs)} file(s) written to _inbox/.\n"
            "[repo-weaver] Inspect the files above, then run without --dry-run to ingest."
        )
        return 0

    # ---- Ingest via wiki-weaver ----
    print(f"\n[repo-weaver] Running: wiki-weaver ingest --wiki {corpus}")
    result = subprocess.run(
        ["wiki-weaver", "ingest", "--wiki", corpus],
    )
    return result.returncode
