"""Amplifier tool module: repo-weaver commands as mountable tools.

Registers 5 tools — one per core repo-weaver command — that an AmplifierSession
agent can invoke. Each tool is a thin wrapper over the importable
``repo_weaver`` lib API (``repo_weaver.init``, ``repo_weaver.weave``,
``repo_weaver.ask``, ``repo_weaver.sync.sync_corpus``,
``repo_weaver.gitio.discover_repos``):

    tool.execute(input_data)
      → await asyncio.to_thread(run_<cmd>, ...)   (lib fns are SYNCHRONOUS)
      → returns ToolResult(success=..., output=<status/answer>)

WHY asyncio.to_thread (the one non-obvious wrinkle):
    repo-weaver's init / weave / ask are *synchronous* functions that call
    subprocess.run() internally (to invoke wiki-weaver as an external process).
    A tool's ``execute()`` is itself awaited inside the host session's running
    event loop, and subprocess.run() is blocking. Running each sync function in
    a worker thread (to_thread) avoids blocking the event loop.

WHY direct subprocess capture for ask:
    ``repo_weaver.ask()`` calls ``subprocess.run(["wiki-weaver", "ask", ...])``
    WITHOUT capture_output — the answer flows to the process's inherited stdout
    (agent log) rather than being returned. For the tool to deliver the answer
    to the calling agent, the ask tool calls the same underlying wiki-weaver
    command directly with capture_output=True.  This is semantically identical
    to calling ``repo_weaver.ask()``; the only difference is output routing.

For init and weave, Python-level print() output is captured via redirect_stdout
and returned in the ToolResult; the wiki-weaver subprocess output (ingest
progress) goes to the process log.  The exit code and Python-level status
messages are the primary signal for the agent.

All real work lives in ``repo_weaver`` (the bundle's root package, installed
editable by Bundle.prepare() before this module activates).  This module adds
NO logic beyond mapping tool arguments to lib arguments and shaping the result.

The Iron Law (creating-amplifier-modules skill): mount() MUST call
coordinator.mount() for each tool, or protocol_compliance validation fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import subprocess
from typing import Any

import repo_weaver
from amplifier_core import ToolResult
from repo_weaver.gitio import discover_repos
from repo_weaver.sync import sync_corpus

logger = logging.getLogger(__name__)

# Mirrors repo_weaver.cli._DEFAULT_SYNC_CLONES_DIR (a CLI-layer default, not
# re-imported here to avoid reaching into the CLI module's internals).
_DEFAULT_CLONES_DIR = "~/dev/amplifier-corpus-clones"


# ---------------------------------------------------------------------------
# Tool classes — one per command. Each maps arguments → real lib call.
# ---------------------------------------------------------------------------


class RepoWeaverInitTool:
    """Scaffold a corpus directory and register git repositories."""

    @property
    def name(self) -> str:
        return "repo_weaver_init"

    @property
    def description(self) -> str:
        return (
            "Initialise a repo-weaver corpus: scaffold the wiki layout, install the "
            "code-fit schema (the entity model that wiki-weaver uses to synthesise git "
            "knowledge pages), and optionally register one or more local git repository "
            "paths. Must be run before repo_weaver_weave. Wraps "
            "repo_weaver.init(corpus, repos=...)."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "corpus": {
                    "type": "string",
                    "description": (
                        "Absolute path to the corpus directory. Created if absent. "
                        "This is the wiki that repo-weaver will populate with "
                        "synthesised knowledge pages from your git repositories."
                    ),
                },
                "repos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of absolute paths to local git repositories to "
                        "register with the corpus. Paths are resolved to absolute before "
                        "saving to the corpus config (.repo-weaver.json). Omit to create "
                        "a repo-less corpus — repositories can be weaved individually "
                        "later via repo_weaver_weave without pre-registration."
                    ),
                },
            },
            "required": ["corpus"],
        }

    async def execute(self, input_data: dict[str, Any]) -> ToolResult:
        corpus = input_data["corpus"]
        # Treat an explicitly-passed empty list the same as omitting repos.
        repos: list[str] | None = input_data.get("repos") or None

        def _call() -> tuple[int, str]:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = repo_weaver.init(corpus, repos=repos)
            return rc, buf.getvalue()

        rc, output = await asyncio.to_thread(_call)
        return ToolResult(
            success=rc == 0,
            output=(output.strip() or f"init exit code {rc}")[:8000],
        )


class RepoWeaverWeaveTool:
    """Materialise git history into the corpus and ingest via wiki-weaver."""

    @property
    def name(self) -> str:
        return "repo_weaver_weave"

    @property
    def description(self) -> str:
        return (
            "Ingest a git repository's history into the repo-weaver corpus: materialises "
            "commit and PR source documents for the given time window, writes them to the "
            "corpus _inbox/, then calls wiki-weaver ingest to synthesise concept pages "
            "(with automatic retry on transient failures). LONG-RUNNING — allow several "
            "minutes per source document. The corpus must already be initialised via "
            "repo_weaver_init and the repo must be a valid local git clone. Wraps "
            "repo_weaver.weave(corpus, repo, since, until, ...)."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "corpus": {
                    "type": "string",
                    "description": (
                        "Absolute path to the corpus directory. Must be initialised "
                        "first with repo_weaver_init."
                    ),
                },
                "repo": {
                    "type": "string",
                    "description": "Absolute path to the local git repository to weave.",
                },
                "since": {
                    "type": "string",
                    "description": (
                        "Window start date (exclusive), ISO format YYYY-MM-DD. "
                        "Omit to auto-detect from the repository's first commit date "
                        "(one day before the earliest commit so it is included)."
                    ),
                },
                "until": {
                    "type": "string",
                    "description": (
                        "Window end date (inclusive), ISO format YYYY-MM-DD. "
                        "Omit to use today's date."
                    ),
                },
                "max_modules": {
                    "type": "integer",
                    "description": (
                        "Maximum module-snapshot documents to emit per repo (default 5). "
                        "Reduce if you only need the change-history digest, not per-module "
                        "code snapshots."
                    ),
                },
                "no_fetch": {
                    "type": "boolean",
                    "description": (
                        "If true, skip `git fetch` and use the local clone state as-is "
                        "(default false). Use for offline or repeatable runs, or when the "
                        "clone is already known to be current."
                    ),
                },
            },
            "required": ["corpus", "repo"],
        }

    async def execute(self, input_data: dict[str, Any]) -> ToolResult:
        corpus = input_data["corpus"]
        repo = input_data["repo"]
        # Treat empty string the same as omitted (let the lib auto-detect).
        since: str | None = input_data.get("since") or None
        until: str | None = input_data.get("until") or None
        max_modules = int(input_data.get("max_modules", 5))
        no_fetch = bool(input_data.get("no_fetch", False))

        def _call() -> tuple[int, str]:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = repo_weaver.weave(
                    corpus=corpus,
                    repo=repo,
                    since=since,
                    until=until,
                    max_modules=max_modules,
                    no_fetch=no_fetch,
                )
            return rc, buf.getvalue()

        rc, output = await asyncio.to_thread(_call)
        return ToolResult(
            success=rc == 0,
            output=(output.strip() or f"weave exit code {rc}")[:8000],
        )


class RepoWeaverAskTool:
    """Query the corpus and return a cited answer (read-only)."""

    @property
    def name(self) -> str:
        return "repo_weaver_ask"

    @property
    def description(self) -> str:
        return (
            "Answer a question by reading the repo-weaver corpus (no embeddings/RAG): "
            "wiki-weaver navigates the compiled knowledge pages, synthesises a cited "
            "answer, and explicitly refuses ('the corpus does not cover X') when the "
            "topic is absent. READ-ONLY — does not modify the corpus. Returns the "
            "cited answer and the pages used. Equivalent to repo_weaver.ask() with "
            "output capture (--json flag) so the answer reaches the calling agent."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "Natural-language question to answer against the corpus. "
                        "Avoid embedding double-quotes — the question is passed as a "
                        "single command-line argument to wiki-weaver."
                    ),
                },
                "corpus": {
                    "type": "string",
                    "description": (
                        "Absolute path to an initialised and populated corpus directory "
                        "(repo_weaver_init + at least one repo_weaver_weave must have run)."
                    ),
                },
            },
            "required": ["question", "corpus"],
        }

    async def execute(self, input_data: dict[str, Any]) -> ToolResult:
        question = input_data["question"]
        corpus = input_data["corpus"]

        def _call() -> tuple[int, str]:
            # repo_weaver.ask() calls subprocess.run() WITHOUT capture_output, so
            # the answer flows to the process log rather than being returned to the
            # calling agent.  Call the same underlying wiki-weaver command directly
            # with capture_output=True so the answer is delivered to the agent.
            r = subprocess.run(
                ["wiki-weaver", "ask", question, "--wiki", corpus, "--json"],
                capture_output=True,
                text=True,
            )
            combined = r.stdout
            if r.stderr.strip():
                combined += "\n" + r.stderr.strip()
            return r.returncode, combined

        rc, raw = await asyncio.to_thread(_call)

        output = raw.strip() or f"ask exit code {rc}"

        # wiki-weaver --json returns {"answer": ..., "pages_used": [...], "refused": bool}.
        # Parse and format; fall back to raw text if the response is not valid JSON.
        try:
            data = json.loads(output)
            answer = str(data.get("answer", "")).strip()
            pages = data.get("pages_used") or []
            refused = bool(data.get("refused", False))
            if pages:
                answer += "\n\nPages used: " + ", ".join(pages)
            output = answer or output
            return ToolResult(success=not refused and rc == 0, output=output[:8000])
        except (json.JSONDecodeError, ValueError):
            # wiki-weaver returned plain text or an error — surface it as-is.
            return ToolResult(success=rc == 0, output=output[:8000])


class RepoWeaverSyncTool:
    """Detect and re-weave tracked repos that changed since their own last sync."""

    @property
    def name(self) -> str:
        return "repo_weaver_sync"

    @property
    def description(self) -> str:
        return (
            "Detect tracked repos that changed since the corpus's OWN per-repo "
            "last-sync date (derived from _sources/*-changes.md filenames — no manual "
            "repo list needed) via `gh`, then clone/re-weave each changed repo over the "
            "existing single-repo weave path. LONG-RUNNING whenever repos have changed "
            "(each is a full weave). Returns the structured sync result (last_sync, "
            "until, owners, changed, errors, discovery_failed, and — unless dry_run — "
            "woven/failed) as JSON. The corpus must already have at least one "
            "repo_weaver_weave run (or pass `since` explicitly). Wraps "
            "repo_weaver.sync.sync_corpus(...)."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "corpus": {
                    "type": "string",
                    "description": (
                        "Absolute path to the corpus directory. Must already have at "
                        "least one change-digest source under _sources/ (from a prior "
                        "repo_weaver_weave), unless `since` is supplied explicitly."
                    ),
                },
                "clones_dir": {
                    "type": "string",
                    "description": (
                        "Directory to hold/locate local clones of changed repos, one "
                        "subdirectory per repo (<clones_dir>/<owner>__<repo>). '~' is "
                        f"expanded. Default: {_DEFAULT_CLONES_DIR!r}."
                    ),
                },
                "since": {
                    "type": "string",
                    "description": (
                        "Override the detected last-sync date (YYYY-MM-DD, exclusive), "
                        "applied globally to every tracked repo. Omit to use each "
                        "repo's own last-sync date, derived independently from its own "
                        "_sources/*-changes.md filenames."
                    ),
                },
                "until": {
                    "type": "string",
                    "description": (
                        "Window end date (inclusive), ISO format YYYY-MM-DD. Omit to "
                        "use today's date."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "If true, detect and report the changed-repo list only; do NOT "
                        "clone or weave anything (default false)."
                    ),
                },
                "max_modules": {
                    "type": "integer",
                    "description": (
                        "Max module-snapshot documents to emit per changed repo "
                        "(default 0 — changes-only, matching a fast-sync run)."
                    ),
                },
            },
            "required": ["corpus"],
        }

    async def execute(self, input_data: dict[str, Any]) -> ToolResult:
        corpus = input_data["corpus"]
        clones_dir = input_data.get("clones_dir") or _DEFAULT_CLONES_DIR
        # Treat empty string the same as omitted (let sync_corpus derive per-repo).
        since: str | None = input_data.get("since") or None
        until: str | None = input_data.get("until") or None
        dry_run = bool(input_data.get("dry_run", False))
        max_modules = int(input_data.get("max_modules", 0))

        def _call() -> dict[str, Any]:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                # sync_corpus has one internal print() (a staleness warning on
                # clone-ensure failure); captured here so it reaches the agent
                # even though the structured result dict is the primary output.
                return sync_corpus(
                    corpus=corpus,
                    clones_dir=clones_dir,
                    since=since,
                    until=until,
                    dry_run=dry_run,
                    max_modules=max_modules,
                )

        try:
            result = await asyncio.to_thread(_call)
        except ValueError as exc:
            # Raised when no last-sync date is derivable (never-woven corpus,
            # no `since` override) — see sync_corpus's docstring.
            return ToolResult(success=False, output=f"sync error: {exc}")

        # Mirrors repo_weaver.cli._sync_exit_code(): a genuine gh discovery
        # failure always fails (the changed-list is incomplete for that
        # owner); otherwise success unless a changed repo failed to
        # clone/weave.
        if result.get("discovery_failed"):
            success = False
        elif not result.get("changed") or dry_run:
            success = True
        else:
            success = not result.get("failed")

        return ToolResult(success=success, output=json.dumps(result, indent=2)[:8000])


class RepoWeaverDiscoverTool:
    """Discover repos matching caller-supplied rules via `gh` (mechanism only)."""

    @property
    def name(self) -> str:
        return "repo_weaver_discover"

    @property
    def description(self) -> str:
        return (
            "Discover GitHub repos across one or more owners via `gh` (mechanism "
            "only — repo-weaver does not own, validate, or persist a discovery "
            "policy; the caller decides which owners/patterns/visibility to use). "
            "Each rule supplies an owner (user or org) plus a name-match pattern; "
            "matched repos across all rules are merged and deduplicated by "
            "nameWithOwner. Use this to find NEW repos to register/weave — not to "
            "re-check already-tracked ones (that's repo_weaver_sync's job). Wraps "
            "repo_weaver.gitio.discover_repos(rules)."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "rules": {
                    "type": "array",
                    "description": (
                        "List of discovery rule objects, one per owner/source. Each "
                        "rule is queried independently via `gh repo list`; matched "
                        "repos from all rules are merged and deduplicated."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "owner": {
                                "type": "string",
                                "description": (
                                    "GitHub user or org login to query (required)."
                                ),
                            },
                            "match": {
                                "type": "string",
                                "description": (
                                    "Glob/prefix pattern matched against each repo's "
                                    'name via fnmatch, e.g. "amplifier*" (required).'
                                ),
                            },
                            "include_forks": {
                                "type": "boolean",
                                "description": "Include forked repos (default true).",
                            },
                            "visibility": {
                                "type": "string",
                                "enum": ["public", "private", "all"],
                                "description": (
                                    "Repo visibility filter, honoring the `gh` "
                                    "token's access (default 'all')."
                                ),
                            },
                        },
                        "required": ["owner", "match"],
                    },
                },
            },
            "required": ["rules"],
        }

    async def execute(self, input_data: dict[str, Any]) -> ToolResult:
        rules = input_data["rules"]

        def _call() -> tuple[list[dict[str, object]], list[str]]:
            return discover_repos(rules)

        matched, errors = await asyncio.to_thread(_call)

        # Mirrors repo_weaver.cli.cmd_discover(): a rule-level gh failure
        # makes the run non-zero regardless of how many other rules matched.
        output = json.dumps({"matched": matched, "errors": errors}, indent=2)
        return ToolResult(success=not errors, output=output[:8000])


# ---------------------------------------------------------------------------
# mount() — THE required entry point. Iron Law: must call coordinator.mount()
# for every tool, or protocol_compliance validation fails.
# ---------------------------------------------------------------------------

_TOOLS = [
    RepoWeaverInitTool(),
    RepoWeaverWeaveTool(),
    RepoWeaverAskTool(),
    RepoWeaverSyncTool(),
    RepoWeaverDiscoverTool(),
]


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Mount all 3 repo-weaver tools into the coordinator.

    Satisfies the Iron Law: calls coordinator.mount() for each tool so that
    protocol_compliance validation passes.
    """
    for tool in _TOOLS:
        await coordinator.mount("tools", tool, name=tool.name)
        logger.debug("tool-repo-weaver: mounted '%s'", tool.name)

    names = [t.name for t in _TOOLS]
    logger.info("tool-repo-weaver: mounted %d tools: %s", len(names), names)
    return {
        "name": "tool-repo-weaver",
        "version": "0.1.0",
        "provides": names,
    }
