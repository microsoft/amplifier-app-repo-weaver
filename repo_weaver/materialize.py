"""Window → list of (filename, markdown_with_frontmatter) source documents.

Produces a small, high-signal set per window:

1. ONE ``<until>-changes.md`` — merged PRs + commit-volume digest.
2. UP TO ``max_modules`` ``module-<slug>.md`` files — snapshots of the
   top-level code directories most changed in the window.

Never fabricates provenance.  All data comes from git plumbing or the gh CLI.
"""

from __future__ import annotations

import textwrap
from typing import Optional

from . import gitio

# Maximum number of files shown in a module inventory before truncating.
_MAX_INVENTORY_FILES = 60

# README filename candidates tried in order.
_README_CANDIDATES = [
    "README.md",
    "readme.md",
    "README.rst",
    "Readme.md",
    "docs/README.md",
]

# Maximum body chars shown per PR before truncating.
_PR_BODY_MAX = 600


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def materialize(
    repo: str,
    since: str,
    until: str,
    max_prs: int = 15,
    max_modules: int = 5,
) -> list[tuple[str, str]]:
    """Materialize source documents for the window (since, until].

    Args:
        repo:        Absolute path to the local git repository.
        since:       Window start date YYYY-MM-DD (exclusive — commits *after*
                     this date are included).
        until:       Window end date YYYY-MM-DD (inclusive, up to 23:59:59).
        max_prs:     Maximum merged PRs to include in the change digest.
        max_modules: Maximum module snapshot documents to emit.

    Returns:
        List of ``(filename, content)`` pairs ready to write into ``_inbox/``.
        Filenames are date-prefixed slugs as recommended by wiki-weaver.
    """
    origin_url = gitio.get_origin_url(repo)
    owner_repo: Optional[tuple[str, str]] = None
    if origin_url:
        owner_repo = gitio.parse_owner_repo(origin_url)

    until_rev = gitio.get_window_rev(repo, until)
    commits = gitio.get_commits_name_only(repo, since, until)

    docs: list[tuple[str, str]] = []

    # 1. Change digest
    digest_content = _build_change_digest(
        repo, since, until, until_rev, commits, owner_repo, max_prs
    )
    docs.append((f"{until}-changes.md", digest_content))

    # 2. Module snapshots
    module_docs = _build_module_snapshots(
        repo, since, until, until_rev, commits, owner_repo, max_modules
    )
    docs.extend(module_docs)

    return docs


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------


def _frontmatter(author: str, source: str, date_str: str) -> str:
    """Emit YAML frontmatter block with only the fields that have values."""
    lines = ["---"]
    if author:
        lines.append(f"author: {author}")
    if source:
        lines.append(f"source: {source}")
    if date_str:
        lines.append(f"date: {date_str}")
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Change digest
# ---------------------------------------------------------------------------


