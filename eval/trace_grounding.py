"""Grounding tracer for repo-weaver corpus answers.

CLI:
    python -m eval.trace_grounding \\
        --corpus <dir> \\
        --answer <answer.json> \\
        [--json] \\
        [--fail-under <rate>]

PURPOSE
-------
For an answer produced by ``run_questions.py``, this tool extracts checkable
concrete tokens (commit hashes, PR refs, file-counts, version strings,
ISO dates, backtick-quoted identifiers) and traces each against two corpora:

  (a) SOURCE docs  = <corpus>/_archive/*.md  — the ground-truth inputs fed to
                     the wiki materialiser.
  (b) WIKI pages   = <corpus>/*.md           — the synthesised wiki output.

Each token is classified:

  GROUNDED         Token appears (after normalization) in at least one SOURCE doc.
                   Traceable to real input fed to the materialiser.

  SYNTHESIZED_ONLY Token appears in a WIKI page but NOT in any SOURCE doc.
                   The wiki synthesis introduced this exact phrasing — softer
                   signal; may be faithful paraphrase or may have drifted.

  UNGROUNDED       Token appears in NEITHER source nor wiki page.
                   Present only in the ask-time answer — the strongest
                   confabulation candidate.

NORMALIZATION
-------------
Before matching, both the token and the corpus text are passed through a
conservative normalization step so trivially-equivalent forms match:

  - Case-folding (ASCII and Unicode casefold).
  - Smart-quote → ASCII-quote substitution.
  - Leading ``v``/``V`` stripped from version tokens (``v8.0.16`` → ``8.0.16``).
  - Internal whitespace collapsed to a single space.

Only forms that represent the *same fact* are normalized.  Digits and numbers
are never altered so distinct versions (``8.0.16`` vs ``9.9.9``) stay distinct.

HEURISTIC CAVEAT
----------------
Matching is case-insensitive exact-substring after normalization.  A real claim
phrased differently than the source (e.g. "78 passing tests" vs "78 pass") may
show as SYNTHESIZED_ONLY or UNGROUNDED even though the underlying fact is
grounded.  This tool SURFACES candidates for a judge to adjudicate; it does not
convict.  Pair it with the judge for a complete verdict.

EXIT CODES
----------
  0   PASS (or --fail-under not set)
  1   FAIL (grounded_rate < --fail-under threshold)
  2   Usage / IO error
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------

# Nouns that signal a numeric count claim.  Order matters: longer phrases
# before shorter ones so "passing tests" is tried before "tests".
_COUNT_NOUNS = (
    r"passing\s+tests?",
    r"failing\s+tests?",
    r"files?\s+changed",
    r"files?\s+total",
    r"files?",
    r"tests?",
    r"pages?",
    r"PRs?",
    r"commits?",
    r"changes?",
    r"endpoints?",
    r"modules?",
    r"sources?",
    r"collections?",
    r"entries?",
    r"rows?",
)

_COUNT_RE = re.compile(
    r"\b(\d+)\s+(?:" + "|".join(_COUNT_NOUNS) + r")\b",
    re.IGNORECASE,
)

# ISO date: YYYY-MM-DD
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# PR reference: #NN
_PR_RE = re.compile(r"#(\d+)")

# Semantic version (e.g. 8.0.16, 1.2.3) or v-prefixed (e.g. v7, v2.1)
_VERSION_RE = re.compile(r"\bv\d+(?:\.\d+)*\b|\b\d+\.\d+\.\d+\b")

# Backtick-quoted identifier or path  (`routes_lens.py`, `synthesize.dot`)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")

# Commit-like hex hash: 7–40 lowercase hex chars, whole word, not a PR ref
_HEX_RE = re.compile(r"(?<![#])\b([0-9a-f]{7,40})\b")


def _snippet(text: str, start: int, end: int, window: int = 45) -> str:
    """Return a short context snippet around [start, end) in text."""
    lo = max(0, start - window)
    hi = min(len(text), end + window)
    raw = text[lo:hi].replace("\n", " ")
    prefix = "..." if lo > 0 else ""
    suffix = "..." if hi < len(text) else ""
    return f"{prefix}{raw}{suffix}"


def _extract_tokens(text: str) -> list[tuple[str, str, str]]:
    """Extract checkable tokens from answer text.

    Returns a list of (token, category, snippet) tuples.  Deduplicates by
    (token, category) — same token/category pair reported only once.
    """
    seen: set[tuple[str, str]] = set()
    results: list[tuple[str, str, str]] = []

    def _add(token: str, category: str, snip: str) -> None:
        key = (token, category)
        if key not in seen:
            seen.add(key)
            results.append((token, category, snip))

    # 1. Backtick-quoted identifiers/paths (min 3 chars to skip `a`, etc.)
    for m in _BACKTICK_RE.finditer(text):
        inner = m.group(1).strip()
        if len(inner) >= 3:
            _add(inner, "backtick_id", _snippet(text, m.start(), m.end()))

    # 2. ISO dates
    for m in _DATE_RE.finditer(text):
        _add(m.group(1), "iso_date", _snippet(text, m.start(), m.end()))

    # 3. PR references (#NN)
    for m in _PR_RE.finditer(text):
        _add(m.group(0), "pr_ref", _snippet(text, m.start(), m.end()))

    # 4. Version strings
    for m in _VERSION_RE.finditer(text):
        _add(m.group(0), "version", _snippet(text, m.start(), m.end()))

    # 5. Noun counts (number + noun phrase)
    for m in _COUNT_RE.finditer(text):
        phrase = m.group(0).strip()
        _add(phrase, "noun_count", _snippet(text, m.start(), m.end()))

    # 6. Hex commit hashes (7–40 hex chars, not purely numeric, not a year)
    for m in _HEX_RE.finditer(text):
        val = m.group(1).lower()
        # Must have at least one letter (a-f) — pure digit strings are handled
        # by noun_count or pr_ref patterns; plain integers are noise here.
        if not val.isdigit() and re.match(r"^[0-9a-f]+$", val):
            _add(val, "hex_hash", _snippet(text, m.start(), m.end()))

    return results


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


def _load_corpus_texts(corpus: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return (source_docs, wiki_pages) as {filename: text} dicts.

    source_docs : <corpus>/_archive/*.md
    wiki_pages  : <corpus>/*.md  (top-level only, excludes _archive/)
    """
    archive_dir = corpus / "_archive"
    source_docs: dict[str, str] = {}
    if archive_dir.is_dir():
        for p in sorted(archive_dir.glob("*.md")):
            try:
                source_docs[p.name] = p.read_text(encoding="utf-8")
            except OSError as exc:
                print(f"WARNING: cannot read {p}: {exc}", file=sys.stderr)

    wiki_pages: dict[str, str] = {}
    for p in sorted(corpus.glob("*.md")):
        try:
            wiki_pages[p.name] = p.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"WARNING: cannot read {p}: {exc}", file=sys.stderr)

    return source_docs, wiki_pages


