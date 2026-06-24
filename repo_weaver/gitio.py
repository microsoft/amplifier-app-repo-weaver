"""Git and gh subprocess helpers.

Most functions are read-only (log, show, ls-tree, shortlog, rev-list,
remote get-url) and do not modify the working tree.

The staleness-check helpers — :func:`fetch_origin`, :func:`fast_forward_origin`
— intentionally have side effects on the clone (fetch updates remote refs;
fast-forward advances HEAD).  They are called only when the caller explicitly
requests a freshness check (i.e. when ``--no-fetch`` is not set).
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Optional


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
    r = _run(cmd)
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