def _build_change_digest(
    repo: str,
    since: str,
    until: str,
    until_rev: Optional[str],
    commits: list[dict[str, object]],
    owner_repo: Optional[tuple[str, str]],
    max_prs: int,
) -> str:
    """Build the ``<until>-changes.md`` source document."""
    parts: list[str] = []
    parts.append(f"# Changes: {since} \u2192 {until}\n\n")

    # ---- Merged PRs section ----
    prs: list[dict[str, object]] = []
    if owner_repo:
        owner, name = owner_repo
        prs = gitio.gh_merged_prs(f"{owner}/{name}", since, until, max_prs)

    if prs:
        parts.append("## Merged Pull Requests\n\n")
        for pr in prs:
            n = pr.get("number", "?")
            title = pr.get("title") or "(no title)"
            raw_body = pr.get("body") or ""
            assert isinstance(raw_body, str)
            body_excerpt = (
                textwrap.shorten(
                    raw_body.strip(), width=_PR_BODY_MAX, placeholder="\u2026"
                )
                if raw_body.strip()
                else ""
            )
            author_info = pr.get("author") or {}
            if isinstance(author_info, dict):
                author_name = author_info.get("login") or "unknown"
            else:
                author_name = str(author_info)
            merged_at = str(pr.get("mergedAt") or "")[:10]
            files_list = pr.get("files") or []
            assert isinstance(files_list, list)
            file_count = len(files_list)
            top_paths = [
                str(f.get("path", "")) for f in files_list[:5] if isinstance(f, dict)
            ]

            parts.append(f"### PR #{n}: {title}\n\n")
            parts.append(f"- **Author:** {author_name}\n")
            parts.append(f"- **Merged:** {merged_at}\n")
            parts.append(f"- **Files changed:** {file_count}\n")
            if top_paths:
                parts.append(f"- **Key paths:** {', '.join(top_paths)}\n")
            if body_excerpt:
                parts.append(f"\n{body_excerpt}\n")
            parts.append("\n")
    else:
        parts.append(
            "## Merged Pull Requests\n\n"
            "_(None found in this window, or gh CLI unavailable.)_\n\n"
        )

    # ---- Commit-volume summary ----
    dir_counts: dict[str, int] = {}
    for commit in commits:
        paths_obj = commit.get("paths", [])
        assert isinstance(paths_obj, list)
        seen_dirs: set[str] = set()
        for p in paths_obj:
            assert isinstance(p, str)
            parts_path = p.split("/")
            top_dir = parts_path[0] if len(parts_path) > 1 else "(root)"
            if top_dir not in seen_dirs:
                dir_counts[top_dir] = dir_counts.get(top_dir, 0) + 1
                seen_dirs.add(top_dir)

    total_commits = len(commits)
    parts.append(
        f"## Commit Volume Summary ({since} \u2192 {until})\n\n"
        f"Total commits in window: {total_commits}\n\n"
    )
    if dir_counts:
        parts.append("Commits by top-level directory:\n\n")
        for d, count in sorted(dir_counts.items(), key=lambda x: -x[1]):
            parts.append(f"- `{d}`: {count} commit(s)\n")
        parts.append("\n")

    # ---- Frontmatter ----
    authors = gitio.get_shortlog_authors(repo, since, until, top_n=3)
    author_str = ", ".join(authors)
    source_url = ""
    if owner_repo:
        owner, name = owner_repo
        source_url = f"https://github.com/{owner}/{name}/pulls?q=is:pr+is:merged"

    fm = _frontmatter(author_str, source_url, until)
    return fm + "\n\n" + "".join(parts)


# ---------------------------------------------------------------------------
# Module snapshots
# ---------------------------------------------------------------------------


def _slug(name: str) -> str:
    """Convert a directory name to a filesystem-safe slug."""
    return name.lower().replace("/", "-").replace("_", "-").replace(" ", "-").strip("-")


def _rank_modules(commits: list[dict[str, object]]) -> list[tuple[str, int]]:
    """Rank top-level directories by the number of commits that touched them.

    Root-level files (no directory component) are excluded since they are
    typically tooling files (pyproject.toml, README.md, etc.) rather than
    substantive code modules.
    """
    dir_counts: dict[str, int] = {}
    for commit in commits:
        paths_obj = commit.get("paths", [])
        assert isinstance(paths_obj, list)
        seen: set[str] = set()
        for p in paths_obj:
            assert isinstance(p, str)
            parts = p.split("/")
            if len(parts) < 2:
                continue  # root-level file — skip
            top_dir = parts[0]
            if top_dir not in seen:
                dir_counts[top_dir] = dir_counts.get(top_dir, 0) + 1
                seen.add(top_dir)
    return sorted(dir_counts.items(), key=lambda x: -x[1])


def _build_module_snapshots(
    repo: str,
    since: str,
    until: str,
    until_rev: Optional[str],
    commits: list[dict[str, object]],
    owner_repo: Optional[tuple[str, str]],
    max_modules: int,
) -> list[tuple[str, str]]:
    ranked = _rank_modules(commits)[:max_modules]
    results: list[tuple[str, str]] = []
    for module_path, commit_count in ranked:
        content = _build_module_doc(
            repo,
            since,
            until,
            until_rev,
            module_path,
            commit_count,
            commits,
            owner_repo,
        )
        if content:
            filename = f"module-{_slug(module_path)}-{until}.md"
            results.append((filename, content))
    return results


