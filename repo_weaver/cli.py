"""Command-line interface for repo-weaver.

Entry point: ``main()`` — registered as the ``repo-weaver`` console script.
Each subcommand is a plain function that returns an integer exit code.

Usage:
    repo-weaver doctor
    repo-weaver init <corpus_dir> [--repo PATH]
    repo-weaver weave --corpus DIR [options]
    repo-weaver ask "<question>" --corpus DIR [--json]
    repo-weaver replay --corpus DIR --windows "D1,D2,..." [options]
    repo-weaver sync --corpus DIR [options] [--json]
    repo-weaver discover --rules-file PATH [--json]
    repo-weaver build-dashboard <corpus> --out PATH [--theme PATH]
    repo-weaver update [--check]
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from wiki_weaver.lib import wiki_dashboard

from . import gitio, updater
from ._version import __version__
from ._version_resolve import resolve_version
from .sync import sync_corpus
from .weave import _DEFAULT_MAX_CYCLES, _DEFAULT_MAX_RETRIES, _POLICY_SCHEMA

# Default location for repo clones ensured/used by `repo-weaver sync`.
_DEFAULT_SYNC_CLONES_DIR = "~/dev/amplifier-corpus-clones"

# Default theme shipped with repo-weaver — written to a corpus's
# .wiki/dashboard/theme.json on first build-dashboard run (idempotent).
_DEFAULT_THEME: Path = Path(__file__).parent / "themes" / "default.json"

# GitHub group-link template: the ONLY repo-weaver-specific policy injected
# into the generic wiki-weaver build-dashboard call.
_GITHUB_GROUP_LINK_TEMPLATE = "https://github.com/{group}"

# repo_weaver/__init__.py exports a public function also named ``weave``, which
# shadows the submodule when accessed as a package attribute.  Both of these
# idiomatic forms bind weave_mod to the *function*, not the module:
#   from . import weave as weave_mod          # IMPORT_FROM on parent → function
#   import repo_weaver.weave as weave_mod     # same IMPORT_FROM bytecode → function
# importlib.import_module bypasses attribute lookup and returns sys.modules directly,
# so we always get the module regardless of __init__.py exports.
weave_mod = importlib.import_module("repo_weaver.weave")


def _load_corpus_repos(corpus: str) -> list[str]:
    """Return the list of repo paths from the corpus config.

    Handles both the new format (``"repos": [...]``) and the old single-repo
    format (``"repo": "..."``), so corpora initialised before multi-repo
    support was added continue to work without migration.
    """
    cfg = weave_mod._load_corpus_config(corpus)
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

    # LLM provider API keys.
    # wiki-weaver defaults to anthropic (PROVIDER="anthropic", MODEL="claude-sonnet-4-6").
    # PASS if AT LEAST ONE of the three supported keys is present; each is shown
    # individually so the operator can see exactly which providers are configured.
    # The gate FAILS only when NONE of them are set.
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    google_key = os.environ.get("GOOGLE_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    any_provider_key = bool(anthropic_key or google_key or openai_key)

    rows.append(
        (
            "ANTHROPIC_API_KEY",
            bool(anthropic_key),
            "set (wiki-weaver default provider)" if anthropic_key else "not set",
        )
    )
    rows.append(
        (
            "GOOGLE_API_KEY",
            bool(google_key),
            "set" if google_key else "not set",
        )
    )
    rows.append(
        (
            "OPENAI_API_KEY",
            bool(openai_key),
            "set" if openai_key else "not set",
        )
    )

    # policy/schema.md — packaged inside repo_weaver/policy/
    if _POLICY_SCHEMA.exists():
        rows.append(("policy/schema.md", True, str(_POLICY_SCHEMA)))
    else:
        rows.append(("policy/schema.md", False, "not found (reinstall repo-weaver)"))

    # ---- Print table ----
    # Key names that use the combined provider gate rather than individual gates.
    _PROVIDER_KEYS = {"ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"}

    col_w = max(len(r[0]) for r in rows) + 2
    print(f"\n{'Dependency':<{col_w}}  {'Status':<6}  Detail")
    print("-" * 72)
    all_ok = True
    for name, ok, detail in rows:
        sym = "\u2713" if ok else "\u2717"
        label = "OK  " if ok else "FAIL"
        print(f"{name:<{col_w}}  {sym} {label}  {detail}")
        # Provider key rows: only fail the gate when NONE of the three are set.
        if name in _PROVIDER_KEYS:
            if not any_provider_key:
                all_ok = False
        elif not ok:
            all_ok = False
    print()

    # ---- Resolved @main commits (local only -- run `repo-weaver update
    # --check` to compare remote) ----
    print(
        "Resolved @main commits (local \u2014 run `repo-weaver update --check` "
        "to compare remote):"
    )
    for rec in updater.installed_commit_records():
        print(f"  {rec.label:<44s} {rec.local_short}")

    # ---- Drift check: repo-weaver's bundled wiki-weaver vs the CLI on PATH ----
    # These are TWO different copies of wiki-weaver -- the @main-pinned wheel
    # dependency baked into repo-weaver's own venv, and the separately
    # installed `wiki-weaver` CLI repo-weaver shells out to. They can drift
    # from each other even when each is individually current relative to its
    # own @main -- a distinct failure mode ordinary staleness checks miss.
    drift = updater.check_wiki_weaver_drift()
    bundled_label = "wiki-weaver (bundled dependency)"
    cli_label = "wiki-weaver (CLI on PATH)"
    bundled_short = (
        (drift.bundled_commit or "")[:8] or "(not cached)"
        if drift.bundled_commit is not None
        else "(not cached)"
    )
    print(f"  {bundled_label:<44s} {bundled_short}")
    if drift.cli_commit:
        cli_short = drift.cli_commit[:8]
        print(f"  {cli_label:<44s} {cli_short}")
    else:
        print(f"  {cli_label:<44s} (unknown: {drift.error})")

    if drift.drifted is True:
        assert drift.cli_commit is not None  # narrows for mypy/pyright
        print(
            f"\n  ! DRIFT: repo-weaver's bundled wiki-weaver ({bundled_short}) differs "
            f"from the wiki-weaver CLI on PATH ({drift.cli_commit[:8]}) \u2014 run "
            "`repo-weaver update` to bring them back in sync."
        )
    elif drift.drifted is False:
        print(
            f"  \u2713 wiki-weaver in sync (bundled and CLI on PATH both at {bundled_short})"
        )
    else:
        print(
            "  ! could not determine wiki-weaver sync status: "
            f"{drift.error or 'commit unavailable'}"
        )
    print()

    if all_ok:
        print("All checks passed.")
        return 0

    print("Some checks failed.  Install hints:")
    print("  wiki-weaver    : pip install wiki-weaver  OR  uv tool install wiki-weaver")
    print("  gh             : https://cli.github.com/")
    print(
        "  LLM API key    : export ANTHROPIC_API_KEY=<key>  "
        "(or GOOGLE_API_KEY / OPENAI_API_KEY)"
    )
    return 1


# ---------------------------------------------------------------------------
# Subcommand: init
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Thin CLI wrapper — delegates to :func:`repo_weaver.weave.init`."""
    # args.repo is list[str] | None  (action="append"; None when flag is absent)
    repo_args: Optional[list[str]] = getattr(args, "repo", None)
    return weave_mod.init(corpus=args.corpus_dir, repos=repo_args)


