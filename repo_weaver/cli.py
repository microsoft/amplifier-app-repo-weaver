"""Command-line interface for repo-weaver.

Entry point: ``main()`` — registered as the ``repo-weaver`` console script.
Each subcommand is a plain function that returns an integer exit code.

Usage:
    repo-weaver doctor
    repo-weaver init <corpus_dir> [--repo PATH]
    repo-weaver weave --corpus DIR [options]
    repo-weaver ask "<question>" --corpus DIR [--json]
    repo-weaver replay --corpus DIR --windows "D1,D2,..." [options]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from . import gitio
from . import weave as weave_mod
from .weave import _DEFAULT_MAX_CYCLES, _DEFAULT_MAX_RETRIES

# Policy schema shipped with repo-weaver.  Stored inside the package at
# repo_weaver/policy/schema.md so it is included in both editable installs
# and wheel installs (uv tool install) without any extra configuration.
_POLICY_SCHEMA = Path(__file__).parent / "policy" / "schema.md"

# Filename stored inside each corpus to record the repo path and origin URL.
_CORPUS_CONFIG = ".repo-weaver.json"


def _load_corpus_config(corpus: str) -> dict[str, object]:
    cfg_path = Path(corpus) / _CORPUS_CONFIG
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))  # type: ignore[return-value]
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_corpus_config(corpus: str, cfg: dict[str, object]) -> None:
    cfg_path = Path(corpus) / _CORPUS_CONFIG
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _load_corpus_repos(corpus: str) -> list[str]:
    """Return the list of repo paths from the corpus config.

    Handles both the new format (``"repos": [...]``) and the old single-repo
    format (``"repo": "..."``), so corpora initialised before multi-repo
    support was added continue to work without migration.
    """
    cfg = _load_corpus_config(corpus)
    repos_val = cfg.get("repos")
    if isinstance(repos_val, list):
        return [str(r) for r in repos_val if r]
    # Backward-compat: old config stored a single path under "repo"
    repo_val = cfg.get("repo")
    if isinstance(repo_val, str) and repo_val:
        return [repo_val]
    return []


# ---------------------------------------------------------------------------
# Subcommand: doctor
# ---------------------------------------------------------------------------


def _check_tool(name: str, version_cmd: Optional[list[str]] = None) -> tuple[bool, str]:
    """Return (ok, detail) for a tool dependency."""
    if shutil.which(name) is None:
        return False, "not found on PATH"
    if version_cmd:
        r = subprocess.run(version_cmd, capture_output=True, text=True)
        if r.returncode != 0:
            stderr = (r.stderr or r.stdout or "").strip()
            return False, f"found but reported an error: {stderr[:80]}"
        ver = (r.stdout or "").strip().splitlines()[0] if r.stdout.strip() else "(ok)"
        return True, ver
    return True, "found"


def cmd_doctor(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Print a dependency status table and exit 1 if anything is missing."""
    rows: list[tuple[str, bool, str]] = []

    # wiki-weaver
    ok, detail = _check_tool("wiki-weaver", ["wiki-weaver", "--version"])
    rows.append(("wiki-weaver", ok, detail))

    # git
    ok, detail = _check_tool("git", ["git", "--version"])
    rows.append(("git", ok, detail))

    # gh binary + auth check
    if shutil.which("gh") is None:
        rows.append(("gh", False, "not found on PATH"))
    else:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip().splitlines()
            short = msg[0][:80] if msg else "auth check failed"
            rows.append(("gh", False, short))
        else:
            rows.append(("gh", True, "authenticated"))

    # GOOGLE_API_KEY — primary key used by wiki-weaver ingest/ask in this environment.
    # ANTHROPIC_API_KEY is accepted as an alternative by some wiki-weaver backends.
    google_key = os.environ.get("GOOGLE_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if google_key:
        rows.append(
            ("GOOGLE_API_KEY", True, "set (primary — used by wiki-weaver ingest)")
        )
    elif anthropic_key:
        rows.append(
            (
                "GOOGLE_API_KEY",
                False,
                "not set; ANTHROPIC_API_KEY is set (alternative — check wiki-weaver backend)",
            )
        )
    else:
        rows.append(
            (
                "GOOGLE_API_KEY",
                False,
                "not set — required for wiki-weaver ingest/ask (set GOOGLE_API_KEY or ANTHROPIC_API_KEY)",
            )
        )

    # policy/schema.md — packaged inside repo_weaver/policy/
    if _POLICY_SCHEMA.exists():
        rows.append(("policy/schema.md", True, str(_POLICY_SCHEMA)))
    else:
        rows.append(("policy/schema.md", False, "not found (reinstall repo-weaver)"))

    # ---- Print table ----
    col_w = max(len(r[0]) for r in rows) + 2
    print(f"\n{'Dependency':<{col_w}}  {'Status':<6}  Detail")
    print("-" * 72)
    all_ok = True
    for name, ok, detail in rows:
        sym = "\u2713" if ok else "\u2717"
        label = "OK  " if ok else "FAIL"
        print(f"{name:<{col_w}}  {sym} {label}  {detail}")
        if not ok:
            all_ok = False
    print()

    if all_ok:
        print("All checks passed.")
        return 0

    print("Some checks failed.  Install hints:")
    print("  wiki-weaver    : pip install wiki-weaver  OR  uv tool install wiki-weaver")
    print("  gh             : https://cli.github.com/")
    print("  GOOGLE_API_KEY : export GOOGLE_API_KEY=<your-key>")
    return 1


# ---------------------------------------------------------------------------
# Subcommand: init
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Scaffold a corpus directory and install the code-fit schema."""
    corpus = args.corpus_dir
    # args.repo is list[str] | None  (action="append"; None when flag is absent)
    repo_args: Optional[list[str]] = getattr(args, "repo", None)

    # 1. Scaffold via wiki-weaver
    print(f"[repo-weaver] Initialising wiki at {corpus} ...")
    r = subprocess.run(["wiki-weaver", "init", corpus, "--plain"])
    if r.returncode != 0:
        print(
            f"ERROR: wiki-weaver init failed (exit {r.returncode})",
            file=sys.stderr,
        )
        return r.returncode

    # 2. Install code-fit schema — REQUIRED: a corpus without a schema is broken.
    if not _POLICY_SCHEMA.exists():
        print(
            "ERROR: policy/schema.md not found in the repo-weaver package.\n"
            "This is required for wiki-weaver to understand the corpus structure.\n"
            "Reinstall repo-weaver:  pip install --force-reinstall repo-weaver\n"
            "                   OR:  uv tool install --reinstall repo-weaver",
            file=sys.stderr,
        )
        return 1

    policy_dst = Path(corpus) / "policy"
    policy_dst.mkdir(parents=True, exist_ok=True)
    schema_dst = policy_dst / "schema.md"
    shutil.copy2(_POLICY_SCHEMA, schema_dst)
    print(f"[repo-weaver] Installed schema: {schema_dst}")

    # 3. Save corpus config (list of repo absolute paths)
    cfg: dict[str, object] = {}
    if repo_args:
        repo_paths = [str(Path(rp).resolve()) for rp in repo_args]
        cfg["repos"] = repo_paths
        for rp in repo_paths:
            origin = gitio.get_origin_url(rp)
            label = f" ({origin})" if origin else ""
            print(f"[repo-weaver] Registered repo: {rp}{label}")

    _save_corpus_config(corpus, cfg)
    print(f"[repo-weaver] Corpus config: {Path(corpus) / _CORPUS_CONFIG}")
    print("[repo-weaver] Done.  Run `repo-weaver weave --corpus <dir>` to populate.")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: weave
# ---------------------------------------------------------------------------


def cmd_weave(args: argparse.Namespace) -> int:
    """Materialise sources and (unless --dry-run) run wiki-weaver ingest."""
    corpus = args.corpus
    repo_override: Optional[str] = getattr(args, "repo", None)
    classify: bool = not getattr(args, "no_classify", False)

    if repo_override:
        # Explicit --repo override: single-repo path, unqualified filenames (historic behaviour).
        return weave_mod.weave(
            corpus=corpus,
            repo=repo_override,
            since=args.since,
            until=args.until,
            max_prs=args.max_prs,
            max_modules=args.max_modules,
            dry_run=args.dry_run,
            max_cycles=args.max_cycles,
            max_retries=args.max_retries,
            classify=classify,
        )

    # No override: weave all repos recorded in the corpus config.
    repos = _load_corpus_repos(corpus)
    if not repos:
        print(
            "ERROR: no repos configured. "
            "Use `repo-weaver init --repo PATH` to record repo(s), or pass --repo to override.",
            file=sys.stderr,
        )
        return 1

    return weave_mod.weave_multi(
        corpus=corpus,
        repos=repos,
        since=args.since,
        until=args.until,
        max_prs=args.max_prs,
        max_modules=args.max_modules,
        dry_run=args.dry_run,
        max_cycles=args.max_cycles,
        max_retries=args.max_retries,
        classify=classify,
    )


# ---------------------------------------------------------------------------
# Subcommand: ask
# ---------------------------------------------------------------------------


def cmd_ask(args: argparse.Namespace) -> int:
    """Pass-through to wiki-weaver ask."""
    cmd = ["wiki-weaver", "ask", args.question, "--wiki", args.corpus]
    if args.json:
        cmd.append("--json")
    r = subprocess.run(cmd)
    return r.returncode


# ---------------------------------------------------------------------------
# Subcommand: replay
# ---------------------------------------------------------------------------


def cmd_replay(args: argparse.Namespace) -> int:
    """Weave successive non-overlapping windows for an over-time corpus build."""
    corpus = args.corpus
    repo_override: Optional[str] = getattr(args, "repo", None)

    if repo_override:
        repos = [repo_override]
    else:
        repos = _load_corpus_repos(corpus)
        if not repos:
            print(
                "ERROR: no repos configured. "
                "Use `repo-weaver init --repo PATH` to record repo(s), or pass --repo.",
                file=sys.stderr,
            )
            return 1

    raw_cutoffs = [w.strip() for w in args.windows.split(",") if w.strip()]
    if not raw_cutoffs:
        print(
            "ERROR: --windows must be a non-empty comma-separated list of YYYY-MM-DD dates.",
            file=sys.stderr,
        )
        return 1

    # Validate date format
    for d in raw_cutoffs:
        try:
            date.fromisoformat(d)
        except ValueError:
            print(f"ERROR: invalid date in --windows: {d!r}", file=sys.stderr)
            return 1

    # Determine start: one day before the earliest first commit across all repos.
    first_dates: list[str] = []
    for r in repos:
        first = gitio.get_first_commit_date(r)
        if first:
            first_dates.append(first)

    if first_dates:
        start = (date.fromisoformat(min(first_dates)) - timedelta(days=1)).isoformat()
    else:
        start = "2000-01-01"

    # Build ordered list of (since, until) pairs.
    windows = [(start, raw_cutoffs[0])]
    for i in range(len(raw_cutoffs) - 1):
        windows.append((raw_cutoffs[i], raw_cutoffs[i + 1]))

    return weave_mod.replay_windows(
        corpus=corpus,
        repos=repos,
        windows=windows,
        max_prs=getattr(args, "max_prs", 15),
        max_modules=getattr(args, "max_modules", 5),
        max_cycles=getattr(args, "max_cycles", _DEFAULT_MAX_CYCLES),
        max_retries=getattr(args, "max_retries", _DEFAULT_MAX_RETRIES),
        classify=not getattr(args, "no_classify", False),
        restart=getattr(args, "restart", False),
    )


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repo-weaver",
        description=(
            "Turn a git repo's commits and PRs into a queryable wiki corpus via wiki-weaver."
        ),
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")

    sub = parser.add_subparsers(dest="command", required=True)

    # ---- doctor ----
    p = sub.add_parser(
        "doctor", help="Check all dependencies and exit 1 if any are missing."
    )
    p.set_defaults(func=cmd_doctor)

    # ---- init ----
    p = sub.add_parser("init", help="Scaffold a new corpus directory.")
    p.add_argument("corpus_dir", help="Directory to create the corpus in.")
    p.add_argument(
        "--repo",
        metavar="PATH",
        action="append",
        help=(
            "Path to a local git repo to include in this corpus. "
            "Repeatable: --repo A --repo B tracks multiple repos. "
            "Omit to create a repo-less corpus and supply --repo at weave time."
        ),
    )
    p.set_defaults(func=cmd_init)

    # ---- weave ----
    p = sub.add_parser(
        "weave",
        help="Materialise source docs and ingest into the corpus.",
    )
    p.add_argument("--corpus", required=True, metavar="DIR", help="Corpus directory.")
    p.add_argument(
        "--repo",
        metavar="PATH",
        help="Git repo path (overrides the path recorded during init).",
    )
    p.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="Window start (exclusive). Default: one day before the repo's first commit.",
    )
    p.add_argument(
        "--until",
        metavar="YYYY-MM-DD",
        help="Window end (inclusive, up to 23:59:59). Default: today.",
    )
    p.add_argument(
        "--max-prs",
        type=int,
        default=15,
        metavar="N",
        help="Max merged PRs to include in the change digest (default: 15).",
    )
    p.add_argument(
        "--max-modules",
        type=int,
        default=5,
        metavar="N",
        help="Max module snapshot documents to emit (default: 5).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Write _inbox files but skip wiki-weaver ingest.",
    )
    p.add_argument(
        "--no-classify",
        action="store_true",
        default=False,
        help=(
            "Disable PR classification: list ALL merged PRs with full detail "
            "instead of splitting into substantive/routine tiers. "
            "Use for A/B testing whether classification improves the corpus."
        ),
    )
    p.add_argument(
        "--max-cycles",
        type=int,
        default=_DEFAULT_MAX_CYCLES,
        metavar="N",
        help=(
            f"Digest cycle budget passed to wiki-weaver ingest (default: {_DEFAULT_MAX_CYCLES}). "
            "Increase for dense repos that do not converge in the default budget."
        ),
    )
    p.add_argument(
        "--max-retries",
        type=int,
        default=_DEFAULT_MAX_RETRIES,
        metavar="N",
        help=(
            f"Max per-source retry attempts after a _failed/ event (default: {_DEFAULT_MAX_RETRIES}). "
            "Each transient-error retry applies exponential back-off; "
            "each not-converged retry increases --max-cycles."
        ),
    )
    p.set_defaults(func=cmd_weave)

    # ---- ask ----
    p = sub.add_parser(
        "ask", help="Ask a question against the corpus (via wiki-weaver ask)."
    )
    p.add_argument("question", help="The question to answer.")
    p.add_argument("--corpus", required=True, metavar="DIR", help="Corpus directory.")
    p.add_argument(
        "--json",
        action="store_true",
        help="Output JSON: {answer, pages_used, refused}.",
    )
    p.set_defaults(func=cmd_ask)

    # ---- replay ----
    p = sub.add_parser(
        "replay",
        help="Weave successive time windows for an over-time corpus build.",
    )
    p.add_argument("--corpus", required=True, metavar="DIR", help="Corpus directory.")
    p.add_argument(
        "--repo",
        metavar="PATH",
        help="Git repo path (overrides the path recorded during init).",
    )
    p.add_argument(
        "--windows",
        required=True,
        metavar="DATES",
        help=(
            "Comma-separated YYYY-MM-DD cutoff dates (ascending). "
            "Weaves windows (start, d1], (d1, d2], ..."
        ),
    )
    p.add_argument("--max-prs", type=int, default=15, metavar="N")
    p.add_argument("--max-modules", type=int, default=5, metavar="N")
    p.add_argument(
        "--max-cycles",
        type=int,
        default=_DEFAULT_MAX_CYCLES,
        metavar="N",
        help=f"Digest cycle budget per window (default: {_DEFAULT_MAX_CYCLES}).",
    )
    p.add_argument(
        "--max-retries",
        type=int,
        default=_DEFAULT_MAX_RETRIES,
        metavar="N",
        help=f"Max per-source retry attempts per window (default: {_DEFAULT_MAX_RETRIES}).",
    )
    p.add_argument(
        "--no-classify",
        action="store_true",
        default=False,
        help=(
            "Disable PR classification: list ALL merged PRs with full detail "
            "instead of splitting into substantive/routine tiers. "
            "Use for A/B testing whether classification improves the corpus."
        ),
    )
    p.add_argument(
        "--restart",
        action="store_true",
        default=False,
        help=(
            "Ignore and clear any existing replay progress, forcing a full redo "
            "from the first window. Without this flag a re-run skips windows that "
            "already completed and resumes at the first incomplete one."
        ),
    )
    p.set_defaults(func=cmd_replay)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Console script entry point.  Calls sys.exit() with the command's return code."""
    parser = _build_parser()
    args = parser.parse_args()

    # Startup check: all commands except doctor require wiki-weaver on PATH.
    if args.command != "doctor":
        if shutil.which("wiki-weaver") is None:
            print(
                "ERROR: wiki-weaver not found on PATH.\n"
                "Install with:  pip install wiki-weaver\n"
                "           OR  uv tool install wiki-weaver",
                file=sys.stderr,
            )
            sys.exit(1)

    rc = args.func(args)
    sys.exit(rc if isinstance(rc, int) else 0)


if __name__ == "__main__":
    main()
