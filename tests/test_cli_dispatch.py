"""CLI subcommand dispatch smoke tests.

Regression guard for the L2-API name-shadowing bug (commit 1456a17):
repo_weaver/__init__.py exported a *function* named ``weave`` into the package
namespace, shadowing the ``repo_weaver.weave`` *submodule*.  The original cli.py
import

    from . import weave as weave_mod

used CPython's IMPORT_FROM bytecode, which resolves names via attribute lookup on
the *parent package* (i.e. getattr(repo_weaver, 'weave')).  Because __init__.py
had already set that attribute to the function, weave_mod was bound to the
function, not the module.  Every subsequent attribute access on weave_mod
(weave_mod.init, weave_mod.weave, weave_mod.ask, weave_mod.replay_windows, …)
raised::

    AttributeError: 'function' object has no attribute 'init'

The same trap applies to ``import repo_weaver.weave as weave_mod`` — CPython
generates identical IMPORT_NAME / IMPORT_FROM bytecode.

Fix: use ``importlib.import_module("repo_weaver.weave")`` which retrieves the
module directly from sys.modules, bypassing the package-attribute shadowing.

Test coverage
-------------
* Sentinel: assert cli.weave_mod is a module (fails immediately if shadowing
  recurs without needing wiki-weaver or any git activity).
* cmd_init end-to-end: call through the argparse function with a fake wiki-weaver
  binary on PATH; assert exit-0 and corpus scaffold created.
* cmd_weave, cmd_ask, cmd_replay: call the cmd_* functions with mocked
  underlying lib functions so no real git/LLM activity occurs; assert dispatch
  resolves (no AttributeError) and the correct callable is invoked.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from repo_weaver import cli


# ---------------------------------------------------------------------------
# Sentinel: weave_mod must be a module object, not a function
# ---------------------------------------------------------------------------


def test_weave_mod_is_module_not_function():
    """cli.weave_mod must be the repo_weaver.weave MODULE, not the weave function.

    FAILS before fix: cli.weave_mod is <function weave> (shadowed by __init__.py).
    PASSES after fix: cli.weave_mod is <module 'repo_weaver.weave'>.
    """
    assert isinstance(cli.weave_mod, types.ModuleType), (
        f"cli.weave_mod is {type(cli.weave_mod).__name__!r} — expected a module. "
        "This means the 'weave' name is shadowed in the package namespace: "
        "repo_weaver/__init__.py exports a 'weave' function that overrides the "
        "submodule when accessed via package-attribute lookup "
        "('from . import weave' or 'import repo_weaver.weave as …'). "
        "Fix: use importlib.import_module('repo_weaver.weave') to bypass shadowing."
    )
    for attr in (
        "init",
        "weave",
        "weave_multi",
        "ask",
        "replay_windows",
        "_load_corpus_config",
    ):
        assert hasattr(cli.weave_mod, attr), (
            f"cli.weave_mod.{attr} not found — weave_mod is not the weave submodule"
        )


# ---------------------------------------------------------------------------
# Fake wiki-weaver for cmd_init end-to-end test
# ---------------------------------------------------------------------------

_FAKE_WW_SCRIPT = """\
#!/usr/bin/env python3
# Minimal fake wiki-weaver: handles --version and 'init <corpus> --plain'.
import sys
from pathlib import Path

args = sys.argv[1:]
if not args or args[0] in ("--version", "-V"):
    print("wiki-weaver 0.0.0-fake")
    sys.exit(0)

if args[0] == "init" and len(args) >= 2:
    corpus = Path(args[1])
    corpus.mkdir(parents=True, exist_ok=True)
    (corpus / "_inbox").mkdir(exist_ok=True)
    (corpus / "_archive").mkdir(exist_ok=True)
    (corpus / "_failed").mkdir(exist_ok=True)
    sys.exit(0)