# ---------------------------------------------------------------------------
# Subcommand: weave
# ---------------------------------------------------------------------------


def cmd_weave(args: argparse.Namespace) -> int:
    """Materialise sources and (unless --dry-run) run wiki-weaver ingest."""
    corpus = args.corpus
    repo_override: Optional[str] = getattr(args, "repo", None)
    classify: bool = not getattr(args, "no_classify", False)
    no_fetch: bool = getattr(args, "no_fetch", False)

    if repo_override:
        # Explicit --repo override: single-repo path — qualified filenames.
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
            no_fetch=no_fetch,
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
        no_fetch=no_fetch,
    )


# ---------------------------------------------------------------------------
# Subcommand: ask
# ---------------------------------------------------------------------------


def cmd_ask(args: argparse.Namespace) -> int:
    """Thin CLI wrapper — delegates to :func:`repo_weaver.weave.ask`."""
    return weave_mod.ask(
        question=args.question,
        corpus=args.corpus,
        output_json=args.json,
    )


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
        no_fetch=getattr(args, "no_fetch", False),
    )


# ---------------------------------------------------------------------------
# Subcommand: sync
# ---------------------------------------------------------------------------


def _sync_exit_code(result: dict[str, object], dry_run: bool) -> int:
    """Determine the exit code for a ``sync_corpus()`` result.

    A genuine ``gh`` discovery failure for any owner makes the run non-zero
    regardless of what else happened -- the CHANGED list is incomplete for
    that owner, and an unattended/scheduled caller must not mistake this for
    a real no-op (silent-stale trap).
    """
    if result.get("discovery_failed"):
        return 1
    changed = result.get("changed", [])
    if not changed or dry_run:
        return 0
    failed = result.get("failed", [])
    return 1 if failed else 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Detect changed/tracked repos since each repo's own last sync and re-weave them.

    Thin CLI wrapper over :func:`repo_weaver.sync.sync_corpus` -- see that
    function's docstring for the full detection + weave algorithm.
    """
    output_json: bool = getattr(args, "json", False)

    try:
        result = sync_corpus(
            corpus=args.corpus,
            clones_dir=args.clones_dir,
            since=args.since,
            until=args.until,
            dry_run=args.dry_run,
            max_modules=args.max_modules,
        )
    except ValueError as exc:
        if output_json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if output_json:
        print(json.dumps(result, indent=2))
        return _sync_exit_code(result, args.dry_run)

    changed = result["changed"]
    print(f"[repo-weaver] Last sync: {result['last_sync']}  ->  {result['until']}")
    print(f"[repo-weaver] Tracked owners checked: {len(result['owners'])}")
    for err in result["errors"]:
        print(f"[repo-weaver] WARNING: {err}", file=sys.stderr)

    discovery_failed = result.get("discovery_failed", [])
    if discovery_failed:
        print(
            f"[repo-weaver] ERROR: gh discovery failed for {len(discovery_failed)} "
            f"owner(s): {', '.join(discovery_failed)} -- the changed-repo list is "
            "incomplete for these owners; re-run once the underlying gh issue is resolved.",
            file=sys.stderr,
        )

    if not changed:
        print("[repo-weaver] No tracked repos changed since last sync.")
        return _sync_exit_code(result, args.dry_run)

    print(f"[repo-weaver] Changed: {len(changed)} repo(s)")
    for entry in changed:
        print(f"  - {entry['nameWithOwner']}  (pushed {entry['pushedAt']})")

    if args.dry_run:
        print("\n[repo-weaver] dry-run complete -- no clones made, nothing woven.")
        return _sync_exit_code(result, args.dry_run)

    woven = result.get("woven", [])
    failed = result.get("failed", [])
    print(f"\n[repo-weaver] Woven: {len(woven) - len(failed)}/{len(changed)} succeeded")
    if failed:
        print(f"[repo-weaver] FAILED: {len(failed)} repo(s):", file=sys.stderr)
        for name in failed:
            print(f"  - {name}", file=sys.stderr)
    return _sync_exit_code(result, args.dry_run)


# ---------------------------------------------------------------------------
# Subcommand: discover
# ---------------------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    """Discover repos matching caller-supplied rules via ``gh`` (mechanism only).

    ``--rules-file`` is authored and owned by the CALLER (e.g. Team Pulse's own
    orchestrator) -- repo-weaver does not define, validate, or persist a
    discovery config schema; it only loads whatever rule list is passed in and
    applies it via :func:`repo_weaver.gitio.discover_repos`. Policy (which
    owners, which match patterns, fork/visibility rules per source) stays
    entirely with the caller; repo-weaver supplies only the mechanism.
    """
    output_json: bool = getattr(args, "json", False)
    rules_path = Path(args.rules_file).expanduser()

    try:
        raw = rules_path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"could not read --rules-file {rules_path}: {exc}"
        if output_json:
            print(json.dumps({"error": msg}, indent=2))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 1

    try:
        rules = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"--rules-file is not valid JSON: {exc}"
        if output_json:
            print(json.dumps({"error": msg}, indent=2))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 1

    if not isinstance(rules, list):
        msg = "--rules-file must contain a JSON list of rule objects."
        if output_json:
            print(json.dumps({"error": msg}, indent=2))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 1

    matched, errors = gitio.discover_repos(rules)

    if output_json:
        print(json.dumps({"matched": matched, "errors": errors}, indent=2))
        return 1 if errors else 0

    print(
        f"[repo-weaver] Discovered {len(matched)} repo(s) across {len(rules)} rule(s)."
    )
    for repo in matched:
        name_with_owner = repo.get("nameWithOwner", "?")
        pushed_at = repo.get("pushedAt", "")
        print(f"  - {name_with_owner}  (pushed {pushed_at})")
    for err in errors:
        print(f"[repo-weaver] WARNING: {err}", file=sys.stderr)
    return 1 if errors else 0


# ---------------------------------------------------------------------------
# Subcommand: build-dashboard
# ---------------------------------------------------------------------------


def _ensure_corpus_theme(corpus: str) -> None:
    """Write the packaged repo-weaver default theme into the corpus if absent.

    wiki-weaver reads ``.wiki/dashboard/theme.json`` automatically; writing it
    here (idempotently) means the repo-weaver title + GitHub-flavoured accent
    apply without the caller needing to pass ``--theme`` every time.
    """
    dashboard_dir = wiki_dashboard(Path(corpus))
    theme_dst = dashboard_dir / "theme.json"
    if theme_dst.exists():
        return  # user already has a theme; never clobber it

    if not _DEFAULT_THEME.exists():
        print(
            "WARNING: packaged repo-weaver theme not found; dashboard will use wiki-weaver defaults.",
            file=sys.stderr,
        )
        return

    dashboard_dir.mkdir(parents=True, exist_ok=True)
    theme_dst.write_text(_DEFAULT_THEME.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"[repo-weaver] Wrote default theme → {theme_dst}", flush=True)


def cmd_build_dashboard(args: argparse.Namespace) -> int:
    """Build a repo-flavoured HTML dashboard via wiki-weaver build-dashboard.

    Delegates entirely to ``wiki-weaver build-dashboard`` using the existing
    subprocess boundary (zero direct LLM calls).  Path helpers are imported
    from ``wiki_weaver.lib`` so corpus layout never drifts between the two.

    repo-weaver contributes two domain-specific policies on top of the generic
    wiki-weaver mechanism:

    1. ``--group-by repos`` — pages are grouped by their ``repos:`` frontmatter
       list field (multi-membership: a page appears under every repo it touches).
    2. ``--group-link-template 'https://github.com/{group}'`` — each repo group
       header becomes a live GitHub link.

    A packaged default theme (title + GitHub-flavoured accent) is written to
    ``<corpus>/.wiki/dashboard/theme.json`` if one is not already present.
    """
    corpus = args.corpus

    # Probe: does the installed wiki-weaver support build-dashboard?
    probe = subprocess.run(
        ["wiki-weaver", "build-dashboard", "--help"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        print(
            "ERROR: wiki-weaver does not support the 'build-dashboard' subcommand.\n"
            "Install or upgrade wiki-weaver:\n"
            "  uv tool install --force --editable <path/to/wiki-weaver-checkout>\n"
            "  OR  pip install --upgrade wiki-weaver",
            file=sys.stderr,
        )
        return 1

    # Idempotently seed the corpus with the repo-weaver default theme.
    _ensure_corpus_theme(corpus)

    # Build the wiki-weaver argv.  The only repo-weaver-specific bits are:
    #   --group-by repos                (group by the multi-valued repos: field)
    #   --group-link-template           (turn group headers into GitHub links)
    argv: list[str] = [
        "wiki-weaver",
        "build-dashboard",
        corpus,
        "--out",
        args.out,
        "--group-by",
        "repos",
        "--group-link-template",
        _GITHUB_GROUP_LINK_TEMPLATE,
    ]
    if getattr(args, "theme", None):
        argv += ["--theme", args.theme]

    # Flush before the subprocess so our progress message appears in the
    # correct order alongside wiki-weaver's direct stdout writes.
    print(f"[repo-weaver] Running: {' '.join(argv)}", flush=True)
    result = subprocess.run(argv)
    return result.returncode


# ---------------------------------------------------------------------------
# Subcommand: update
# ---------------------------------------------------------------------------


def cmd_update(args: argparse.Namespace) -> int:
    """Refresh repo-weaver's own install, then delegate to `wiki-weaver update`.

    Thin CLI wrapper over :func:`repo_weaver.updater.update` -- see that
    function's docstring for the full two-stage refresh (repo-weaver itself
    via the uv reinstall ladder, then wiki-weaver via subprocess delegation).
    """
    return updater.update(check_only=args.check)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


class _VersionAction(argparse.Action):
    """Resolves + prints the version lazily, only when --version is passed.

    Unlike argparse's built-in "version" action (which formats its string
    eagerly at add_argument() time), this defers resolve_version() until the
    flag is actually invoked -- so a possible dev-mode git subprocess call
    (see repo_weaver._version_resolve) never runs on ordinary command
    invocations, only on `repo-weaver --version` itself.
    """

    def __init__(self, option_strings, dest=argparse.SUPPRESS, **kwargs) -> None:
        kwargs.setdefault("nargs", 0)
        kwargs.setdefault("help", "show program's version number and exit")
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        print(f"repo-weaver {resolve_version(__version__)}")
        parser.exit()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repo-weaver",
        description=(
            "Turn a git repo's commits and PRs into a queryable wiki corpus via wiki-weaver."
        ),
    )
    parser.add_argument("--version", action=_VersionAction)

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
            f"Max per-source retry attempts after a .wiki/failed/ event (default: {_DEFAULT_MAX_RETRIES}). "
            "Each transient-error retry applies exponential back-off; "
            "each not-converged retry increases --max-cycles."
        ),
    )
    p.add_argument(
        "--no-fetch",
        action="store_true",
        default=False,
        dest="no_fetch",
        help=(
            "Skip the git fetch staleness check before materialising. "
            "Use for offline or repeatable runs where a network call to origin "
            "is undesirable. Without this flag, repo-weaver fetches from origin "
            "and warns (or fast-forwards) when the local clone is behind."
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
    p.add_argument(
        "--no-fetch",
        action="store_true",
        default=False,
        dest="no_fetch",
        help=(
            "Skip the git fetch staleness check before materialising each window. "
            "Use for offline or repeatable replay runs."
        ),
    )
    p.set_defaults(func=cmd_replay)

    # ---- sync ----
    p = sub.add_parser(
        "sync",
        help=(
            "Detect tracked repos that changed since the corpus's last sync "
            "(via `gh`, no cloning needed for detection) and re-weave them."
        ),
    )
    p.add_argument("--corpus", required=True, metavar="DIR", help="Corpus directory.")
    p.add_argument(
        "--clones-dir",
        metavar="PATH",
        default=_DEFAULT_SYNC_CLONES_DIR,
        help=(
            "Directory to hold/locate local clones of changed repos, one "
            f"subdirectory per repo (default: {_DEFAULT_SYNC_CLONES_DIR}). "
            "'~' is expanded."
        ),
    )
    p.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        default=None,
        help=(
            "Override the detected last-sync date (exclusive). Default: the "
            "max YYYY-MM-DD parsed across the corpus's _sources/*-changes.md "
            "filenames."
        ),
    )
    p.add_argument(
        "--until",
        metavar="YYYY-MM-DD",
        default=None,
        help="Window end (inclusive). Default: today.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect and print the changed-repo list only; clone/weave nothing.",
    )
    p.add_argument(
        "--max-modules",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Max module snapshot documents to emit per changed repo (default: 0 "
            "-- changes-only, matching a fast-sync run)."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help=(
            "Output the structured sync result as JSON to stdout instead of the "
            "human-readable summary (for programmatic/scheduled callers)."
        ),
    )
    p.set_defaults(func=cmd_sync)

    # ---- discover ----
    p = sub.add_parser(
        "discover",
        help=(
            "Discover repos matching caller-supplied rules via `gh` "
            "(mechanism only -- repo-weaver does not own a discovery config schema)."
        ),
    )
    p.add_argument(
        "--rules-file",
        required=True,
        metavar="PATH",
        help=(
            "Path to a JSON file containing a list of discovery rule objects, e.g. "
            '[{"owner": "microsoft", "match": "amplifier*", "include_forks": true, '
            '"visibility": "all"}, {"owner": "someuser", "match": "amplifier*", '
            '"include_forks": false, "visibility": "all"}]. '
            "This file is authored and OWNED BY THE CALLER (e.g. your own orchestrator) "
            "-- repo-weaver does not define, validate, or persist a discovery config "
            "schema; it only loads and applies whatever rules you pass in, each "
            "invocation. Rule keys: owner (required), match (glob/prefix, required), "
            "include_forks (bool, default true), visibility "
            '("public"/"private"/"all", default "all").'
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help=(
            "Output JSON: {matched: [...], errors: [...]} for programmatic "
            "consumption (e.g. by an orchestrator that calls `discover` then `sync`)."
        ),
    )
    p.set_defaults(func=cmd_discover)

    # ---- build-dashboard ----
    p = sub.add_parser(
        "build-dashboard",
        help=(
            "Build a repo-flavoured HTML dashboard via wiki-weaver build-dashboard. "
            "Pages are grouped by repos: field (multi-membership); each group header "
            "links to https://github.com/<repo>."
        ),
    )
    p.add_argument("corpus", help="wiki corpus directory.")
    p.add_argument(
        "--out", required=True, metavar="PATH", help="Destination .html file."
    )
    p.add_argument(
        "--theme",
        metavar="PATH",
        default=None,
        help=(
            "Path to a theme.json file (optional). Overrides the corpus's "
            ".wiki/dashboard/theme.json. If absent, the packaged repo-weaver "
            "default theme (GitHub-flavoured slate) is used."
        ),
    )
    p.set_defaults(func=cmd_build_dashboard)

    # ---- update ----
    p = sub.add_parser(
        "update",
        help=(
            "Refresh repo-weaver to latest @main, then delegate to "
            "`wiki-weaver update` to keep it current too."
        ),
    )
    p.add_argument(
        "--check",
        "--dry-run",
        action="store_true",
        dest="check",
        default=False,
        help=(
            "Report drift (repo-weaver + wiki-weaver) without making any "
            "changes. Aliases: --check, --dry-run."
        ),
    )
    p.set_defaults(func=cmd_update)

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
