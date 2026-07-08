# pyright: reportMissingImports=false
"""repo-weaver source freshness utilities.

Strategy: track @main, fix-forward -- no SHA pinning (same strategy as
wiki_weaver.updater). This module mirrors wiki_weaver.updater's Layer-1 ladder
structurally (same ``SourceRecord``, same PEP 610 ``direct_url.json`` read,
same 3-rung verify+escalate+fail-loud reinstall ladder) but is scoped to
repo-weaver's actual situation, which differs from wiki-weaver's in two ways:

  * repo-weaver tracks exactly ONE Layer-1 source: itself. It has no OTHER
    git-pinned wheel dependency worth tracking as an independent moving
    target the way wiki-weaver tracks ``amplifier-foundation`` /
    ``amplifier-unified-llm-client`` -- repo-weaver's only git dependency is
    wiki-weaver, and wiki-weaver already owns a complete two-layer refresh
    for itself (see :func:`update_wiki_weaver`).
  * repo-weaver does not depend on ``amplifier-foundation``, so remote-commit
    lookups use a plain ``git ls-remote`` subprocess instead of foundation's
    ``GitSourceHandler`` (which wiki_weaver.updater uses for the same fact).

``update()`` is the top-level orchestrator (mirrors ``wiki_weaver.lib.update``
/ ``_update_check`` / ``_update_real``): it refreshes repo-weaver's own
install first, then delegates to ``wiki-weaver update`` as the final step --
covering wiki-weaver's own install AND its Layer-2 engine bundle cache in one
call -- so a single ``repo-weaver update`` keeps both tools current.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------

# The uv-tool install URL for repo-weaver itself.
INSTALL_URL = "git+https://github.com/microsoft/amplifier-app-repo-weaver"

# Layer-1 source repo-weaver tracks: itself.  (dist-package-name,
# plain-https-url-for-ls-remote).  repo-weaver has no OTHER git-pinned wheel
# dependency worth tracking here -- its only git dependency is wiki-weaver,
# which is refreshed via update_wiki_weaver() (a subprocess delegation to
# wiki-weaver's own `update` command), not this ladder.
_LAYER1_SOURCES: list[tuple[str, str]] = [
    ("repo-weaver", "https://github.com/microsoft/amplifier-app-repo-weaver"),
]

# The wiki-weaver git dependency, for PEP 610 introspection of repo-weaver's
# OWN bundled/installed copy (used by the drift check, not the ladder).
_WIKI_WEAVER_DIST_NAME = "wiki-weaver"
_WIKI_WEAVER_CLI_NAME = "wiki-weaver"


# ---------------------------------------------------------------------------
# SourceRecord: unified result type (mirrors wiki_weaver.updater.SourceRecord)
# ---------------------------------------------------------------------------


@dataclass
class SourceRecord:
    """Result of a check-or-update operation on one @main git source."""

    label: str
    uri: str
    local_sha: Optional[str] = None
    """Current locally-resolved commit (before update or from cache)."""
    target_sha: Optional[str] = None
    """Target commit: remote HEAD (check) or new-local (after update)."""
    needs_update: Optional[bool] = None
    """True when update is warranted; None when unknown (e.g. network error)."""
    skipped: bool = False
    error: Optional[str] = None

    @property
    def local_short(self) -> str:
        return (self.local_sha or "")[:8] or "(not cached)"

    @property
    def target_short(self) -> str:
        return (self.target_sha or "")[:8] or "(unknown)"

    @property
    def is_mutable(self) -> bool:
        """True when ref is a branch name (@main/HEAD), not a pinned SHA/tag."""
        after_scheme = self.uri.split("://", 1)[-1]
        ref = (
            after_scheme.rsplit("@", 1)[-1].split("#")[0] if "@" in after_scheme else ""
        )
        if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref.lower()):
            return False  # full SHA
        if ref.startswith("v") and any(c.isdigit() for c in ref):
            return False  # version tag
        return True  # branch name = mutable


# ---------------------------------------------------------------------------
# Layer-1: commit reading via PEP 610 direct_url.json (no network)
# ---------------------------------------------------------------------------


def _installed_commit(package_name: str) -> Optional[str]:
    """Read installed git commit SHA from PEP 610 direct_url.json.

    Reads from disk (not an in-memory cache), so it reflects the filesystem
    state even within the same process after a ``uv tool install --reinstall``.
    ``importlib.invalidate_caches()`` flushes any stale path lookups first.
    """
    try:
        importlib.invalidate_caches()
        dist = importlib.metadata.distribution(package_name)
        raw = dist.read_text("direct_url.json")
        if raw:
            info = json.loads(raw)
            return info.get("vcs_info", {}).get("commit_id")
    except Exception:  # noqa: BLE001
        pass
    return None


def installed_commit_records() -> list[SourceRecord]:
    """Read locally-installed commits for Layer-1 sources -- no network.

    Used by ``doctor`` to report "what am I running" without a network call.
    """
    results: list[SourceRecord] = []
    for name, git_url in _LAYER1_SOURCES:
        rec = SourceRecord(label=name, uri=f"git+{git_url}@main")
        rec.local_sha = _installed_commit(name)
        results.append(rec)
    return results


# ---------------------------------------------------------------------------
# Layer-1: check (installed vs remote via `git ls-remote`)
# ---------------------------------------------------------------------------


async def _get_remote_commit_for(git_url: str, ref: str = "main") -> Optional[str]:
    """Return the remote commit SHA for *git_url* at *ref* via ``git ls-remote``.

    repo-weaver does not depend on amplifier-foundation, so this is a direct
    subprocess call rather than foundation's GitSourceHandler (which
    wiki_weaver.updater uses for the same fact).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "ls-remote",
            git_url,
            f"refs/heads/{ref}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        lines = stdout.decode().strip().splitlines()
        if not lines:
            return None
        return lines[0].split()[0]
    except Exception:  # noqa: BLE001
        return None


