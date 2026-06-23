"""Orchestrate: materialise source docs → _inbox → wiki-weaver ingest.

The only side effects are:
  1. Writing markdown files into ``<corpus>/_inbox/`` (idempotent).
  2. Spawning ``wiki-weaver ingest`` (only when ``dry_run=False``).

The target git repo is never mutated.

**Multi-repo usage** — use ``weave_multi()`` when a corpus spans several repos.
``weave()`` remains the single-repo primitive; ``weave_multi()`` with one repo
delegates to it so single-repo behaviour is bit-for-bit identical.

**Resilience** — after every ``wiki-weaver ingest`` call the ``_failed/``
directory is inspected and any stranded sources are automatically retried:

* **TRANSIENT** (overloaded / api / timeout / 4xx-5xx) → exponential
  back-off, same cycle budget.  On the final attempt the cycle budget is
  also bumped as a belt-and-suspenders measure.
* **NOT-CONVERGED** (cycle cap hit) → no back-off; ``--max-cycles`` is
  increased by ``_DEFAULT_CYCLES_BUMP`` on each attempt.
* **UNKNOWN** → treated as transient.

If a source is still in ``_failed/`` after all retries a loud named summary
is printed to stderr and the exit code is non-zero.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from . import gitio
from . import materialize as mat


# ---------------------------------------------------------------------------
# Retry / resilience configuration
# ---------------------------------------------------------------------------

#: Default max digest cycles sent to ``wiki-weaver ingest``.
#: Raised from wiki-weaver's built-in default of 3 to handle active repos.
_DEFAULT_MAX_CYCLES: int = 4

#: Default number of per-source retry attempts after an initial ingest failure.
_DEFAULT_MAX_RETRIES: int = 3

#: Base back-off delay (seconds) for transient errors (doubles each attempt).
_DEFAULT_RETRY_BASE_DELAY: float = 5.0

#: Extra cycles added per retry attempt for NOT-CONVERGED sources.
_DEFAULT_CYCLES_BUMP: int = 2

# Substrings (lower-cased) that indicate a *transient* provider error.
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "overloaded_error",
    "api_error",
    "internal server error",
    "timeout",
    "429",
    "500",
    "529",
)

# Substrings (lower-cased) that indicate a *convergence* failure.
_CONVERGE_MARKERS: tuple[str, ...] = (
    "cycle cap",
    "not converged",
    "did not converge",
    "cycles exceeded",
    "cycles exhausted",
    "failed to converge",
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _tee_subprocess(cmd: list[str]) -> tuple[int, str]:
    """Run *cmd*, echo output to the terminal, return ``(returncode, combined_output)``.

    Used for *per-source* retry calls where we need the output text to
    classify the next failure.  The initial full-corpus ingest uses
    :func:`subprocess.run` with inherited file descriptors so progress
    streams in real time.
    """
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        sys.stdout.write(result.stdout)
        sys.stdout.flush()
    if result.stderr:
        sys.stderr.write(result.stderr)
        sys.stderr.flush()
    return result.returncode, result.stdout + result.stderr


def _read_ledger_for_source(corpus_path: Path, source_name: str) -> str:
    """Return raw text of all ``.processed.jsonl`` rows that mention *source_name*."""
    ledger_path = corpus_path / ".processed.jsonl"
    if not ledger_path.exists():
        return ""
    try:
        text = ledger_path.read_text(encoding="utf-8")
    except OSError:
        return ""

    parts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if row.get("source") == source_name or row.get("filename") == source_name:
            parts.append(stripped)
    return "\n".join(parts)


def _classify_failure(
    source_name: str,
    corpus_path: Path,
    captured_output: str = "",
) -> str:
    """Classify a ``_failed/`` source as ``'not_converged'`` or ``'transient'``.

    Checks *captured_output* first (from the most-recent per-source ingest
    call), then falls back to the ``.processed.jsonl`` ledger.  When text
    is ambiguous the safe default is ``'transient'``; the cycle budget is
    also bumped on the final attempt via the caller's belt-and-suspenders
    logic.
    """
    text = (
        captured_output + "\n" + _read_ledger_for_source(corpus_path, source_name)
    ).lower()

    # Convergence markers take priority — different remedy than transient.
    if any(m in text for m in _CONVERGE_MARKERS):
        return "not_converged"
    if any(m in text for m in _TRANSIENT_MARKERS):
        return "transient"
    return "transient"  # safe default


def _retry_failed_sources(
    corpus: str,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    max_cycles: int = _DEFAULT_MAX_CYCLES,
    retry_base_delay: float = _DEFAULT_RETRY_BASE_DELAY,
    cycles_bump: int = _DEFAULT_CYCLES_BUMP,
    _sleep: Optional[Callable[[float], None]] = None,
) -> int:
    """Retry any sources in ``<corpus>/_failed/`` up to *max_retries* times.

    Tactic per failure class:

    * **TRANSIENT** — exponential back-off starting at *retry_base_delay*
      seconds (doubles each attempt).  Same cycle budget.  On the last
      attempt cycles are also bumped as belt-and-suspenders.
    * **NOT-CONVERGED** — no back-off; ``--max-cycles`` grows by
      *cycles_bump* on each attempt.
    * **UNKNOWN** — treated as transient.

    Each retry emits a progress line: source, attempt N/M, reason,
    back-off seconds, cycle budget.

    Returns 0 if every source eventually leaves ``_failed/``, 1 otherwise.
    Exhausted sources are left in ``_failed/`` and a named summary is
    printed to stderr — **no silent fallbacks**.
    """
    if _sleep is None:
        _sleep = time.sleep

    corpus_path = Path(corpus)
    failed_dir = corpus_path / "_failed"
    inbox = corpus_path / "_inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    if not failed_dir.exists():
        return 0

    initial_failed = sorted(p for p in failed_dir.iterdir() if p.is_file())
    if not initial_failed:
        return 0

    # Per-source state: name → {"attempts": int, "last_reason": str}
    source_state: dict[str, dict[str, Any]] = {}
    for fp in initial_failed:
        name = fp.name
        # Classify from ledger (no per-source captured output from initial full ingest).
        reason = _classify_failure(name, corpus_path, captured_output="")
        source_state[name] = {"attempts": 0, "last_reason": reason}

    for attempt in range(1, max_retries + 1):
        still_failing = (
            sorted(p for p in failed_dir.iterdir() if p.is_file())
            if failed_dir.exists()
            else []
        )
        if not still_failing:
            break

        for failed_file in still_failing:
            source_name = failed_file.name
            state = source_state.setdefault(
                source_name, {"attempts": 0, "last_reason": "transient"}
            )
            state["attempts"] = state["attempts"] + 1
            reason = str(state["last_reason"])

            # Compute delay and cycle budget for this attempt.
            if reason == "not_converged":
                delay = 0.0
                cycles = max_cycles + (attempt - 1) * cycles_bump
            else:
                # transient or unknown
                delay = retry_base_delay * (2 ** (attempt - 1))
                cycles = max_cycles
                # Last attempt: also bump cycles (belt-and-suspenders).
                if attempt == max_retries:
                    cycles += cycles_bump

            print(
                f"[repo-weaver] RETRY  source={source_name!r}  "
                f"attempt={attempt}/{max_retries}  "
                f"reason={reason}  "
                f"backoff={delay:.1f}s  "
                f"max-cycles={cycles}"
            )

            if delay > 0:
                _sleep(delay)

            # Move source: _failed/ → _inbox/ before calling ingest.
            inbox_dest = inbox / source_name
            failed_file.rename(inbox_dest)

            # Per-source ingest with appropriate cycle budget.
            cmd = [
                "wiki-weaver",
                "ingest",
                "--wiki",
                corpus,
                "--source",
                source_name,
                "--max-cycles",
                str(cycles),
            ]
            print(f"[repo-weaver] Running: {' '.join(cmd)}")
            _rc, output = _tee_subprocess(cmd)

            # Re-classify from fresh output + ledger for the next round.
            if (failed_dir / source_name).exists():
                new_reason = _classify_failure(source_name, corpus_path, output)
                state["last_reason"] = new_reason
            else:
                print(
                    f"[repo-weaver] OK  source={source_name!r} "
                    f"converged on attempt {attempt}."
                )
                state["last_reason"] = "success"

    # ---- Final report ----
    still_failed = (
        sorted(p.name for p in failed_dir.iterdir() if p.is_file())
        if failed_dir.exists()
        else []
    )

    if not still_failed:
        return 0

    print(
        f"\n[repo-weaver] ERROR: {len(still_failed)} source(s) exhausted all "
        f"{max_retries} retries and remain in _failed/:",
        file=sys.stderr,
    )
    for name in still_failed:
        last_reason = str(source_state.get(name, {}).get("last_reason", "unknown"))
        print(f"  - {name}  (last failure: {last_reason})", file=sys.stderr)
    print(
        "[repo-weaver] Inspect _failed/ and .processed.jsonl for details.",
        file=sys.stderr,
    )
    return 1


def _run_ingest_with_retry(
    corpus: str,
    max_cycles: int = _DEFAULT_MAX_CYCLES,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    retry_base_delay: float = _DEFAULT_RETRY_BASE_DELAY,
    _sleep: Optional[Callable[[float], None]] = None,
) -> int:
    """Run ``wiki-weaver ingest`` then auto-retry any ``_failed/`` sources.

    The initial full-corpus ingest streams directly to the terminal (real-time
    progress).  Per-source retry calls capture output for failure classification.

    Returns 0 if all sources ultimately succeed, non-zero on persistent failure.
    """
    cmd = [
        "wiki-weaver",
        "ingest",
        "--wiki",
        corpus,
        "--max-cycles",
        str(max_cycles),
    ]
    print(f"\n[repo-weaver] Running: {' '.join(cmd)}")
    # Stream the initial full-corpus ingest for real-time terminal feedback.
    initial_result = subprocess.run(cmd)
    initial_rc = initial_result.returncode

    if initial_rc != 0:
        print(
            f"[repo-weaver] WARNING: initial ingest exited {initial_rc}; "
            "checking _failed/ for retriable sources.",
            file=sys.stderr,
        )

    retry_rc = _retry_failed_sources(
        corpus=corpus,
        max_retries=max_retries,
        max_cycles=max_cycles,
        retry_base_delay=retry_base_delay,
        _sleep=_sleep,
    )

    # Retry result takes precedence: if every source was retried to success,
    # return 0.  If there was nothing to retry, honour the initial rc.
    if retry_rc != 0:
        return retry_rc
    return initial_rc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def weave(
    corpus: str,
    repo: str,
    since: Optional[str],
    until: Optional[str],
    max_prs: int = 15,
    max_modules: int = 5,
    dry_run: bool = False,
    max_cycles: int = _DEFAULT_MAX_CYCLES,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    retry_base_delay: float = _DEFAULT_RETRY_BASE_DELAY,
    _sleep: Optional[Callable[[float], None]] = None,
) -> int:
    """Materialise source documents and optionally run wiki-weaver ingest.

    Args:
        corpus:           Path to the wiki corpus directory (must be initialised).
        repo:             Path to the local git repository.
        since:            Window start (exclusive). Defaults to one day before the
                          repo's first commit so that the first commit is included.
        until:            Window end (inclusive). Defaults to today.
        max_prs:          Maximum PRs to include in the change digest.
        max_modules:      Maximum module snapshot documents to emit.
        dry_run:          If True, write _inbox files but skip the ingest step.
        max_cycles:       Digest cycle budget passed to ``wiki-weaver ingest``.
                          Default raised to 4 (from wiki-weaver's built-in 3) to
                          handle active repos.
        max_retries:      Max per-source retry attempts after a ``_failed/`` event.
        retry_base_delay: Base exponential back-off delay in seconds (transient).
        _sleep:           Injectable sleep callable (tests pass a no-op).

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
            f"\n[repo-weaver] dry-run complete \u2014 {len(docs)} file(s) written to _inbox/.\n"
            "[repo-weaver] Inspect the files above, then run without --dry-run to ingest."
        )
        return 0

    # ---- Ingest via wiki-weaver (with auto-retry) ----
    return _run_ingest_with_retry(
        corpus=corpus,
        max_cycles=max_cycles,
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
        _sleep=_sleep,
    )