# ---------------------------------------------------------------------------
# Normalization for grounding comparison
# ---------------------------------------------------------------------------

# Semver-like pattern: one or more numeric segments separated by dots,
# immediately preceded by a word-boundary + v/V prefix.
# Examples: v8.0.16 → 8.0.16   v2 → 2   V1.2.3 → 1.2.3
_VPFX_RE = re.compile(r"\bv(\d+(?:\.\d+)*)\b", re.IGNORECASE)


def _normalize_for_match(s: str) -> str:
    """Conservative normalization applied to BOTH token and source text.

    Matches trivially-equivalent forms so faithful reformatting is not
    penalized as UNGROUNDED.  Only normalizes forms that represent the
    *same fact* — digits and distinct numbers are never altered.

    Transformations (in order):
      1. Strip leading v/V from semver-like tokens  (v8.0.16 → 8.0.16)
      2. Smart-quote → ASCII-quote substitution
      3. Unicode case-fold (handles é/É, German ß, etc.)
      4. Collapse internal whitespace to a single space
    """
    # 1. v-prefix removal before a digit sequence
    s = _VPFX_RE.sub(r"\1", s)
    # 2. Smart quotes → ASCII
    s = (
        s.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2014", "--")  # em-dash → double-hyphen
        .replace("\u2013", "-")  # en-dash → hyphen
    )
    # 3. Case-fold
    s = s.casefold()
    # 4. Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---------------------------------------------------------------------------