async def _check_source(name: str, git_url: str) -> SourceRecord:
    rec = SourceRecord(label=name, uri=f"git+{git_url}@main")
    rec.local_sha = _installed_commit(name)
    rec.target_sha = await _get_remote_commit_for(git_url)
    if rec.local_sha and rec.target_sha:
        rec.needs_update = rec.local_sha != rec.target_sha
    return rec


async def _check_l1_all() -> list[SourceRecord]:
    return list(
        await asyncio.gather(*[_check_source(n, u) for n, u in _LAYER1_SOURCES])
    )


def check_layer1() -> list[SourceRecord]:
    """ls-remote all Layer-1 sources and compare to installed commits."""
    return asyncio.run(_check_l1_all())


# ---------------------------------------------------------------------------
# Layer-1 update: uv tool install --reinstall + verify + ladder + fail-loud
# ---------------------------------------------------------------------------


@dataclass
class Layer1Result:
    """Outcome of the Layer-1 uv reinstall ladder."""

    success: bool = False
    rung_reached: int = 0
    """1 = plain reinstall, 2 = --no-cache, 3 = cache-clean+reinstall."""
    before: dict[str, Optional[str]] = field(default_factory=dict)
    after: dict[str, Optional[str]] = field(default_factory=dict)
    remote: dict[str, Optional[str]] = field(default_factory=dict)
    stale: list[str] = field(default_factory=list)
    """Packages whose remote had moved but installed commit didn't update."""
    errors: list[str] = field(default_factory=list)


def _run_install(*, no_cache: bool = False) -> tuple[int, str]:
    """Run uv tool install --reinstall [--no-cache].  Returns (rc, stderr)."""
    cmd = ["uv", "tool", "install", "--reinstall"]
    if no_cache:
        cmd.append("--no-cache")
    cmd.append(INSTALL_URL)
    r = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    return r.returncode, r.stderr


def _run_cache_clean(names: list[str]) -> int:
    cmd = ["uv", "cache", "clean", *names]
    return subprocess.run(cmd, capture_output=True, text=True).returncode  # noqa: S603


async def _fetch_remotes() -> list[Optional[str]]:
    return list(
        await asyncio.gather(*[_get_remote_commit_for(u) for _, u in _LAYER1_SOURCES])
    )