def _extract_readme_purpose(content: str) -> str:
    """Extract the first prose paragraph from a README, skipping the title."""
    lines = content.splitlines()
    para: list[str] = []
    past_title = False

    for line in lines:
        stripped = line.strip()
        if not past_title:
            if stripped.startswith("#"):
                past_title = True
            elif stripped:
                # No title found; treat first line as content
                past_title = True
                para.append(stripped)
            continue
        # After the title
        if not stripped:
            if para:
                break  # end of first paragraph
        else:
            # Skip lines that look like badges, shields, HTML tags
            if stripped.startswith("[![") or stripped.startswith("<"):
                continue
            para.append(stripped)

    result = " ".join(para).strip()
    if not result:
        return ""
    return textwrap.shorten(result, width=500, placeholder="\u2026")


def _build_module_doc(
    repo: str,
    since: str,
    until: str,
    until_rev: Optional[str],
    module_path: str,
    commit_count: int,
    all_commits: list[dict[str, object]],
    owner_repo: Optional[tuple[str, str]],
) -> Optional[str]:
    """Build a single module snapshot document."""
    parts: list[str] = []
    parts.append(f"# Module: {module_path}\n\n")

    # ---- Purpose ----
    purpose: Optional[str] = None
    if until_rev:
        for readme_name in _README_CANDIDATES:
            raw = gitio.get_file_at_rev(repo, until_rev, f"{module_path}/{readme_name}")
            if raw:
                purpose = _extract_readme_purpose(raw)
                break

    if purpose:
        parts.append(f"## Purpose\n\n{purpose}\n\n")
    else:
        # Derive purpose from the directory name — mark clearly as inferred
        inferred = (
            f"_(Inferred — no README found for `{module_path}`.)_ "
            f"The `{module_path}` directory contains source files for this "
            f"area of the repository."
        )
        parts.append(f"## Purpose\n\n{inferred}\n\n")

    # ---- File inventory ----
    if until_rev:
        files = gitio.get_tree_at_rev(repo, until_rev, module_path)
        if files:
            rev_short = until_rev[:8]
            parts.append(f"## File Inventory (at {rev_short})\n\n")
            shown = files[:_MAX_INVENTORY_FILES]
            for f in shown:
                parts.append(f"- `{f}`\n")
            if len(files) > _MAX_INVENTORY_FILES:
                extra = len(files) - _MAX_INVENTORY_FILES
                parts.append(f"- _(\u2026 and {extra} more files)_\n")
            parts.append("\n")

    # ---- What changed this window ----
    touching: list[dict[str, object]] = []
    prefix = module_path + "/"
    for c in all_commits:
        paths_obj = c.get("paths", [])
        assert isinstance(paths_obj, list)
        if any(
            (isinstance(p, str) and (p.startswith(prefix) or p == module_path))
            for p in paths_obj
        ):
            touching.append(c)

    parts.append(f"## Changes This Window ({since} \u2192 {until})\n\n")
    parts.append(f"{commit_count} commit(s) touched this module in this window.\n\n")
    if touching:
        for c in touching[:12]:
            h = str(c.get("hash", ""))[:8]
            subj = str(c.get("subject", ""))
            parts.append(f"- `{h}` {subj}\n")
        if len(touching) > 12:
            parts.append(f"- _(\u2026 and {len(touching) - 12} more)_\n")
    parts.append("\n")

    # ---- Frontmatter ----
    authors = gitio.get_shortlog_authors(repo, since, until, path=module_path, top_n=3)
    author_str = ", ".join(authors)
    source_url = ""
    if owner_repo and until_rev:
        owner, name = owner_repo
        rev_short = until_rev[:8]
        source_url = f"https://github.com/{owner}/{name}/tree/{rev_short}/{module_path}"

    fm = _frontmatter(author_str, source_url, until)
    return fm + "\n\n" + "".join(parts)
