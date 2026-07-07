"""Deterministic change-detection + discovery-free sync over an existing corpus.

``sync_corpus`` is glue, not a new engine: it reads the corpus's own
``_sources/*-changes.md`` filenames to recover (a) each tracked repo's own
last-sync date and (b) the set of already-tracked ``(owner, repo)`` pairs,
asks GitHub (via ``gh``) which of those repos have pushed since, ensures a
local clone for each changed repo, and re-weaves each one over the reused
single-repo :func:`repo_weaver.weave.weave` path.

No new orchestration engine, no ``.dot`` pipeline -- the existing weave /
gitio primitives do all the real work; this module only decides *which*
repos need re-weaving and *since when*.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

from wiki_weaver.lib import wiki_sources

from . import gitio
from .weave import weave as _weave

# Matches e.g. "microsoft__amplifier-app-repo-weaver-2026-07-05-changes.md"
# or the no-remote fallback form "some-basename-2026-07-05-changes.md".
_SOURCE_CHANGES_RE = re.compile(
    r"^(?P<qualifier>.+)-(?P<date>\d{4}-\d{2}-\d{2})-changes\.md$"
)


# ---------------------------------------------------------------------------
# Corpus introspection: last-sync date(s) + tracked (owner, repo) set
# ---------------------------------------------------------------------------


def _iter_source_change_files(corpus_path: Path) -> list[tuple[str, str]]:
    """Return ``(qualifier, date_str)`` pairs parsed from every ``*-changes.md``
    filename in ``<corpus>/_sources/``.

    Returns an empty list if the ``_sources/`` directory does not exist yet
    (a corpus that has never been woven).
    """
    sources_dir = wiki_sources(corpus_path)
    if not sources_dir.exists():
        return []

    pairs: list[tuple[str, str]] = []
    for fp in sources_dir.iterdir():
        if not fp.is_file():
            continue
        m = _SOURCE_CHANGES_RE.match(fp.name)
        if m:
            pairs.append((m.group("qualifier"), m.group("date")))
    return pairs


def _last_sync_date(corpus_path: Path) -> Optional[str]:
    """Return the maximum ``YYYY-MM-DD`` parsed across all change-digest filenames.

    This is a corpus-WIDE watermark, used only for the top-level informational
    ``last_sync`` report field and to detect a never-woven corpus (raise). It is
    NOT used to decide whether any individual repo changed -- see
    :func:`_per_repo_last_sync` for that (the per-repo correctness fix).

    Returns ``None`` when the corpus has no change-digest sources yet.
    """
    pairs = _iter_source_change_files(corpus_path)
    if not pairs:
        return None
    return max(date_str for _, date_str in pairs)


def _per_repo_last_sync(corpus_path: Path) -> dict[tuple[str, str], str]:
    """Return the maximum ``YYYY-MM-DD`` parsed from EACH tracked repo's OWN filenames.

    Fixes the corpus-wide-watermark bug: previously a single ``max()`` across
    every repo's filenames meant a repo with an older digest inherited a more
    recently-synced repo's date as its own last-sync, silently hiding any of
    its activity in that gap. Here each ``(owner, repo)`` pair's last-sync date
    is derived only from ITS OWN ``*-changes.md`` filenames.

    Only qualifiers containing the ``owner__repo`` double-underscore form
    contribute -- the no-remote fallback (bare basename) has no owner to query
    ``gh`` with, so those sources are invisible to ``sync`` (they were never
    reachable via GitHub in the first place).
    """
    per_repo: dict[tuple[str, str], str] = {}
    for qualifier, date_str in _iter_source_change_files(corpus_path):
        if "__" not in qualifier:
            continue
        owner, _, repo = qualifier.partition("__")
        if not owner or not repo:
            continue
        key = (owner, repo)
        if key not in per_repo or date_str > per_repo[key]:
            per_repo[key] = date_str
    return per_repo


def _tracked_repos(corpus_path: Path) -> tuple[set[tuple[str, str]], set[str]]:
    """Return ``(tracked (owner, repo) pairs, distinct owners)`` from filenames.

    Derived from :func:`_per_repo_last_sync` so the tracked set and the
    per-repo watermark always agree on which repos are known.
    """
    tracked = set(_per_repo_last_sync(corpus_path).keys())
    owners = {owner for owner, _ in tracked}
    return tracked, owners


# ---------------------------------------------------------------------------
# Clone management
# ---------------------------------------------------------------------------


def _ensure_local_clone(clone_path: Path, name_with_owner: str) -> bool:
    """Ensure a usable local clone exists at *clone_path*.

    * If *clone_path* already exists: verify it's a valid git repo and run
      ``git fetch`` to update remote refs (the actual fast-forward / staleness
      warning happens inside :func:`repo_weaver.weave.weave` itself via its
      existing ``_ensure_fresh_clone`` guard).
    * If it does not exist: ``gh repo clone <name_with_owner> <clone_path>``.

    Returns True on success, False on any failure (fail-loud is the caller's
    responsibility -- this just reports pass/fail).
    """
    if clone_path.exists():
        if not gitio.is_git_repo(str(clone_path)):
            return False
        gitio.fetch_origin(str(clone_path))
        return True

    clone_path.parent.mkdir(parents=True, exist_ok=True)
    return gitio.gh_clone_repo(name_with_owner, str(clone_path))


# ---------------------------------------------------------------------------
# sync_corpus
# ---------------------------------------------------------------------------


def sync_corpus(
    corpus: str,
    clones_dir: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
    dry_run: bool = False,
    max_modules: int = 0,
) -> dict[str, Any]:
    """Re-weave only the repos that changed since each repo's OWN last sync.

    No manual repo list is required: the corpus's own ``_sources/*-changes.md``
    filenames already record, per repo, the last date it was woven through
    (baked into the filename). This function:

    1. Determines each tracked repo's own last-sync date -- *since* if given
       (applied globally as an explicit override), else the max ``YYYY-MM-DD``
       parsed from THAT repo's own ``_sources/*-changes.md`` filenames. Repos
       are evaluated independently: one repo's more recent digest never masks
       another repo's older one.
    2. Recovers the tracked ``(owner, repo)`` set and distinct owners from
       those same filenames (an ``owner__repo`` qualifier is required -- the
       no-remote fallback form cannot be queried via ``gh``).
    3. For each owner, asks GitHub (``gh repo list``) for every non-fork
       ``amplifier*``-prefixed repo and its ``pushedAt`` date. A tracked repo
       is CHANGED if ``pushedAt`` is strictly after ITS OWN last-sync date.
       A genuine ``gh`` discovery failure for an owner (auth, rate-limit,
       network, unparsable output) is recorded in ``discovery_failed`` and
       is distinct from "gh succeeded, zero matching repos".
    4. Unless *dry_run*, ensures a local clone of each changed repo under
       *clones_dir* and re-weaves it (reusing the existing single-repo
       :func:`repo_weaver.weave.weave` path) over that repo's own window
       ``(its_last_sync, until]``.

    Args:
        corpus:      Path to the wiki corpus directory (must already exist
                     and have at least one change-digest source, unless
                     *since* is supplied explicitly).
        clones_dir:  Directory to hold/locate local clones, one subdirectory
                     per changed repo (``<clones_dir>/<owner>__<repo>``).
                     ``~`` is expanded.
        since:       Override for the last-sync date (``YYYY-MM-DD``,
                     exclusive), applied globally to every tracked repo --
                     an explicit override is an intentional caller directive
                     and takes precedence over any repo's own digest history.
                     Defaults to per-repo corpus-derived values.
        until:       Window end (inclusive). Defaults to today (UTC date).
        dry_run:     If True, detect and report the changed-repo list but do
                     NOT clone or weave anything.
        max_modules: Module snapshot cap forwarded to ``weave()`` for each
                     changed repo (default 0 -- changes-only, no module
                     snapshots, matching the "fast sync" use case).

    Returns:
        A result dict:
            ``last_sync``        (str)           -- the resolved *since*
                                                     override, or the
                                                     corpus-wide max digest
                                                     date (informational only;
                                                     each repo's own date is
                                                     used for its own CHANGED
                                                     decision -- see
                                                     ``changed[i]["since"]``).
            ``until``             (str)           -- the resolved window end.
            ``owners``            (dict[str,int]) -- per-owner changed-repo
                                                     counts.
            ``changed``           (list[dict])    -- changed repos: each has
                                                     ``owner``, ``repo``,
                                                     ``nameWithOwner``,
                                                     ``pushedAt``, and
                                                     ``since`` (that repo's
                                                     own effective last-sync
                                                     date, used for its weave
                                                     window).
            ``errors``            (list[str])     -- non-fatal owner-level
                                                     notes (e.g. gh returned
                                                     nothing for an owner with
                                                     tracked repos, or a
                                                     genuine gh failure).
            ``discovery_failed``  (list[str])     -- owners for which ``gh``
                                                     genuinely failed (auth,
                                                     rate-limit, network,
                                                     unparsable output) --
                                                     distinct from "gh
                                                     succeeded, zero repos".
                                                     A non-empty list here
                                                     means the CHANGED list is
                                                     incomplete for those
                                                     owners; callers (e.g. the
                                                     CLI) should exit non-zero.
        When *dry_run* is False, additionally:
            ``woven``       (list[dict])        -- ``{"repo", "returncode"}`` per
                                                   changed repo actually woven.
            ``failed``      (list[str])         -- ``nameWithOwner`` values that
                                                   failed to clone or weave.

    Raises:
        ValueError: if *since* is not given and the corpus has no
                    change-digest sources to derive a last-sync date from.
    """
    corpus_path = Path(corpus)
    clones_path = Path(clones_dir).expanduser()

    tracked, owners = _tracked_repos(corpus_path)
    per_repo_since = _per_repo_last_sync(corpus_path)

    last_sync = since if since is not None else _last_sync_date(corpus_path)
    if last_sync is None:
        raise ValueError(
            "No last-sync date available: the corpus has no *-changes.md "
            "sources yet and --since was not provided. Run `repo-weaver weave` "
            "at least once, or pass --since explicitly."
        )

    effective_until = until if until is not None else date.today().isoformat()

    result: dict[str, Any] = {
        "last_sync": last_sync,
        "until": effective_until,
        "owners": {},
        "changed": [],
        "errors": [],
        "discovery_failed": [],
    }

    if not owners:
        # Nothing tracked yet -- nothing to detect or weave.
        return result

    changed: list[dict[str, str]] = []
    errors: list[str] = []
    discovery_failed: list[str] = []
    per_owner_counts: dict[str, int] = {}

    for owner in sorted(owners):
        repos_info, gh_error = gitio.gh_list_repos(owner)
        owner_tracked = {repo for o, repo in tracked if o == owner}

        if gh_error is not None:
            # Genuine gh failure (auth, rate-limit, network, unparsable
            # output) -- distinct from "gh succeeded, zero repos". This
            # owner's CHANGED detection is incomplete; record it loudly so
            # the caller (CLI) can exit non-zero rather than silently
            # treating this the same as a real no-op.
            errors.append(f"{owner}: {gh_error}")
            discovery_failed.append(owner)
            per_owner_counts[owner] = 0
            continue

        if not repos_info and owner_tracked:
            errors.append(
                f"{owner}: gh returned no repos, but {len(owner_tracked)} "
                "tracked repo(s) were expected (check gh auth / rate limit)."
            )

        owner_changed = 0
        for r in repos_info:
            name = r.get("name")
            if not isinstance(name, str) or not name:
                continue
            if name not in owner_tracked:
                continue
            if r.get("isFork"):
                continue

            pushed_at_raw = r.get("pushedAt")
            pushed_at = pushed_at_raw if isinstance(pushed_at_raw, str) else ""
            pushed_date = pushed_at[:10]

            # Per-repo watermark fix: an explicit --since override applies
            # globally (intentional caller directive); otherwise each repo
            # is compared against ITS OWN last-sync date, not a corpus-wide
            # max that could mask this repo's activity behind another's.
            effective_since = (
                since
                if since is not None
                else per_repo_since.get((owner, name), last_sync)
            )

            if pushed_date and pushed_date > effective_since:
                name_with_owner_raw = r.get("nameWithOwner")
                name_with_owner = (
                    name_with_owner_raw
                    if isinstance(name_with_owner_raw, str) and name_with_owner_raw
                    else f"{owner}/{name}"
                )
                changed.append(
                    {
                        "owner": owner,
                        "repo": name,
                        "nameWithOwner": name_with_owner,
                        "pushedAt": pushed_at,
                        "since": effective_since,
                    }
                )
                owner_changed += 1

        per_owner_counts[owner] = owner_changed

    result["owners"] = per_owner_counts
    result["changed"] = changed
    result["errors"] = errors
    result["discovery_failed"] = discovery_failed

    if dry_run or not changed:
        return result

    # ---- Ensure clones (fail loud on any bad clone path, before weaving) ----
    clones_path.mkdir(parents=True, exist_ok=True)
    clone_paths: dict[str, Path] = {}
    clone_failures: list[str] = []
    for entry in changed:
        name_with_owner = entry["nameWithOwner"]
        clone_path = clones_path / f"{entry['owner']}__{entry['repo']}"
        if _ensure_local_clone(clone_path, name_with_owner):
            clone_paths[name_with_owner] = clone_path
        else:
            clone_failures.append(name_with_owner)

    if clone_failures:
        print(
            "ERROR: could not ensure a local clone for the following repo(s); "
            "aborting sync before weaving anything:\n  " + "\n  ".join(clone_failures),
            file=sys.stderr,
        )
        result["failed"] = clone_failures
        result["woven"] = []
        return result

    # ---- Weave each changed repo over the reused single-repo weave path ----
    # Success is determined empirically, not from the raw returncode: weave()
    # (via wiki-weaver's own .wiki/failed/ retry mechanism) can recover from
    # an initial subprocess failure -- e.g. an OOM-killed (-9) ingest whose
    # source is later retried successfully -- and the returncode it reports
    # can be stale relative to that recovery. Rather than trust it blindly,
    # check whether the expected change-digest actually landed in _sources/;
    # that file's presence is the ground truth for "this repo is woven".
    woven: list[dict[str, object]] = []
    weave_failures: list[str] = []
    sources_dir = wiki_sources(corpus_path)
    for entry in changed:
        name_with_owner = entry["nameWithOwner"]
        clone_path = clone_paths[name_with_owner]
        rc = _weave(
            corpus=corpus,
            repo=str(clone_path),
            since=entry["since"],
            until=effective_until,
            max_modules=max_modules,
            dry_run=False,
        )
        woven.append({"repo": name_with_owner, "returncode": rc})

        expected_filename = (
            f"{entry['owner']}__{entry['repo']}-{effective_until}-changes.md"
        )
        landed = (sources_dir / expected_filename).exists()
        if not landed:
            weave_failures.append(name_with_owner)

    result["woven"] = woven
    result["failed"] = weave_failures
    return result