def update_layer1(*, verbose: bool = False) -> Layer1Result:
    """Run the Layer-1 uv reinstall ladder with verify+fail-loud.

    Ladder:
      Rung 1: ``uv tool install --reinstall <url>``
      Rung 2: ``uv tool install --reinstall --no-cache <url>``
      Rung 3: ``uv cache clean <deps>`` then ``uv tool install --reinstall <url>``

    After each rung, verifies that packages whose remote HEAD had moved are
    now installed at the new remote commit.  A package is only flagged stale
    when remote != before AND installed-after != remote (i.e., change expected
    but didn't happen).  If stale after all rungs, ``result.success`` is False.
    """
    res = Layer1Result()

    # Pre-step: capture remote HEAD + currently-installed commits
    try:
        remotes: list[Optional[str]] = asyncio.run(_fetch_remotes())
    except Exception as e:  # noqa: BLE001
        res.errors.append(f"pre-check ls-remote failed: {e}")
        remotes = [None] * len(_LAYER1_SOURCES)

    for (name, _), remote in zip(_LAYER1_SOURCES, remotes):
        res.before[name] = _installed_commit(name)
        res.remote[name] = remote

    if verbose:
        for name, _ in _LAYER1_SOURCES:
            b = (res.before[name] or "?")[:8]
            r = (res.remote[name] or "?")[:8]
            print(f"  {name}: installed={b}  remote={r}", flush=True)

    def _check_stale() -> list[str]:
        stale: list[str] = []
        for name, _ in _LAYER1_SOURCES:
            after = _installed_commit(name)
            res.after[name] = after
            before = res.before.get(name)
            remote = res.remote.get(name)
            # Flag stale only when remote had moved but installed didn't follow
            if remote and before and remote != before and after != remote:
                stale.append(name)
        return stale

    # Rung 1: plain --reinstall
    rc, err = _run_install()
    res.rung_reached = 1
    if rc != 0:
        res.errors.append(f"rung-1 failed (exit {rc}): {err[:300]}")
        return res
    stale = _check_stale()
    if not stale:
        res.success = True
        return res

    if verbose:
        print(
            f"  ! rung-1: still stale: {stale}. Trying --no-cache\u2026",
            file=sys.stderr,
        )

    # Rung 2: --reinstall --no-cache
    rc, err = _run_install(no_cache=True)
    res.rung_reached = 2
    if rc != 0:
        res.errors.append(f"rung-2 failed (exit {rc}): {err[:300]}")
        return res
    stale = _check_stale()
    if not stale:
        res.success = True
        return res

    if verbose:
        print(
            f"  ! rung-2: still stale: {stale}. Trying cache clean + reinstall\u2026",
            file=sys.stderr,
        )

    # Rung 3: uv cache clean + reinstall
    pkg_names = [name for name, _ in _LAYER1_SOURCES]
    rc_clean = _run_cache_clean(pkg_names)
    if rc_clean != 0 and verbose:
        print(
            "  ! uv cache clean returned non-zero; continuing anyway", file=sys.stderr
        )
    rc, err = _run_install()
    res.rung_reached = 3
    if rc != 0:
        res.errors.append(f"rung-3 failed (exit {rc}): {err[:300]}")
        return res
    stale = _check_stale()
    res.stale = stale
    res.success = not stale
    return res


# ---------------------------------------------------------------------------
# Delegated: wiki-weaver update (its own two-layer refresh, not reimplemented)
# ---------------------------------------------------------------------------


def update_wiki_weaver(*, check_only: bool = False) -> int:
    """Delegate to ``wiki-weaver update`` (or ``--check``) as a subprocess.

    wiki-weaver already owns a two-layer refresh for itself (its own uv-tool
    install + wheel deps, plus its engine bundle cache) -- repo-weaver must
    not reimplement that ladder or import wiki_weaver's internals (the
    amplifier-tool-leverage-patterns rule: heavy/stateful dependencies stay
    behind the public CLI boundary). This single delegated call keeps the
    standalone wiki-weaver CLI *and* its Layer-2 engine bundles current in
    one step.

    Raises ``RuntimeError`` if wiki-weaver is not found on PATH -- fail loud,
    no silent skip.
    """
    if shutil.which(_WIKI_WEAVER_CLI_NAME) is None:
        raise RuntimeError(
            "wiki-weaver not found on PATH -- cannot update it. "
            "Install with:  uv tool install wiki-weaver  OR  pip install wiki-weaver"
        )
    cmd = [_WIKI_WEAVER_CLI_NAME, "update"]
    if check_only:
        cmd.append("--check")
    result = subprocess.run(cmd)  # noqa: S603
    return result.returncode


