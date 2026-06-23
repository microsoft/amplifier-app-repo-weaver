"""Deterministic corpus integrity gate for repo-weaver.

Regression gate for the "silent skip on filename collision" bug: a changed
module snapshot that shared a filename with an already-archived source could
be registered in .sources.json (ingested=False) and then silently skipped,
leaving ingested=False with no ledger entry.

CLI:
    python -m eval.coverage_check --corpus <dir> [--json]

PASS iff ALL of:
  1. Every source in .sources.json has ingested==True.
  2. Every source in .sources.json has a matching entry in .processed.jsonl
     where converged==True or status in ("success", "converged").
  3. The _failed/ directory is empty (or absent).

Exit codes:
  0  PASS
  1  FAIL  (integrity violations found)
  2  Usage / IO error
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def _read_registry(corpus: Path) -> list[dict]:
    """Return the list of source entries from .sources.json.

    Returns an empty list if the file is absent (fresh corpus — not an error).
    Raises SystemExit(2) on parse errors.
    """
    reg_path = corpus / ".sources.json"
    if not reg_path.exists():
        return []
    try:
        raw = reg_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot read .sources.json: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: .sources.json is not valid JSON: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not isinstance(data, dict):
        print("ERROR: .sources.json root must be a JSON object", file=sys.stderr)
        raise SystemExit(2)
    return list(data.get("sources", []))


def _read_ledger(corpus: Path) -> dict[str, list[dict]]:
    """Return a dict mapping source filename -> list of ledger rows.

    Reads .processed.jsonl (one JSON object per line).  Returns an empty dict
    if the file is absent.  Raises SystemExit(2) on parse errors.
    """
    ledger_path = corpus / ".processed.jsonl"
    if not ledger_path.exists():
        return {}
    try:
        text = ledger_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot read .processed.jsonl: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    result: dict[str, list[dict]] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            print(
                f"ERROR: .processed.jsonl line {lineno} is not valid JSON: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(2) from exc
        src = row.get("source", "")
        result.setdefault(src, []).append(row)
    return result


def _list_failed(corpus: Path) -> list[str]:
    """Return filenames present in _failed/ (empty list if directory absent)."""
    failed_dir = corpus / "_failed"
    if not failed_dir.exists():
        return []
    return sorted(p.name for p in failed_dir.iterdir() if p.is_file())


def _ledger_row_ok(rows: list[dict]) -> bool:
    """Return True if at least one ledger row shows successful convergence."""
    for row in rows:
        if row.get("converged") is True:
            return True
        status = row.get("status", "")
        if isinstance(status, str) and status.lower() in ("success", "converged"):
            return True
    return False


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------


def check(corpus_dir: str) -> dict:
    """Run the integrity check against a corpus directory.

    Returns a result dict::

        {
            "pass": bool,
            "corpus": str,           # resolved absolute path
            "total_registered": int, # sources in .sources.json
            "failures": [{"filename": str, "reason": str}, ...]
        }

    Raises SystemExit(2) on IO/parse errors.
    """
    corpus = Path(corpus_dir).resolve()
    if not corpus.is_dir():
        print(f"ERROR: corpus directory not found: {corpus}", file=sys.stderr)
        raise SystemExit(2)

    registry = _read_registry(corpus)
    ledger = _read_ledger(corpus)
    failed_files = _list_failed(corpus)

    failures: list[dict] = []

    for entry in registry:
        filename = entry.get("filename", "<unknown>")
        ingested = entry.get("ingested", False)

        if not ingested:
            failures.append(
                {"filename": filename, "reason": "ingested=False (unprocessed source)"}
            )
            continue  # no point checking ledger for an un-ingested source

        # ingested=True but nothing in the ledger → silent skip bug
        ledger_rows = ledger.get(filename, [])
        if not ledger_rows:
            failures.append(
                {
                    "filename": filename,
                    "reason": "in registry but not in ledger (silent skip)",
                }
            )
            continue

        # ledger entry exists but none converged/succeeded
        if not _ledger_row_ok(ledger_rows):
            statuses = [r.get("status", "?") for r in ledger_rows]
            failures.append(
                {
                    "filename": filename,
                    "reason": f"ledger entry not converged (status={statuses})",
                }
            )

    # Anything in _failed/ is a hard failure
    for fname in failed_files:
        failures.append({"filename": fname, "reason": "present in _failed/"})

    return {
        "pass": len(failures) == 0,
        "corpus": str(corpus),
        "total_registered": len(registry),
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Human-readable output
# ---------------------------------------------------------------------------


def _print_table(failures: list[dict]) -> None:
    """Print a two-column failure table to stdout."""
    if not failures:
        return
    col_w = max(len(f["filename"]) for f in failures)
    col_w = max(col_w, len("Source")) + 2
    reason_w = max(len(f["reason"]) for f in failures)
    reason_w = max(reason_w, len("Reason"))

    header = f"  {'Source':<{col_w}}  {'Reason':<{reason_w}}"
    sep = "  " + "-" * (col_w + 2 + reason_w)
    print(header)
    print(sep)
    for f in failures:
        print(f"  {f['filename']:<{col_w}}  {f['reason']}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.coverage_check",
        description=(
            "Deterministic integrity gate for a repo-weaver corpus.  "
            "Run after each ingest window.  Exit 0 = PASS, 1 = FAIL."
        ),
    )
    parser.add_argument(
        "--corpus",
        required=True,
        metavar="DIR",
        help="Path to the wiki corpus directory.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_out",
        help="Emit a JSON result object instead of a human-readable table.",
    )
    args = parser.parse_args(argv)

    result = check(args.corpus)
    passed: bool = result["pass"]
    failures: list[dict] = result["failures"]

    if args.json_out:
        print(json.dumps(result, indent=2))
    else:
        corpus_label = args.corpus
        total = result["total_registered"]
        if passed:
            print(
                f"PASS  corpus={corpus_label}  "
                f"({total} source(s) registered, all converged, _failed/ empty)"
            )
        else:
            print(
                f"FAIL  corpus={corpus_label}  "
                f"({len(failures)} issue(s) out of {total} registered source(s))\n"
            )
            _print_table(failures)
            print()

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
