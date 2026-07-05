"""Tests for the retry-resilience layer in repo_weaver.weave.

Three deterministic scenarios, each using a fake ``wiki-weaver`` executable
on PATH (same technique as the timeout tests in eval/).  No network traffic,
no real provider calls.

Scenarios:
  (a) test_transient_retry_eventually_succeeds
      Source fails with overloaded_error K times, converges on attempt K+1.
      Asserts: returns 0, source is out of _failed/, exponential back-off applied.

  (b) test_exhausted_retries_exits_nonzero
      Source always fails.  After max_retries the function returns non-zero
      with the source still in _failed/.

  (c) test_not_converged_bumps_max_cycles
      Source emits a "cycle cap" error.  Asserts that each successive retry
      passes an increased --max-cycles value (verified from the fake's call log).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from wiki_weaver.lib import wiki_failed, wiki_inbox, wiki_ledger, wiki_sources

from repo_weaver.weave import (
    _DEFAULT_CYCLES_BUMP,
    _DEFAULT_MAX_CYCLES,
    _retry_failed_sources,
)

# ---------------------------------------------------------------------------
# Fake wiki-weaver script
#
# Reads .fake-ww-config.json from the corpus dir to control behavior:
#   {
#       "behavior": "transient" | "not_converged",
#       "fail_limit": N,   # for "transient": fail first N calls per source
#   }
#
# Appends one JSON record per call to .fake-ww-calls.jsonl:
#   {"source": name, "max_cycles": N, "call_num": N, "behavior": str}
#
# File-system effects (mirrors real wiki-weaver ingest --source behavior):
#   transient failure → moves source from _inbox/ to _failed/
#   not_converged     → moves source from _inbox/ to _failed/
#   success           → moves source from _inbox/ to _archive/,
#                       writes pages/<name>.page.md
# ---------------------------------------------------------------------------

_FAKE_WW_SCRIPT = """\
#!/usr/bin/env python3
# Fake wiki-weaver for repo-weaver retry tests (stdlib only).
import json
import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("--version", "-V"):
        print("wiki-weaver 0.0.0-fake")
        return 0

    if args[0] != "ingest":
        return 0

    wiki = None
    source = None
    max_cycles = 4
    i = 1
    while i < len(args):
        if args[i] == "--wiki" and i + 1 < len(args):
            wiki = args[i + 1]
            i += 2
        elif args[i] == "--source" and i + 1 < len(args):
            source = args[i + 1]
            i += 2
        elif args[i] == "--max-cycles" and i + 1 < len(args):
            max_cycles = int(args[i + 1])
            i += 2
        else:
            i += 1

    if not wiki:
        print("ERROR: --wiki required", file=sys.stderr)
        return 1

    corpus = Path(wiki)
    failed_dir = corpus / ".wiki" / "failed"
    inbox = corpus / "_inbox"
    archive_dir = corpus / "_sources"
    (corpus / ".wiki").mkdir(exist_ok=True)
    failed_dir.mkdir(exist_ok=True)
    archive_dir.mkdir(exist_ok=True)

    config_path = corpus / ".fake-ww-config.json"
    config = (
        json.loads(config_path.read_text(encoding="utf-8"))
        if config_path.exists()
        else {}
    )

    behavior = config.get("behavior", "succeed")
    fail_limit = int(config.get("fail_limit", 0))

    if source:
        src_path = inbox / source
        paths = [src_path] if src_path.exists() else []
    else:
        paths = sorted(inbox.glob("*.md"))

    call_log_path = corpus / ".fake-ww-calls.jsonl"

    for src_path in paths:
        name = src_path.name
        call_key = "calls:" + name
        call_num = int(config.get(call_key, 0)) + 1
        config[call_key] = call_num

        with open(call_log_path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({
                    "source": name,
                    "max_cycles": max_cycles,
                    "call_num": call_num,
                    "behavior": behavior,
                })
                + "\\n"
            )

        if behavior == "not_converged":
            print(
                f"ERROR: cycle cap reached -- max_cycles exceeded for {name}",
                file=sys.stderr,
            )
            if src_path.exists():
                src_path.rename(failed_dir / name)
        elif behavior == "transient" and call_num <= fail_limit:
            print(
                f"ERROR: overloaded_error -- provider overloaded processing {name}",
                file=sys.stderr,
            )
            if src_path.exists():
                src_path.rename(failed_dir / name)
        else:
            # Success path
            if src_path.exists():
                src_path.rename(archive_dir / name)
            pages_dir = corpus / "pages"
            pages_dir.mkdir(exist_ok=True)
            (pages_dir / (name + ".page.md")).write_text(
                f"# Page for {name}\\n", encoding="utf-8"
            )

    config_path.write_text(json.dumps(config), encoding="utf-8")
    return 0