# Per-token classification
# ---------------------------------------------------------------------------


def _classify(
    token: str,
    source_docs: dict[str, str],
    wiki_pages: dict[str, str],
) -> tuple[str, str]:
    """Return (classification, matched_file).

    classification : "GROUNDED" | "SYNTHESIZED_ONLY" | "UNGROUNDED"
    matched_file   : name of the first file the token was found in, or ""

    Matching uses _normalize_for_match() on both the token and the corpus
    text so trivially-equivalent forms (e.g. ``v8.0.16`` vs ``8.0.16``,
    smart-quotes vs ASCII) are treated as the same fact.  Distinct values
    (e.g. ``8.0.16`` vs ``9.9.9``) remain distinct — normalization never
    conflates different numbers.
    """
    norm_token = _normalize_for_match(token)

    # Check source docs first (strongest signal)
    for fname, text in source_docs.items():
        if norm_token in _normalize_for_match(text):
            return "GROUNDED", f"_archive/{fname}"

    # Check wiki pages
    for fname, text in wiki_pages.items():
        if norm_token in _normalize_for_match(text):
            return "SYNTHESIZED_ONLY", fname

    return "UNGROUNDED", ""


# ---------------------------------------------------------------------------
# Navigation-trailer stripping
# ---------------------------------------------------------------------------

# Matches the opening line of a "Pages used / Pages consulted / Pages:" block.
# Supports optional Markdown bold markers (**) around the label.
# Covered forms (case-insensitive):
#   "Pages used: ..."        "Pages consulted: ..."    "Pages: ..."
#   "**Pages used:** ..."    "**Pages consulted:** ..." "**Pages:** ..."
_TRAILER_HEADER_RE = re.compile(
    r"^\*{0,2}\s*pages(?:\s+(?:used|consulted))?\s*\*{0,2}\s*:",
    re.IGNORECASE,
)

# Captures the content after a "Source:" label so we can inspect it.
_SOURCE_NAV_RE = re.compile(
    r"^\*{0,2}\s*source\s*\*{0,2}\s*:\s*(.*)",
    re.IGNORECASE,
)

# Trailing ellipsis ("index.md, overview.md, ...")
_ELLIPSIS_TAIL_RE = re.compile(r"\s*\.\.\.\s*$")


def _is_nav_only_source_line(line: str) -> bool:
    """Return True if *line* is a ``Source:`` citation containing only ``.md`` page names.

    Accepts: ``Source: index.md``, ``Source: [index.md, overview.md]``,
             ``**Source:** [index.md]``.
    Rejects: any line whose post-label content includes prose words that do
    not end in ``.md`` — those are real citations, not navigation metadata.
    """
    m = _SOURCE_NAV_RE.match(line.strip())
    if not m:
        return False
    rest = m.group(1).strip().strip("[]").strip()
    if not rest:
        return True  # bare "Source:" with nothing following
    raw = re.split(r"[,\s]+", rest)
    tokens = [t.strip("[].,") for t in raw if t.strip("[].,")]
    return bool(tokens) and all(t.endswith(".md") for t in tokens)


