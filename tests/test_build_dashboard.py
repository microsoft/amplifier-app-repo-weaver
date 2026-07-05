"""Tests for the build-dashboard subcommand.

Verifies:
- cmd_build_dashboard assembles the correct wiki-weaver argv
  (--group-by repos + GitHub group-link-template)
- Missing / old wiki-weaver (no build-dashboard subcommand) is rejected cleanly
- Corpus theme is seeded idempotently from the packaged default
- Parser accepts and rejects expected arguments
"""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from repo_weaver import cli


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_FAKE_WW_OK = """\
#!/usr/bin/env python3
# Fake wiki-weaver that succeeds for any subcommand.
import sys
sys.exit(0)
"""

_FAKE_WW_NO_DASHBOARD = """\
#!/usr/bin/env python3
# Fake wiki-weaver that rejects 'build-dashboard'.
import sys
args = sys.argv[1:]
if args and args[0] == "build-dashboard":
    sys.exit(2)      # subcommand unknown
sys.exit(0)
"""


def _write_fake_ww(
    tmp_path: Path, script: str, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Install a fake wiki-weaver binary at the front of PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / "wiki-weaver"
    binary.write_text(script, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return binary


# ---------------------------------------------------------------------------
# Argv assembly: the critical domain-policy assertion
# ---------------------------------------------------------------------------


def test_build_dashboard_assembles_correct_argv(tmp_path, monkeypatch):
    """cmd_build_dashboard must call wiki-weaver with:
      build-dashboard <corpus> --out <PATH>
      --group-by repos
      --group-link-template 'https://github.com/{group}'

    The GitHub group-link-template is repo-weaver's ONLY domain-specific
    policy contribution — this test is the primary contract assertion.
    """
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    out_file = tmp_path / "dash.html"

    args = argparse.Namespace(corpus=str(corpus_dir), out=str(out_file), theme=None)

    captured: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs) -> MagicMock:
        captured.append(argv)
        mock = MagicMock()
        mock.returncode = 0
        return mock

    # Patch subprocess.run: first call is the probe, second is the real build.
    with patch("repo_weaver.cli.subprocess.run", side_effect=fake_run):
        rc = cli.cmd_build_dashboard(args)

    assert rc == 0, f"expected exit 0, got {rc}"

    # Two subprocess.run calls: [probe-help, real-build]
    assert len(captured) == 2, f"expected 2 subprocess.run calls, got {len(captured)}"

    probe_argv = captured[0]
    build_argv = captured[1]

    # Probe must check build-dashboard --help
    assert probe_argv == ["wiki-weaver", "build-dashboard", "--help"]

    # Build argv must include all required flags.
    assert build_argv[0] == "wiki-weaver"
    assert build_argv[1] == "build-dashboard"
    assert str(corpus_dir) in build_argv
    assert "--out" in build_argv
    assert str(out_file) in build_argv
    assert "--group-by" in build_argv
    gi = build_argv.index("--group-by")
    assert build_argv[gi + 1] == "repos", (
        f"--group-by value must be 'repos', got {build_argv[gi + 1]!r}"
    )
    assert "--group-link-template" in build_argv
    ti = build_argv.index("--group-link-template")
    assert build_argv[ti + 1] == "https://github.com/{group}", (
        f"--group-link-template must be 'https://github.com/{{group}}', "
        f"got {build_argv[ti + 1]!r}"
    )

    # --theme must NOT appear when not supplied
    assert "--theme" not in build_argv


def test_build_dashboard_passes_theme_when_supplied(tmp_path, monkeypatch):
    """When --theme PATH is passed, it must be forwarded to wiki-weaver."""
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    theme_file = tmp_path / "my-theme.json"
    theme_file.write_text("{}", encoding="utf-8")
    out_file = tmp_path / "dash.html"

    args = argparse.Namespace(
        corpus=str(corpus_dir), out=str(out_file), theme=str(theme_file)
    )

    captured: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs) -> MagicMock:
        captured.append(argv)
        mock = MagicMock()
        mock.returncode = 0
        return mock

    with patch("repo_weaver.cli.subprocess.run", side_effect=fake_run):
        rc = cli.cmd_build_dashboard(args)

    assert rc == 0
    build_argv = captured[1]
    assert "--theme" in build_argv
    ti = build_argv.index("--theme")
    assert build_argv[ti + 1] == str(theme_file)