sys.exit(main())
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SOURCE_NAME = "2026-01-01-digest.md"


def _install_fake_ww(bin_dir: Path) -> None:
    """Write the fake wiki-weaver script and make it executable."""
    script = bin_dir / "wiki-weaver"
    script.write_text(_FAKE_WW_SCRIPT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _setup_corpus(
    tmp_path: Path,
    *,
    source_name: str = _SOURCE_NAME,
    ledger_entry: dict | None = None,
) -> Path:
    """Create a minimal corpus with the source already in _failed/.

    Simulates the state after a first ``wiki-weaver ingest`` that failed for
    one source — which is exactly where ``_retry_failed_sources`` picks up.
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    wiki_inbox(corpus).mkdir(parents=True, exist_ok=True)
    wiki_failed(corpus).mkdir(parents=True, exist_ok=True)
    wiki_sources(corpus).mkdir(parents=True, exist_ok=True)
    (wiki_failed(corpus) / source_name).write_text(
        "# test source content\n", encoding="utf-8"
    )
    if ledger_entry is not None:
        wiki_ledger(corpus).parent.mkdir(parents=True, exist_ok=True)
        wiki_ledger(corpus).write_text(
            json.dumps(ledger_entry) + "\n", encoding="utf-8"
        )
    return corpus


def _configure_fake(corpus: Path, behavior: str, fail_limit: int = 0) -> None:
    (corpus / ".fake-ww-config.json").write_text(
        json.dumps({"behavior": behavior, "fail_limit": fail_limit}),
        encoding="utf-8",
    )


def _read_call_log(corpus: Path) -> list[dict]:
    log = corpus / ".fake-ww-calls.jsonl"
    if not log.exists():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Fixture: inject fake wiki-weaver onto PATH for the duration of each test
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_ww_env(tmp_path):
    """Put a temp bin dir containing the fake wiki-weaver first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_fake_ww(bin_dir)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
    yield bin_dir
    os.environ["PATH"] = old_path


# ---------------------------------------------------------------------------
# (a) Transient failure → eventually succeeds
# ---------------------------------------------------------------------------


def test_transient_retry_eventually_succeeds(tmp_path, fake_ww_env):
    """Source fails with overloaded_error twice; converges on the third attempt.

    Verifies:
    - _retry_failed_sources returns 0 (success).
    - Source is no longer in _failed/.
    - Source is moved to _archive/ by the fake.
    - Exponential back-off was applied (three sleep calls recorded).
    """
    corpus = _setup_corpus(tmp_path)
    # fail_limit=2: first 2 calls fail (transient), 3rd call succeeds.
    _configure_fake(corpus, behavior="transient", fail_limit=2)

    slept: list[float] = []
    rc = _retry_failed_sources(
        corpus=str(corpus),
        max_retries=3,
        max_cycles=_DEFAULT_MAX_CYCLES,
        retry_base_delay=1.0,
        _sleep=slept.append,
    )

    assert rc == 0, f"Expected 0, got {rc}"
    failed_files = list(wiki_failed(corpus).iterdir())
    assert failed_files == [], f"_failed/ should be empty, found: {failed_files}"
    assert (wiki_sources(corpus) / _SOURCE_NAME).exists(), (
        "Source should be archived after successful retry"
    )
    # Three retry attempts: each with backoff (1.0, 2.0, 4.0).
    assert len(slept) == 3, f"Expected 3 sleep calls, got {len(slept)}: {slept}"
    assert slept == [1.0, 2.0, 4.0], f"Unexpected back-off sequence: {slept}"


# ---------------------------------------------------------------------------
# (b) Always fails → exhausted retries, non-zero exit, named summary
# ---------------------------------------------------------------------------


def test_exhausted_retries_exits_nonzero(tmp_path, fake_ww_env):
    """Source never succeeds; after max_retries the function returns non-zero.

    Verifies:
    - _retry_failed_sources returns non-zero.
    - Source remains in _failed/.
    - Exactly max_retries call attempts were made.
    """
    corpus = _setup_corpus(tmp_path)
    # fail_limit=999: never succeeds.
    _configure_fake(corpus, behavior="transient", fail_limit=999)

    max_retries = 2
    rc = _retry_failed_sources(
        corpus=str(corpus),
        max_retries=max_retries,
        max_cycles=_DEFAULT_MAX_CYCLES,
        retry_base_delay=0.0,  # no real sleep
        _sleep=lambda _: None,
    )

    assert rc != 0, "Expected non-zero exit when all retries exhausted"
    failed_files = list(wiki_failed(corpus).iterdir())
    assert len(failed_files) == 1, (
        f"Source should remain in .wiki/failed/, found: {failed_files}"
    )
    assert failed_files[0].name == _SOURCE_NAME

    calls = _read_call_log(corpus)
    assert len(calls) == max_retries, (
        f"Expected exactly {max_retries} retry calls, got {len(calls)}"
    )


# ---------------------------------------------------------------------------
# (c) NOT-CONVERGED → each retry bumps --max-cycles
# ---------------------------------------------------------------------------


def test_not_converged_bumps_max_cycles(tmp_path, fake_ww_env):
    """Source fails with 'cycle cap reached'; retry passes increasing --max-cycles.

    The .processed.jsonl ledger is pre-seeded with a not_converged entry so
    that the FIRST retry is already classified correctly (before any captured
    per-source output exists).

    Verifies:
    - First retry uses the initial max_cycles value.
    - Second retry uses max_cycles + _DEFAULT_CYCLES_BUMP.
    - Function returns non-zero (fake always fails with not_converged).
    - Source remains in _failed/.
    """
    ledger_entry = {
        "source": _SOURCE_NAME,
        "status": "failed",
        "error": "cycle cap reached -- max_cycles exceeded",
    }
    corpus = _setup_corpus(tmp_path, ledger_entry=ledger_entry)
    _configure_fake(corpus, behavior="not_converged")

    max_cycles = 4
    max_retries = 2
    rc = _retry_failed_sources(
        corpus=str(corpus),
        max_retries=max_retries,
        max_cycles=max_cycles,
        retry_base_delay=0.0,
        _sleep=lambda _: None,
    )

    assert rc != 0, "Expected non-zero (always fails with not_converged)"
    assert (wiki_failed(corpus) / _SOURCE_NAME).exists(), (
        "Source should still be in .wiki/failed/"
    )

    calls = _read_call_log(corpus)
    assert len(calls) == max_retries, (
        f"Expected {max_retries} call(s) in call log, got {len(calls)}: {calls}"
    )

    expected_cycles = [
        max_cycles + i * _DEFAULT_CYCLES_BUMP for i in range(max_retries)
    ]
    actual_cycles = [c["max_cycles"] for c in calls]
    assert actual_cycles == expected_cycles, (
        f"max_cycles not bumped correctly: expected {expected_cycles}, got {actual_cycles}"
    )
