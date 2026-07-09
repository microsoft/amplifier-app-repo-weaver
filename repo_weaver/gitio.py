"""Git and gh subprocess helpers.

Most functions are read-only (log, show, ls-tree, shortlog, rev-list,
remote get-url) and do not modify the working tree.

The staleness-check helpers — :func:`fetch_origin`, :func:`fast_forward_origin`
— intentionally have side effects on the clone (fetch updates remote refs;
fast-forward advances HEAD).  They are called only when the caller explicitly
requests a freshness check (i.e. when ``--no-fetch`` is not set).
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import time
from typing import Callable, Literal, Optional


# ---------------------------------------------------------------------------
# Internal runner
# ---------------------------------------------------------------------------


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command and return the result (never raises on non-zero exit)."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


# Retry configuration for `gh` CLI calls specifically (not plain `git`).
_GH_RETRY_MAX_ATTEMPTS = 3
_GH_RETRY_BASE_DELAY = 1.0  # seconds; doubles each attempt (1s, 2s, 4s)


def _run_gh_with_retry(
    cmd: list[str],
    max_attempts: int = _GH_RETRY_MAX_ATTEMPTS,
    base_delay: float = _GH_RETRY_BASE_DELAY,
    _sleep: Optional[Callable[[float], None]] = None,
) -> subprocess.CompletedProcess[str]:
    """Run a ``gh`` CLI command, retrying non-zero exits with exponential back-off.

    Up to *max_attempts* attempts (default 3), waiting *base_delay* seconds
    before the first retry and doubling each subsequent attempt (1s, 2s, 4s
    by default). Any non-zero exit is treated as potentially transient
    (network blip, rate-limit) and retried -- `gh`'s own error text is not
    parsed to decide retry-worthiness; that classification would be fragile
    and the cost of one extra retry on a genuinely permanent error (bad auth,
    404) is small compared to silently failing a scheduled/unattended run.

    Never swallows a failure: if every attempt fails, the LAST attempt's
    ``CompletedProcess`` (non-zero returncode, real stderr) is returned as-is
    so callers see the true failure, not a fabricated success.
    """
    sleep_fn = _sleep if _sleep is not None else time.sleep
    result = _run(cmd)
    attempt = 1
    while result.returncode != 0 and attempt < max_attempts:
        sleep_fn(base_delay * (2 ** (attempt - 1)))
        result = _run(cmd)
        attempt += 1
    return result


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def get_origin_url(repo: str) -> Optional[str]:
    """Return the remote origin URL for a repo, or None if unavailable."""
    r = _run(["git", "-C", repo, "remote", "get-url", "origin"])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.strip()


def parse_owner_repo(url: str) -> Optional[tuple[str, str]]:
    """Parse (owner, repo) from a GitHub HTTPS or SSH URL.

    Accepts:
      https://github.com/owner/repo.git
      git@github.com:owner/repo.git
    Returns None if url is not a recognisable GitHub remote.
    """
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    return None


def is_git_repo(repo: str) -> bool:
    """Return True if *repo* is a valid, reachable git repository.

    Uses ``git rev-parse --is-inside-work-tree`` which exits non-zero for any
    path that is not inside a git working tree (including non-existent paths,
    bare non-git directories, and permission-denied paths).
    """
    r = _run(["git", "-C", repo, "rev-parse", "--is-inside-work-tree"])
    return r.returncode == 0


def get_first_commit_date(repo: str) -> Optional[str]:
    """Return YYYY-MM-DD of the earliest commit on HEAD, or None."""
    r = _run(
        [
            "git",
            "-C",
            repo,
            "log",
            "--reverse",
            "--format=%cs",
            "--max-parents=0",
            "HEAD",
        ]
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.strip().split("\n")[0]


def get_window_rev(repo: str, until: str) -> Optional[str]:
    """Return the SHA of the most recent commit at or before end-of-day on *until*.

    Returns ``None`` if no such commit exists — i.e. *until* predates the
    repo's first commit or the repo is invalid/empty.  Callers **must not**
    fall back to HEAD in this case: HEAD may post-date *until*, causing module
    snapshots to reflect a later historical state than intended.

    The previous HEAD-fallback behaviour has been removed deliberately.  When
    *until* is genuinely in the future relative to all commits, ``git rev-list
    --before=<future>`` already returns the most recent commit (which is HEAD),
    so no fallback is ever needed for that case.  The only scenario where
    ``rev-list`` returns nothing is when *until* predates every commit — the
    correct answer is then ``None``.
    """
    r = _run(
        [
            "git",
            "-C",
            repo,
            "rev-list",
            "-1",
            f"--before={until} 23:59:59",
            "HEAD",
        ]
    )
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    # No commits exist at or before `until`.  Return None so callers skip
    # module snapshots rather than snapshotting an anachronistic HEAD state.
    return None


def get_commits_name_only(
    repo: str,
    since: str,
    until: str,
    path: Optional[str] = None,
) -> list[dict[str, object]]:
    """Return commits in the window (since, until] with their touched paths.

    Each entry: {"hash": str, "subject": str, "paths": list[str]}

    Uses ``--after``/``--before`` so the boundary at *since* is exclusive and
    the boundary at *until* is inclusive (full day).
    """
    cmd = [
        "git",
        "-C",
        repo,
        "log",
        f"--after={since}",
        f"--before={until} 23:59:59",
        "--name-only",
        "--format=COMMIT:%H\t%cs\t%an\t%s",
    ]
    if path:
        cmd += ["--", path]

    r = _run(cmd)
    if r.returncode != 0 or not r.stdout.strip():
        return []

    commits: list[dict[str, object]] = []
    current: Optional[dict[str, object]] = None

    for line in r.stdout.splitlines():
        if line.startswith("COMMIT:"):
            if current is not None:
                commits.append(current)
            rest = line[len("COMMIT:") :]
            # Format: <sha>\t<date>\t<author>\t<subject>
            # Use split with maxsplit=3 so subject may contain tabs.
            fields = rest.split("\t", 3)
            sha = fields[0].strip() if len(fields) > 0 else ""
            date = fields[1].strip() if len(fields) > 1 else ""
            author = fields[2].strip() if len(fields) > 2 else ""
            subject = fields[3].strip() if len(fields) > 3 else ""
            current = {
                "hash": sha,
                "date": date,
                "author": author,
                "subject": subject,
                "paths": [],
            }
        elif line.strip() and current is not None:
            cast_current = current
            paths_list = cast_current["paths"]
            assert isinstance(paths_list, list)
            paths_list.append(line.strip())

    if current is not None:
        commits.append(current)

    return commits


def get_shortlog_authors(
    repo: str,
    since: str,
    until: str,
    path: Optional[str] = None,
    top_n: int = 3,
) -> list[str]:
    """Return the top *top_n* contributor display names for the window.

    Uses ``git shortlog -sn`` which accepts the same date filters as git log.
    Returns an empty list if git fails or no commits exist.
    """
    cmd = [
        "git",
        "-C",
        repo,
        "shortlog",
        "-sn",
        f"--after={since}",
        f"--before={until} 23:59:59",
        "HEAD",
    ]
    if path:
        cmd += ["--", path]

    r = _run(cmd)
    if r.returncode != 0 or not r.stdout.strip():
        return []

    names: list[str] = []
    for line in r.stdout.splitlines():
        # Format: "     5\tJohn Doe"
        parts = line.strip().split("\t", 1)
        if len(parts) == 2:
            names.append(parts[1].strip())

    return names[:top_n]


def get_file_at_rev(repo: str, rev: str, path: str) -> Optional[str]:
    """Read a file's content at a specific git revision.

    Returns None if the file does not exist at that revision.
    """
    r = _run(["git", "-C", repo, "show", f"{rev}:{path}"])
    if r.returncode != 0:
        return None
    return r.stdout


def get_tree_at_rev(repo: str, rev: str, path: str) -> list[str]:
    """List all file paths under *path* at the given revision.

    Returns an empty list if the path doesn't exist or git fails.
    """
    r = _run(
        [
            "git",
            "-C",
            repo,
            "ls-tree",
            "-r",
            "--name-only",
            rev,
            "--",
            path,
        ]
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# GitHub CLI helpers
# ---------------------------------------------------------------------------


def gh_merged_prs(
    owner_repo: str,
    since: str,
    until: str,
    max_fetch: int = 120,
) -> tuple[list[dict[str, object]], Optional[str]]:
    """Fetch merged PRs from GitHub whose mergedAt date falls in (since, until].

    *owner_repo* is the ``owner/repo`` string.  Fetches up to *max_fetch* PRs
    (capped at 200) from the API, then filters to the date window.  Returns
    **all** matching PRs without further trimming — callers classify and apply
    their own per-tier caps (e.g. cap substantive, collapse routine).

    Returns a ``(prs, error)`` tuple:

    * ``(prs, None)``   — gh ran successfully; *prs* may be an empty list when
                          the window genuinely contains no merged PRs.
    * ``([], error)``   — gh exited non-zero or produced unusable output; the
                          *error* string carries a human-readable reason.
                          Callers **must** surface this loudly rather than
                          silently treating it as "zero PRs".
    """
    fetch_limit = min(max(max_fetch, 60), 200)
    cmd = [
        "gh",
        "pr",
        "list",
        "--repo",
        owner_repo,
        "--state",
        "merged",
        "--json",
        "number,title,body,mergedAt,author,files",
        "--limit",
        str(fetch_limit),
    ]
    r = _run_gh_with_retry(cmd)
    if r.returncode != 0:
        # gh FAILED — extract the first line of stderr as the reason
        raw_err = (r.stderr or r.stdout or "").strip()
        first_line = (
            raw_err.splitlines()[0][:200]
            if raw_err
            else "gh exited non-zero with no message"
        )
        return [], f"gh error: {first_line}"

    if not r.stdout.strip():
        # gh succeeded but returned nothing — genuinely zero PRs
        return [], None

    try:
        prs: list[dict[str, object]] = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        return [], f"gh error: could not parse JSON response ({exc})"

    # Filter: mergedAt date in (since, until]  — string compare works for ISO dates
    result: list[dict[str, object]] = []
    for pr in prs:
        merged = pr.get("mergedAt") or ""
        if not isinstance(merged, str) or len(merged) < 10:
            continue
        merged_date = merged[:10]  # YYYY-MM-DD
        if since < merged_date <= until:
            result.append(pr)

    return result, None


def gh_most_recent_update(
    owner_repo: str,
    kind: Literal["pr", "issue"],
) -> tuple[Optional[str], Optional[str]]:
    """Return the ``YYYY-MM-DD`` of the single most-recently-updated PR or issue.

    This is the CHEAP gating check for change-detection: GitHub bumps a PR or
    issue's own ``updatedAt`` field on *any* comment, review, or label change
    -- not only on new commits. Comparing this single date against a repo's
    last-sync watermark lets callers (:func:`repo_weaver.sync.sync_corpus`)
    detect "did discussion activity happen" with ONE lightweight ``gh`` call
    per kind (2 total per repo per sync), instead of enumerating every open
    item's full history.

    *owner_repo* is the ``owner/repo`` string. *kind* selects ``gh pr list``
    (``"pr"``) or ``gh issue list`` (``"issue"``); both support identical
    ``--search "sort:updated-desc"`` + ``--limit 1`` semantics.

    Returns a ``(date_str, error)`` tuple:

    * ``(date_str, None)`` -- gh ran successfully and found at least one
      PR/issue; *date_str* is that item's ``updatedAt`` truncated to
      ``YYYY-MM-DD``.
    * ``(None, None)``     -- gh ran successfully but the repo genuinely has
      zero PRs/issues of this kind. This is NOT an error -- a repo can
      legitimately have no issues (or no PRs) at all.
    * ``(None, error)``    -- gh exited non-zero (after retries) or produced
      unparsable output; *error* carries a human-readable reason. Callers
      **must** surface this loudly rather than silently treating it as
      "no activity".
    """
    subcommand = "pr" if kind == "pr" else "issue"
    cmd = [
        "gh",
        subcommand,
        "list",
        "--repo",
        owner_repo,
        "--state",
        "all",
        "--json",
        "updatedAt",
        "--limit",
        "1",
        "--search",
        "sort:updated-desc",
    ]
    r = _run_gh_with_retry(cmd)
    if r.returncode != 0:
        raw_err = (r.stderr or r.stdout or "").strip()
        first_line = (
            raw_err.splitlines()[0][:200]
            if raw_err
            else "gh exited non-zero with no message"
        )
        return None, f"gh error: {first_line}"

    if not r.stdout.strip():
        # gh succeeded but returned nothing -- genuinely zero PRs/issues.
        return None, None

    try:
        items: list[dict[str, object]] = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        return None, f"gh error: could not parse JSON response ({exc})"

    if not items:
        return None, None

    updated_at = items[0].get("updatedAt")
    if not isinstance(updated_at, str) or len(updated_at) < 10:
        return None, None
    return updated_at[:10], None


def gh_pr_discussion(
    owner_repo: str,
    pr_number: int,
) -> tuple[dict[str, list[dict[str, object]]], Optional[str]]:
    """Fetch a single PR's comments and reviews in one ``gh pr view`` call.

    Both ``comments`` and ``reviews`` are natively supported fields on a
    single ``gh pr view --json`` invocation -- no need for two separate
    requests. Only called for PRs already known to fall inside an
    already-determined active sync window (bounded fetch, not full history).

    Returns a ``(discussion, error)`` tuple:

    * ``(discussion, None)`` -- gh ran successfully; *discussion* is
      ``{"comments": [...], "reviews": [...]}``. Either list may genuinely
      be empty (a PR with no comments/reviews) -- that is NOT an error.
    * ``({"comments": [], "reviews": []}, error)`` -- gh exited non-zero
      (after retries) or produced unparsable output; *error* carries a
      human-readable reason. Callers **must** surface this loudly rather
      than silently treating it as "no discussion".
    """
    empty: dict[str, list[dict[str, object]]] = {"comments": [], "reviews": []}
    cmd = [
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--repo",
        owner_repo,
        "--json",
        "comments,reviews",
    ]
    r = _run_gh_with_retry(cmd)
    if r.returncode != 0:
        raw_err = (r.stderr or r.stdout or "").strip()
        first_line = (
            raw_err.splitlines()[0][:200]
            if raw_err
            else "gh exited non-zero with no message"
        )
        return empty, f"gh error: {first_line}"

    if not r.stdout.strip():
        return empty, None

    try:
        data: dict[str, object] = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        return empty, f"gh error: could not parse JSON response ({exc})"

    comments = data.get("comments")
    reviews = data.get("reviews")
    return {
        "comments": comments if isinstance(comments, list) else [],
        "reviews": reviews if isinstance(reviews, list) else [],
    }, None


def gh_issues(
    owner_repo: str,
    since: str,
    until: str,
    max_fetch: int = 120,
) -> tuple[list[dict[str, object]], Optional[str]]:
    """Fetch issues from GitHub whose ``updatedAt`` date falls in (since, until].

    Mirrors :func:`gh_merged_prs`'s date-windowing pattern: fetch up to
    *max_fetch* issues (capped at 200), sorted by most-recently-updated,
    via the GitHub search ``updated:<since>..<until>`` range, then
    re-verify the window client-side (belt-and-suspenders -- never trust
    search-syntax parsing alone to be the sole gate).

    Returns an ``(issues, error)`` tuple, mirroring :func:`gh_merged_prs`:

    * ``(issues, None)`` -- gh ran successfully; *issues* may be an empty
      list when the window genuinely contains no updated issues.
    * ``([], error)``    -- gh exited non-zero (after retries) or produced
      unparsable output; *error* carries a human-readable reason. Callers
      **must** surface this loudly rather than silently treating it as
      "zero issues".
    """
    fetch_limit = min(max(max_fetch, 60), 200)
    cmd = [
        "gh",
        "issue",
        "list",
        "--repo",
        owner_repo,
        "--state",
        "all",
        "--json",
        "number,title,body,createdAt,updatedAt,author",
        "--limit",
        str(fetch_limit),
        "--search",
        f"updated:{since}..{until} sort:updated-desc",
    ]
    r = _run_gh_with_retry(cmd)
    if r.returncode != 0:
        raw_err = (r.stderr or r.stdout or "").strip()
        first_line = (
            raw_err.splitlines()[0][:200]
            if raw_err
            else "gh exited non-zero with no message"
        )
        return [], f"gh error: {first_line}"

    if not r.stdout.strip():
        # gh succeeded but returned nothing -- genuinely zero issues.
        return [], None

    try:
        issues: list[dict[str, object]] = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        return [], f"gh error: could not parse JSON response ({exc})"

    # Belt-and-suspenders re-check: the --search window should already
    # constrain results, but re-verify against updatedAt so a search-syntax
    # edge case never silently admits an out-of-window issue.
    result: list[dict[str, object]] = []
    for issue in issues:
        updated = issue.get("updatedAt") or ""
        if not isinstance(updated, str) or len(updated) < 10:
            continue
        updated_date = updated[:10]
        if since < updated_date <= until:
            result.append(issue)

    return result, None


def gh_issue_discussion(
    owner_repo: str,
    issue_number: int,
) -> tuple[list[dict[str, object]], Optional[str]]:
    """Fetch a single issue's comments via ``gh issue view --json comments``.

    Only called for issues already known to fall inside an
    already-determined active sync window (bounded fetch, not full history).

    Returns a ``(comments, error)`` tuple:

    * ``(comments, None)`` -- gh ran successfully; *comments* may genuinely
      be an empty list (an issue with no comments) -- that is NOT an error.
    * ``([], error)``      -- gh exited non-zero (after retries) or produced
      unparsable output; *error* carries a human-readable reason. Callers
      **must** surface this loudly rather than silently treating it as
      "no comments".
    """
    cmd = [
        "gh",
        "issue",
        "view",
        str(issue_number),
        "--repo",
        owner_repo,
        "--json",
        "comments",
    ]
    r = _run_gh_with_retry(cmd)
    if r.returncode != 0:
        raw_err = (r.stderr or r.stdout or "").strip()
        first_line = (
            raw_err.splitlines()[0][:200]
            if raw_err
            else "gh exited non-zero with no message"
        )
        return [], f"gh error: {first_line}"

    if not r.stdout.strip():
        return [], None

    try:
        data: dict[str, object] = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        return [], f"gh error: could not parse JSON response ({exc})"

    comments = data.get("comments")
    return (comments if isinstance(comments, list) else []), None


def gh_repo_pushed_at(owner_repo: str) -> tuple[Optional[str], Optional[str]]:
    """Return the ISO 8601 ``pushedAt`` timestamp for a single repo.

    The single-repo equivalent of the ``pushedAt`` field :func:`gh_list_repos`
    only returns in bulk per-owner (via ``gh repo list``). Exists so a caller
    that only cares about ONE repo -- e.g. :func:`repo_weaver.sync.changed_since`,
    a pure per-repo query -- doesn't need to fetch and filter an owner's
    entire repo list just to get one push date.

    *owner_repo* is the ``owner/repo`` string.

    Returns a ``(pushed_at, error)`` tuple:

    * ``(pushed_at, None)`` -- gh ran successfully; *pushed_at* is the raw
      ISO 8601 timestamp string (e.g. ``"2026-07-08T14:03:34Z"``).
    * ``(None, error)``     -- gh exited non-zero (after retries), returned
      an empty/unparsable response, or the response was missing the
      ``pushedAt`` field; *error* carries a human-readable reason. Unlike
      list-style calls, an empty response here is NOT a legitimate "zero
      results" case (a single named repo either exists and has a push date,
      or the call failed) -- callers **must** surface this loudly rather
      than silently treating it as "never pushed".
    """
    cmd = ["gh", "repo", "view", owner_repo, "--json", "pushedAt"]
    r = _run_gh_with_retry(cmd)
    if r.returncode != 0:
        raw_err = (r.stderr or r.stdout or "").strip()
        first_line = (
            raw_err.splitlines()[0][:200]
            if raw_err
            else "gh exited non-zero with no message"
        )
        return None, f"gh error: {first_line}"

    if not r.stdout.strip():
        return None, "gh error: empty response for pushedAt"

    try:
        data: dict[str, object] = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        return None, f"gh error: could not parse JSON response ({exc})"

    pushed_at = data.get("pushedAt")
    if not isinstance(pushed_at, str) or not pushed_at:
        return None, "gh error: response missing 'pushedAt' field"
    return pushed_at, None


def gh_list_repos(
    owner: str,
    include_forks: bool = True,
    visibility: str = "all",
) -> tuple[list[dict[str, object]], Optional[str]]:
    """Return repo metadata for every non-archived repo owned by *owner*.

    Shells out to::

        gh repo list <owner> --json name,isFork,pushedAt,nameWithOwner \\
            --limit 500 --no-archived [--visibility public|private]

    Each returned dict has (at least) the keys ``name``, ``isFork``,
    ``pushedAt`` (ISO 8601 string), and ``nameWithOwner`` (``"owner/repo"``).

    Args:
        owner:         A GitHub user or org login.
        include_forks: When False, forked repos (``isFork: true``) are
                       filtered out of the result client-side. Default True
                       (matches prior behaviour: no filtering).
        visibility:    ``"public"``, ``"private"``, or ``"all"`` (default).
                       Anything other than ``"all"`` is passed through to
                       ``gh repo list --visibility`` so results are filtered
                       server-side against what the authenticated token can see.

    Returns a ``(repos, error)`` tuple, mirroring :func:`gh_merged_prs`'s shape:

    * ``(repos, None)`` -- gh ran successfully; *repos* may be an empty list
                          when the owner genuinely has zero matching repos.
    * ``([], error)``   -- gh exited non-zero (after retries) or produced
                          unparsable output; the *error* string carries a
                          human-readable reason. Callers **must** surface
                          this loudly rather than silently treating it as
                          "owner has zero repos".
    """
    cmd = [
        "gh",
        "repo",
        "list",
        owner,
        "--json",
        "name,isFork,pushedAt,nameWithOwner",
        "--limit",
        "500",
        "--no-archived",
    ]
    if visibility in ("public", "private"):
        cmd += ["--visibility", visibility]

    r = _run_gh_with_retry(cmd)
    if r.returncode != 0:
        raw_err = (r.stderr or r.stdout or "").strip()
        first_line = (
            raw_err.splitlines()[0][:200]
            if raw_err
            else "gh exited non-zero with no message"
        )
        return [], f"gh error: {first_line}"

    if not r.stdout.strip():
        # gh succeeded but returned nothing -- genuinely zero repos.
        return [], None

    try:
        repos: list[dict[str, object]] = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        return [], f"gh error: could not parse JSON response ({exc})"

    if not include_forks:
        repos = [r for r in repos if not r.get("isFork")]

    return repos, None


def gh_clone_repo(name_with_owner: str, dest: str) -> bool:
    """Clone ``owner/repo`` into *dest* via ``gh repo clone``.

    Returns True on success (exit 0), False otherwise. Side-effecting: creates
    a new local clone. Callers (e.g. :func:`repo_weaver.sync.sync_corpus`)
    should call this only when no local clone exists yet at *dest*.
    """
    r = _run_gh_with_retry(["gh", "repo", "clone", name_with_owner, dest])
    return r.returncode == 0


# ---------------------------------------------------------------------------
# Discovery mechanism (NOT policy) -- caller supplies the rule list
# ---------------------------------------------------------------------------


def discover_repos(
    rules: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[str]]:
    """Discover repos across multiple owners/rules, merged and deduplicated.

    This is a MECHANISM, not a policy: repo-weaver does not own, parse, or
    validate a discovery config file -- the caller supplies *rules* directly
    (e.g. loaded from their own JSON file, or built up in memory) each time.
    Each rule is a dict with keys:

        owner:         str  -- a GitHub user or org login (required).
        match:         str  -- a glob/prefix pattern, e.g. ``"amplifier*"``
                                (required; matched via :func:`fnmatch.fnmatch`
                                against the repo's ``name``).
        include_forks: bool -- default True.
        visibility:    str  -- ``"public"`` / ``"private"`` / ``"all"``
                                (default ``"all"``).

    For each rule, calls :func:`gh_list_repos` with that rule's
    ``include_forks``/``visibility``, then filters the result by ``match``.
    Matched repos from all rules are merged into one list, deduplicated by
    ``nameWithOwner`` (first occurrence wins).

    A failing rule (gh discovery error for that owner) does NOT abort
    discovery of the others -- mirrors :func:`repo_weaver.sync.sync_corpus`'s
    "collect failures per repo/owner, keep going" pattern. Errors are
    collected and returned alongside the matched repos.

    Returns:
        ``(matched_repos, errors)`` -- *matched_repos* is the deduplicated,
        filtered list; *errors* is a list of human-readable per-rule failure
        strings (empty when every rule's ``gh`` call succeeded).
    """
    matched: list[dict[str, object]] = []
    seen: set[str] = set()
    errors: list[str] = []

    for rule in rules:
        owner = str(rule.get("owner", ""))
        match = str(rule.get("match", "*"))
        include_forks = bool(rule.get("include_forks", True))
        visibility = str(rule.get("visibility", "all"))

        if not owner:
            errors.append("discovery rule missing required 'owner' key; skipped.")
            continue

        repos, error = gh_list_repos(
            owner, include_forks=include_forks, visibility=visibility
        )
        if error is not None:
            errors.append(f"{owner}: {error}")
            continue

        for repo in repos:
            name = repo.get("name")
            if not isinstance(name, str) or not name:
                continue
            if not fnmatch.fnmatch(name, match):
                continue
            name_with_owner_raw = repo.get("nameWithOwner")
            name_with_owner = (
                name_with_owner_raw
                if isinstance(name_with_owner_raw, str) and name_with_owner_raw
                else f"{owner}/{name}"
            )
            if name_with_owner in seen:
                continue
            seen.add(name_with_owner)
            matched.append(repo)

    return matched, errors


# ---------------------------------------------------------------------------
# Clone-staleness helpers  (Change 4: fetch-or-warn guard)
# ---------------------------------------------------------------------------


def get_default_branch(repo: str) -> str:
    """Return the name of the default branch for the ``origin`` remote.

    Tries ``git symbolic-ref --short refs/remotes/origin/HEAD`` first
    (works on any clone where the remote HEAD was set at clone time or
    via ``git remote set-head origin -a``).  Falls back to parsing
    ``git remote show origin`` (requires a network round-trip but is
    reliable).  Returns ``"main"`` if all detection methods fail.

    Does NOT mutate the repo.
    """
    r = _run(["git", "-C", repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if r.returncode == 0 and r.stdout.strip():
        # e.g. "origin/main" → "main"
        ref = r.stdout.strip()
        parts = ref.split("/", 1)
        return parts[1] if len(parts) == 2 else parts[0]

    # Slow fallback: parse the output of `git remote show origin`.
    # This contacts the remote server, so it's only reached when the
    # symbolic-ref isn't set locally.
    r2 = _run(["git", "-C", repo, "remote", "show", "origin"])
    if r2.returncode == 0:
        for line in r2.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("HEAD branch:"):
                branch = stripped.split(":", 1)[1].strip()
                if branch and branch != "(unknown)":
                    return branch

    return "main"


def fetch_origin(repo: str) -> bool:
    """Run ``git fetch origin`` against *repo*.

    Returns True if the fetch succeeded (exit 0), False otherwise.
    This is the only function in this module with a remote-network side
    effect — it updates ``refs/remotes/origin/*`` in the local clone.
    """
    r = _run(["git", "-C", repo, "fetch", "origin"])
    return r.returncode == 0


def commits_behind_origin(repo: str, branch: str) -> int:
    """Return how many commits HEAD is behind ``origin/<branch>``.

    Requires that ``refs/remotes/origin/<branch>`` exists (i.e. a prior
    ``git fetch`` has been done).  Returns 0 on any error so callers can
    treat an inability-to-check as "not stale" rather than crashing.
    """
    r = _run(
        [
            "git",
            "-C",
            repo,
            "rev-list",
            "--count",
            f"HEAD..origin/{branch}",
        ]
    )
    if r.returncode != 0 or not r.stdout.strip():
        return 0
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


def is_working_tree_clean(repo: str) -> bool:
    """Return True if the working tree has no uncommitted changes.

    Uses ``git status --porcelain``: any output means the tree is dirty.
    Returns False on git errors (conservative — treat unknown as dirty).
    """
    r = _run(["git", "-C", repo, "status", "--porcelain"])
    return r.returncode == 0 and not r.stdout.strip()


def fast_forward_origin(repo: str, branch: str) -> bool:
    """Attempt to fast-forward HEAD to ``origin/<branch>``.

    Uses ``git merge --ff-only origin/<branch>`` so it refuses to create
    a merge commit.  Returns True on success, False if the fast-forward
    was not possible or git returned non-zero.
    """
    r = _run(["git", "-C", repo, "merge", "--ff-only", f"origin/{branch}"])
    return r.returncode == 0