# ---------------------------------------------------------------------------
# Missing / old wiki-weaver detection
# ---------------------------------------------------------------------------


def test_build_dashboard_rejects_old_wiki_weaver(tmp_path, monkeypatch, capsys):
    """If wiki-weaver doesn't support build-dashboard (probe exits non-zero),
    cmd_build_dashboard must return non-zero and print a clear error."""
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    args = argparse.Namespace(
        corpus=str(corpus_dir), out=str(tmp_path / "dash.html"), theme=None
    )

    def fake_run(argv: list[str], **_kwargs) -> MagicMock:
        mock = MagicMock()
        mock.returncode = 2  # simulate old wiki-weaver rejecting build-dashboard
        return mock

    with patch("repo_weaver.cli.subprocess.run", side_effect=fake_run):
        rc = cli.cmd_build_dashboard(args)

    assert rc != 0, "should fail when wiki-weaver lacks build-dashboard"
    captured = capsys.readouterr()
    assert (
        "build-dashboard" in captured.err.lower()
        or "wiki-weaver" in captured.err.lower()
    )


# ---------------------------------------------------------------------------
# Default theme seeding
# ---------------------------------------------------------------------------


def test_ensure_corpus_theme_writes_default_when_absent(tmp_path):
    """_ensure_corpus_theme must write the packaged default into an empty corpus."""
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    from wiki_weaver.lib import wiki_dashboard
    theme_dst = wiki_dashboard(corpus_dir) / "theme.json"

    assert not theme_dst.exists(), "precondition: no theme yet"
    cli._ensure_corpus_theme(str(corpus_dir))
    assert theme_dst.exists(), "theme.json should be written by _ensure_corpus_theme"

    import json

    data = json.loads(theme_dst.read_text(encoding="utf-8"))
    assert data.get("title") == "Repo Weaver", (
        f"default theme must have title='Repo Weaver', got {data.get('title')!r}"
    )
    assert "--wiki-accent" in data, "default theme must include --wiki-accent token"


def test_ensure_corpus_theme_does_not_clobber_existing(tmp_path):
    """_ensure_corpus_theme must not overwrite a user's existing theme.json."""
    from wiki_weaver.lib import wiki_dashboard
    corpus_dir = tmp_path / "corpus"
    dash_dir = wiki_dashboard(corpus_dir)
    dash_dir.mkdir(parents=True)
    existing_theme = dash_dir / "theme.json"
    existing_theme.write_text('{"title": "My Custom Title"}', encoding="utf-8")

    cli._ensure_corpus_theme(str(corpus_dir))

    content = existing_theme.read_text(encoding="utf-8")
    assert "My Custom Title" in content, "existing theme.json must not be overwritten"


# ---------------------------------------------------------------------------
# Parser smoke test
# ---------------------------------------------------------------------------


def test_build_dashboard_parser_requires_out(tmp_path):
    """The build-dashboard subcommand must require --out."""
    parser = cli._build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["build-dashboard", str(tmp_path)])
    assert exc_info.value.code != 0


def test_build_dashboard_parser_accepts_valid_args(tmp_path):
    """build-dashboard parser must accept corpus + --out (and optional --theme)."""
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    out = str(tmp_path / "out.html")
    theme = str(tmp_path / "theme.json")

    parser = cli._build_parser()
    args = parser.parse_args(
        ["build-dashboard", str(corpus_dir), "--out", out, "--theme", theme]
    )
    assert args.corpus == str(corpus_dir)
    assert args.out == out
    assert args.theme == theme


def test_build_dashboard_parser_theme_defaults_to_none(tmp_path):
    """--theme must default to None when omitted."""
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    out = str(tmp_path / "out.html")

    parser = cli._build_parser()
    args = parser.parse_args(["build-dashboard", str(corpus_dir), "--out", out])
    assert args.theme is None