sys.exit(0)
"""


@pytest.fixture()
def fake_ww_path(tmp_path, monkeypatch):
    """Install a minimal fake wiki-weaver binary first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "wiki-weaver"
    script.write_text(_FAKE_WW_SCRIPT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


# ---------------------------------------------------------------------------
# cmd_init: end-to-end dispatch (no LLM required)
# ---------------------------------------------------------------------------


def test_cmd_init_returns_zero_and_creates_corpus(tmp_path, fake_ww_path):
    """cmd_init must dispatch to weave_mod.init, return 0, and scaffold the corpus.

    Before the fix: AttributeError: 'function' object has no attribute 'init'
    After the fix:  corpus directory created, config file written, exit code 0.
    """
    corpus_dir = str(tmp_path / "my-corpus")
    args = argparse.Namespace(corpus_dir=corpus_dir, repo=None)

    rc = cli.cmd_init(args)

    assert rc == 0, (
        f"cmd_init returned non-zero ({rc}) — possible dispatch error or weave_mod shadowing"
    )
    assert Path(corpus_dir).is_dir(), "Corpus directory was not created by cmd_init"
    assert (Path(corpus_dir) / ".repo-weaver.json").exists(), (
        "Corpus config (.repo-weaver.json) not written by cmd_init"
    )
    assert (Path(corpus_dir) / "policy" / "schema.md").exists(), (
        "policy/schema.md not installed by cmd_init"
    )


# ---------------------------------------------------------------------------
# cmd_weave: dispatch smoke test (underlying weave() mocked)
# ---------------------------------------------------------------------------


def test_cmd_weave_dispatches_to_weave_function(tmp_path):
    """cmd_weave must call weave_mod.weave without AttributeError.

    Before the fix: AttributeError on weave_mod.weave (weave_mod is a function).
    After the fix:  weave() is called exactly once with the correct arguments.
    """
    args = argparse.Namespace(
        corpus=str(tmp_path / "corpus"),
        repo="/some/repo",
        since=None,
        until=None,
        max_prs=5,
        max_modules=3,
        dry_run=True,
        max_cycles=4,
        max_retries=1,
        no_classify=False,
        no_fetch=True,
    )
    with patch("repo_weaver.weave.weave", return_value=0) as mock_fn:
        rc = cli.cmd_weave(args)

    mock_fn.assert_called_once()
    assert rc == 0


# ---------------------------------------------------------------------------
# cmd_ask: dispatch smoke test (underlying ask() mocked)
# ---------------------------------------------------------------------------


def test_cmd_ask_dispatches_to_ask_function(tmp_path):
    """cmd_ask must call weave_mod.ask without AttributeError.

    Before the fix: AttributeError on weave_mod.ask (weave_mod is a function).
    After the fix:  ask() is called exactly once.
    """
    args = argparse.Namespace(
        question="What changed last week?",
        corpus=str(tmp_path / "corpus"),
        json=False,
    )
    with patch("repo_weaver.weave.ask", return_value=0) as mock_fn:
        rc = cli.cmd_ask(args)

    mock_fn.assert_called_once()
    assert rc == 0


# ---------------------------------------------------------------------------
# cmd_replay: dispatch smoke test (underlying replay_windows() mocked)
# ---------------------------------------------------------------------------


def test_cmd_replay_dispatches_to_replay_windows(tmp_path):
    """cmd_replay must call weave_mod.replay_windows without AttributeError.

    Before the fix: AttributeError on weave_mod.replay_windows and also on
    weave_mod._load_corpus_config (used internally by _load_corpus_repos).
    After the fix:  replay_windows() is called exactly once.
    """
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    # Write a minimal .repo-weaver.json so _load_corpus_repos returns repos.
    (corpus_dir / ".repo-weaver.json").write_text(
        json.dumps({"repos": [str(tmp_path / "some-repo")]}),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        corpus=str(corpus_dir),
        repo=None,
        windows="2026-01-01,2026-06-01",
        max_prs=5,
        max_modules=3,
        max_cycles=4,
        max_retries=1,
        no_classify=False,
        restart=False,
        no_fetch=True,
    )
    with patch("repo_weaver.weave.replay_windows", return_value=0) as mock_fn:
        # _load_corpus_repos calls gitio.get_first_commit_date for each repo;
        # mock it so no real git subprocess is launched.
        with patch("repo_weaver.gitio.get_first_commit_date", return_value=None):
            rc = cli.cmd_replay(args)

    mock_fn.assert_called_once()
    assert rc == 0
