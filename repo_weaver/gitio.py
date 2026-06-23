"""Read-only git and gh subprocess helpers.

All functions are side-effect free with respect to the target repo: they only
read (log, show, ls-tree, shortlog, rev-list, remote get-url) and never write
to the working tree or index.
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

    Falls back to HEAD if rev-list returns nothing (e.g. until is in the future).
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

    # Fallback: use HEAD
    r2 = _run(["git", "-C", repo, "rev-parse", "HEAD"])
    if r2.returncode == 0 and r2.stdout.strip():
        return r2.stdout.strip()
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
        "--format=COMMIT:%H\t%s",
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
            sha, _, subject = rest.partition("\t")
            current = {"hash": sha.strip(), "subject": subject.strip(), "paths": []}
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
) -> list[dict[str, object]]:
    """Fetch merged PRs from GitHub whose mergedAt date falls in (since, until].

    *owner_repo* is the ``owner/repo`` string.  Fetches up to *max_fetch* PRs
    (capped at 200) from the API, then filters to the date window.  Returns
    **all** matching PRs without further trimming — callers classify and apply
    their own per-tier caps (e.g. cap substantive, collapse routine).
    Returns an empty list on any gh error.
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
    if r.returncode != 0 or not r.stdout.strip():
        return []

    try:
        prs: list[dict[str, object]] = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []

    # Filter: mergedAt date in (since, until]  — string compare works for ISO dates
    result: list[dict[str, object]] = []
    for pr in prs:
        merged = pr.get("mergedAt") or ""
        if not isinstance(merged, str) or len(merged) < 10:
            continue
        merged_date = merged[:10]  # YYYY-MM-DD
        if since < merged_date <= until:
            result.append(pr)

    return result