# ---------------------------------------------------------------------------
# Drift check: repo-weaver's BUNDLED wiki-weaver vs the wiki-weaver CLI on PATH
# ---------------------------------------------------------------------------
#
# repo-weaver has a DUAL relationship with wiki-weaver: it directly imports
# wiki_weaver.lib as a Python dependency (baked into repo-weaver's own uv-tool
# venv at whatever commit repo-weaver's own install last resolved) AND it
# shells out to a SEPARATELY-installed `wiki-weaver` CLI on PATH (which could
# be a different install, updated independently, at a different commit).
# These two can drift from each other even if each is individually "fresh"
# relative to its own @main -- a distinct failure mode from ordinary
# staleness that plain per-source freshness checks can't answer.
#
# wiki-weaver's own `--version` is a static "wiki-weaver 0.1.0" string that
# does NOT reflect @main commit drift, and its `doctor` reports wheel-dep +
# bundle commits but never its OWN installed commit -- so neither surface
# gives a usable identifier for the standalone CLI's commit.  The only
# reliable source of truth is the same PEP 610 direct_url.json mechanism
# _installed_commit() already uses, read from the *wiki-weaver CLI's own
# venv* (found via its installed script's shebang line -- the standard
# mechanism console-script wrappers use to locate their interpreter).


def _wiki_weaver_cli_interpreter() -> tuple[Optional[str], Optional[str]]:
    """Return (interpreter_path, error) for the venv backing `wiki-weaver` on PATH."""
    exe = shutil.which(_WIKI_WEAVER_CLI_NAME)
    if exe is None:
        return None, "wiki-weaver not found on PATH"
    try:
        with open(exe, encoding="utf-8", errors="replace") as f:
            first_line = f.readline().strip()
    except OSError as e:
        return None, f"could not read wiki-weaver script header: {e}"
    if not first_line.startswith("#!"):
        return (
            None,
            "wiki-weaver is not a shebang script -- cannot locate its venv interpreter",
        )
    return first_line[2:].strip(), None


_PEP610_PROBE = (
    "import importlib.metadata, json, sys\n"
    "try:\n"
    "    d = importlib.metadata.distribution('wiki-weaver')\n"
    "    raw = d.read_text('direct_url.json')\n"
    "    sys.stdout.write(raw or '')\n"
    "except Exception:\n"
    "    sys.exit(1)\n"
)


def _wiki_weaver_cli_commit() -> tuple[Optional[str], Optional[str]]:
    """Read the installed commit SHA of the wiki-weaver CLI-on-PATH.

    Returns ``(commit_or_None, error_or_None)``.  Never raises.
    """
    interpreter, err = _wiki_weaver_cli_interpreter()
    if interpreter is None:
        return None, err

    try:
        r = subprocess.run(
            [interpreter, "-c", _PEP610_PROBE], capture_output=True, text=True
        )  # noqa: S603
    except OSError as e:
        return None, f"could not run wiki-weaver's interpreter: {e}"

    if r.returncode != 0 or not r.stdout.strip():
        return (
            None,
            "could not read wiki-weaver's installed commit (direct_url.json unavailable)",
        )

    try:
        info = json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return None, "wiki-weaver's direct_url.json was not parseable JSON"

    commit = info.get("vcs_info", {}).get("commit_id")
    if not commit:
        return (
            None,
            "wiki-weaver's install has no vcs_info.commit_id (not a git install?)",
        )
    return commit, None


@dataclass
class DriftCheck:
    """Result of comparing repo-weaver's bundled wiki-weaver vs the CLI on PATH."""

    bundled_commit: Optional[str] = None
    """Commit of the wiki-weaver dependency baked into repo-weaver's own install."""
    cli_commit: Optional[str] = None
    """Commit of the standalone wiki-weaver CLI found on PATH."""
    error: Optional[str] = None
    """Set when the CLI-on-PATH commit could not be determined."""

    @property
    def drifted(self) -> Optional[bool]:
        """True/False when both commits are known; None when undetermined."""
        if self.bundled_commit and self.cli_commit:
            return self.bundled_commit != self.cli_commit
        return None