def _is_page_list_continuation(line: str) -> bool:
    """Return True if *line* looks like a comma-separated list of ``.md`` page names.

    Used to recognise multi-line continuations of a ``Pages used:`` block::

        Pages used:
          index.md, overview.md
          frontend-toolchain.md
    """
    stripped = line.strip()
    if not stripped:
        return False
    # Strip a trailing ellipsis (e.g. "index.md, overview.md, ...")
    stripped = _ELLIPSIS_TAIL_RE.sub("", stripped).strip()
    if not stripped:
        return False
    raw = re.split(r"[,\s]+", stripped)
    tokens = [t.strip("[].,") for t in raw if t.strip("[].,")]
    return bool(tokens) and all(t.endswith(".md") for t in tokens)


def _strip_navigation_trailer(text: str) -> str:
    """Strip trailing navigation-metadata lines from an answer.

    Removes lines that form a recognised navigation trailer at the *end* of
    the answer so they are not analysed for grounding.  The body of the answer
    is never touched.

    Supported trailer forms (all case-insensitive):

    * ``Pages used: overview.md, index.md``
    * ``Pages consulted: index.md, overview.md, ...``
    * ``Pages: index.md``
    * ``**Pages used:** overview.md, index.md``  (Markdown bold label)
    * Multi-line block: ``Pages used:\\n  index.md, overview.md``
    * ``Source: [index.md]``  (pure page-name citation at the end)

    Conservative guarantees:

    * Only lines at the **tail** of the text are inspected.
    * Scanning stops the moment a non-trailer, non-blank line is encountered.
    * If the same label appears mid-answer (body prose), it is **not** stripped.
    * Returns the original text unchanged when no recognised trailer is found.
    """
    lines = text.splitlines()
    if not lines:
        return text

    # ------------------------------------------------------------------
    # Pass 1 — locate a Pages: header block at the tail.
    # Scan backward, accepting: blanks, page-list continuations, Source: nav.
    # Commit (pages_cutoff) as soon as the Pages: header is confirmed.
    # ------------------------------------------------------------------
    pages_cutoff: int | None = None
    i = len(lines) - 1
    while i >= 0:
        stripped = lines[i].strip()
        if not stripped:
            i -= 1
            continue
        if _TRAILER_HEADER_RE.match(stripped):
            pages_cutoff = i
            break  # definitive — stop scanning
        if _is_nav_only_source_line(stripped) or _is_page_list_continuation(stripped):
            i -= 1
            continue
        break  # body content encountered — stop

    if pages_cutoff is not None:
        result = "\n".join(lines[:pages_cutoff])
        return result.rstrip()

    # ------------------------------------------------------------------
    # Pass 2 — look for a standalone Source: nav line at the tail.
    # Only strip if ALL non-blank trailing lines are Source: pure-nav.
    # ------------------------------------------------------------------
    source_cutoff: int | None = None
    i = len(lines) - 1
    while i >= 0:
        stripped = lines[i].strip()
        if not stripped:
            i -= 1
            continue
        if _is_nav_only_source_line(stripped):
            source_cutoff = i
            i -= 1
            continue
        break  # non-source content

    if source_cutoff is not None:
        result = "\n".join(lines[:source_cutoff])
        return result.rstrip()

    return text


# ---------------------------------------------------------------------------
# Core trace function
# ---------------------------------------------------------------------------