def weave_multi(
    corpus: str,
    repos: list[str],
    since: Optional[str],
    until: Optional[str],
    max_prs: int = 15,
    max_modules: int = 5,
    dry_run: bool = False,
    max_cycles: int = _DEFAULT_MAX_CYCLES,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    retry_base_delay: float = _DEFAULT_RETRY_BASE_DELAY,
    _sleep: Optional[Callable[[float], None]] = None,
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
        corpus:           Path to the wiki corpus directory (must be initialised).
        repos:            Ordered list of absolute paths to local git repositories.
        since:            Window start (exclusive).  ``None`` → per-repo auto-detect.
        until:            Window end (inclusive).  ``None`` → today.
        max_prs:          Maximum PRs to include in each repo's change digest.
        max_modules:      Maximum module snapshot documents to emit per repo.
        dry_run:          If True, write _inbox files but skip the ingest step.
        max_cycles:       Digest cycle budget passed to ``wiki-weaver ingest``.
        max_retries:      Max per-source retry attempts after a ``_failed/`` event.
        retry_base_delay: Base exponential back-off delay in seconds (transient).
        _sleep:           Injectable sleep callable (tests pass a no-op).

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
            max_cycles=max_cycles,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
            _sleep=_sleep,
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

    # ---- Ingest via wiki-weaver (with auto-retry) ----
    return _run_ingest_with_retry(
        corpus=corpus,
        max_cycles=max_cycles,
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
        _sleep=_sleep,
    )