def check_wiki_weaver_drift() -> DriftCheck:
    """Compare repo-weaver's bundled wiki-weaver commit vs the CLI-on-PATH's.

    Local only (PEP 610 reads + one subprocess for the on-PATH interpreter --
    no network), so this stays fast enough for ``doctor``.
    """
    check = DriftCheck()
    check.bundled_commit = _installed_commit(_WIKI_WEAVER_DIST_NAME)
    cli_commit, err = _wiki_weaver_cli_commit()
    check.cli_commit = cli_commit
    if cli_commit is None:
        check.error = err
    return check


# ---------------------------------------------------------------------------
# Top-level orchestration (mirrors wiki_weaver.lib.update / _update_check / _update_real)
# ---------------------------------------------------------------------------


def _print_check_records(records: list[SourceRecord]) -> bool:
    """Print check-mode rows for *records*; return True if any hard error."""
    any_error = False
    for rec in records:
        if rec.error:
            print(f"  ! {rec.label}: {rec.error}")
            any_error = True
        elif rec.needs_update:
            print(
                f"  ! {rec.label}: UPDATE AVAILABLE  "
                f"{rec.local_short} -> {rec.target_short}"
            )
        elif rec.needs_update is False:
            print(f"  \u2713 {rec.label}: up to date ({rec.local_short})")
        else:
            print(f"  ! {rec.label}: unknown (local={rec.local_short} remote=?)")
    return any_error


def update(*, check_only: bool = False) -> int:
    """Refresh repo-weaver's @main sources to latest, then wiki-weaver's.

    Tracks @main, fix-forward -- no SHA pinning (same strategy as wiki-weaver).

    Two stages:
      Stage 1 -- repo-weaver itself: ``uv tool install --reinstall`` with
                 verify+ladder+fail-loud (see :func:`update_layer1`).
      Stage 2 -- wiki-weaver: ``wiki-weaver update`` (subprocess delegation;
                 see :func:`update_wiki_weaver`) -- covers wiki-weaver's own
                 install AND its engine bundle cache in one call.

    ``check_only=True`` -- detect and report without modifying anything.
    """
    if check_only:
        print("Checking repo-weaver's @main sources for drift (no changes made)\u2026")
    else:
        print("Updating repo-weaver to latest @main\u2026")
    overall_error = False

    # --- Stage 1: repo-weaver itself ---
    print()
    print("Stage 1 \u2014 repo-weaver itself:")
    if check_only:
        try:
            records = check_layer1()
            overall_error = _print_check_records(records) or overall_error
        except Exception as e:  # noqa: BLE001
            print(f"  ! check failed: {e}")
            overall_error = True
    else:
        res = update_layer1(verbose=True)
        for name in res.before:
            b = (res.before.get(name) or "?")[:8]
            a = (res.after.get(name) or "?")[:8]
            if b != a:
                print(f"  \u2713 {name}: {b} -> {a}")
            else:
                print(f"  \u2713 {name}: {a} (already at latest)")
        for err in res.errors:
            print(f"  \u2717 error: {err}")
        if res.stale:
            print(
                f"  \u2717 FAIL: after {res.rung_reached} rung(s), {res.stale} still "
                "didn't update. uv is serving a stale cache.  Manual fix:\n"
                f"    uv cache clean repo-weaver && uv tool install --reinstall {INSTALL_URL}"
            )
            overall_error = True
        elif not res.success and res.errors:
            overall_error = True

    # --- Stage 2: wiki-weaver (delegated) ---
    print()
    print("Stage 2 \u2014 wiki-weaver (delegated to `wiki-weaver update`):")
    try:
        rc = update_wiki_weaver(check_only=check_only)
        if rc != 0:
            overall_error = True
    except RuntimeError as e:
        print(f"  \u2717 {e}")
        overall_error = True

    print()
    if overall_error:
        print("\u2717 Completed with errors (see above).")
        print("  Run `repo-weaver doctor` for diagnostics.")
        return 1
    if check_only:
        print("\u2713 Check complete.")
    else:
        print("\u2713 Update complete.")
        print("  Run `repo-weaver doctor` to confirm resolved commits.")
    return 0