def trace(corpus_dir: str, answer_path: str) -> dict:
    """Run the grounding trace for a single answer file.

    Returns a result dict with keys:
        id, question, corpus, source_doc_count, wiki_page_count,
        tokens (list of per-token dicts),
        counts (GROUNDED / SYNTHESIZED_ONLY / UNGROUNDED),
        grounded_rate (float),
        ungrounded_tokens (list of token dicts for UNGROUNDED only)

    Raises SystemExit(2) on IO/parse errors.
    """
    corpus = Path(corpus_dir).resolve()
    if not corpus.is_dir():
        print(f"ERROR: corpus directory not found: {corpus}", file=sys.stderr)
        raise SystemExit(2)

    ans_path = Path(answer_path).resolve()
    if not ans_path.is_file():
        print(f"ERROR: answer file not found: {ans_path}", file=sys.stderr)
        raise SystemExit(2)

    try:
        answer_json = json.loads(ans_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot parse {ans_path}: {exc}", file=sys.stderr)
        raise SystemExit(2)

    answer_text: str = answer_json.get("answer") or ""
    qid: str = answer_json.get("id", ans_path.stem)
    question: str = answer_json.get("question", "")

    if not answer_text:
        print(
            f"WARNING: answer field is empty in {ans_path} — no tokens to trace",
            file=sys.stderr,
        )

    # Strip trailing navigation-metadata lines (Pages used:, Pages consulted:,
    # Source: [.md refs]) before tokenising so wiki-layer filenames are never
    # analysed as factual claims.  Body prose is never touched.
    answer_text = _strip_navigation_trailer(answer_text)

    source_docs, wiki_pages = _load_corpus_texts(corpus)
    raw_tokens = _extract_tokens(answer_text)

    token_results: list[dict] = []
    counts: dict[str, int] = {"GROUNDED": 0, "SYNTHESIZED_ONLY": 0, "UNGROUNDED": 0}

    for token, category, snippet in raw_tokens:
        classification, found_in = _classify(token, source_docs, wiki_pages)
        counts[classification] += 1
        token_results.append(
            {
                "token": token,
                "category": category,
                "classification": classification,
                "found_in": found_in,
                "snippet": snippet,
            }
        )

    total = sum(counts.values())
    grounded_rate = counts["GROUNDED"] / total if total > 0 else 0.0

    ungrounded = [t for t in token_results if t["classification"] == "UNGROUNDED"]

    return {
        "id": qid,
        "question": question,
        "corpus": str(corpus),
        "source_doc_count": len(source_docs),
        "wiki_page_count": len(wiki_pages),
        "tokens": token_results,
        "counts": counts,
        "total_tokens": total,
        "grounded_rate": round(grounded_rate, 4),
        "ungrounded_tokens": ungrounded,
    }


# ---------------------------------------------------------------------------
# Human-readable output
# ---------------------------------------------------------------------------


def _print_table(result: dict) -> None:
    """Print a per-token trace table followed by summary to stdout."""
    tokens = result["tokens"]
    qid = result["id"]
    question = result["question"]
    grounded_rate = result["grounded_rate"]
    counts = result["counts"]
    total = result["total_tokens"]

    print(f"Grounding Trace: {qid}")
    print("=" * 70)
    if question:
        # Wrap at 68 chars
        q_wrapped = (question[:65] + "...") if len(question) > 68 else question
        print(f"Question : {q_wrapped}")
    print(f"Corpus   : {result['corpus']}")
    print(
        f"Sources  : {result['source_doc_count']} source doc(s) in _archive/,"
        f" {result['wiki_page_count']} wiki page(s)"
    )
    print()

    if not tokens:
        print("(no checkable tokens found in answer)")
        print()
        return

    # Column widths
    tok_w = max(max(len(t["token"]) for t in tokens), len("Token")) + 1
    tok_w = min(tok_w, 46)  # cap for readability
    cat_w = max(max(len(t["category"]) for t in tokens), len("Category")) + 1
    cat_w = min(cat_w, 16)
    cls_w = len("SYNTHESIZED_ONLY") + 1
    found_w = max(max(len(t["found_in"]) for t in tokens), len("Found-in")) + 1
    found_w = min(found_w, 48)

    header = (
        f"  {'Token':<{tok_w}}  {'Category':<{cat_w}}  "
        f"{'Classification':<{cls_w}}  {'Found-in'}"
    )
    sep = "  " + "-" * (tok_w + 2 + cat_w + 2 + cls_w + 2 + found_w)
    print(header)
    print(sep)

    for t in tokens:
        tok_disp = t["token"]
        if len(tok_disp) > tok_w - 1:
            tok_disp = tok_disp[: tok_w - 4] + "..."
        found_disp = t["found_in"] if t["found_in"] else "-"
        if len(found_disp) > found_w - 1:
            found_disp = "..." + found_disp[-(found_w - 4) :]
        print(
            f"  {tok_disp:<{tok_w}}  {t['category']:<{cat_w}}  "
            f"{t['classification']:<{cls_w}}  {found_disp}"
        )

    print()
    print("Summary")
    print("-------")
    for cls in ("GROUNDED", "SYNTHESIZED_ONLY", "UNGROUNDED"):
        n = counts[cls]
        pct = f"{100 * n / total:.1f}%" if total else "n/a"
        print(f"  {cls:<18}  {n:>3}  ({pct})")
    print(f"  {'Total checkable':<18}  {total:>3}")
    print()
    print(f"grounded_rate = {grounded_rate:.4f}  ({counts['GROUNDED']}/{total})")
    print()

    ungrounded = result["ungrounded_tokens"]
    if ungrounded:
        print("UNGROUNDED tokens — confabulation candidates (adjudicate with judge):")
        print()
        for t in ungrounded:
            print(f"  {t['token']!r:40s}  [{t['category']}]")
            print(f"    snippet: {t['snippet']!r}")
        print()
        print(
            "NOTE: This is a heuristic signal — exact-substring matching.\n"
            "      A real claim phrased differently than the source may appear\n"
            "      UNGROUNDED or SYNTHESIZED_ONLY. Pair with the judge for a\n"
            "      complete verdict; this tool surfaces candidates, not convictions."
        )
    else:
        print("No UNGROUNDED tokens detected.")
    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.trace_grounding",
        description=(
            "Trace concrete claims in a repo-weaver answer back to corpus source docs.\n\n"
            "Classifies each checkable token as GROUNDED (found in _archive/*.md),\n"
            "SYNTHESIZED_ONLY (found in a wiki page but not a source doc), or\n"
            "UNGROUNDED (found in neither — confabulation candidate).\n\n"
            "HEURISTIC CAVEAT: matching is exact-substring; a real claim phrased\n"
            "differently than the source may show as SYNTHESIZED_ONLY or UNGROUNDED.\n"
            "This tool surfaces candidates — pair it with a judge for full verdicts."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--corpus",
        required=True,
        metavar="DIR",
        help=(
            "Path to the wiki corpus directory.  Source docs are read from "
            "<corpus>/_archive/*.md; wiki pages from <corpus>/*.md."
        ),
    )
    parser.add_argument(
        "--answer",
        required=True,
        metavar="FILE",
        help=(
            "Path to a single answer JSON file written by run_questions.py "
            "({id, question, answer, pages_used, ...})."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_out",
        help="Emit a JSON result object instead of a human-readable table.",
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=None,
        metavar="RATE",
        dest="fail_under",
        help=(
            "Exit 1 if grounded_rate < RATE (e.g. --fail-under 0.5). "
            "Exit 0 otherwise.  RATE must be in [0.0, 1.0]."
        ),
    )
    args = parser.parse_args(argv)

    if args.fail_under is not None and not (0.0 <= args.fail_under <= 1.0):
        print(
            "ERROR: --fail-under must be in [0.0, 1.0]",
            file=sys.stderr,
        )
        return 2

    result = trace(
        corpus_dir=args.corpus,
        answer_path=args.answer,
    )

    if args.json_out:
        print(json.dumps(result, indent=2))
    else:
        _print_table(result)

    if args.fail_under is not None:
        if result["grounded_rate"] < args.fail_under:
            if not args.json_out:
                print(
                    f"FAIL: grounded_rate {result['grounded_rate']:.4f} "
                    f"< threshold {args.fail_under:.4f}",
                    file=sys.stderr,
                )
            return 1
        if not args.json_out:
            print(
                f"PASS: grounded_rate {result['grounded_rate']:.4f} "
                f">= threshold {args.fail_under:.4f}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
