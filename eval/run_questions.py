"""Run the eval question set against a built corpus and save raw outputs.

Grading is done by a downstream judge agent — this script only collects answers.

CLI:
    python -m eval.run_questions \\
        --corpus  <corpus-dir>          \\
        --questions eval/questions.yaml \\
        --out     <output-dir>

For each question the script:
  1. Calls ``repo-weaver ask "<question>" --corpus <corpus> --json`` via subprocess.
  2. Extracts the JSON payload from stdout (skipping any preceding log lines).
  3. Writes ``<out>/answers/<id>.json`` with the full record.
  4. Writes ``<out>/answers.index.json`` once all questions are done.

Exit 0 if every question was attempted (grading is separate).
Exit 1 if the subprocess could not be launched at all.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Minimal YAML parser (stdlib-only) — handles the specific format in
# eval/questions.yaml.  No anchors, no multi-line scalars, no complex types.
#
# Expected layout (fixed indentation):
#
#   questions:                          indent 0
#     - id: some-id                     indent 2, starts a new question
#       kind: content                   indent 4+, key-value fields
#       must_cite: true
#       question: "text"
#       expected:                       indent 4, starts sub-list
#         - "item 1"                    indent 6+, list items
#         - "item 2"
# ---------------------------------------------------------------------------


def _unquote(s: str) -> str:
    """Strip a matching pair of surrounding quotes (single or double) if present."""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def _parse_questions_yaml(path: Path) -> list[dict]:
    """Parse questions.yaml with stdlib only.

    Raises ValueError with a description if the file cannot be parsed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc

    questions: list[dict] = []
    current: dict | None = None
    in_expected = False

    for lineno, raw in enumerate(text.splitlines(), 1):
        # Preserve raw for error messages; strip trailing whitespace only.
        line = raw.rstrip()
        stripped = line.lstrip()

        # Skip blanks and comments.
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(stripped)

        # indent 0 — top-level key (only "questions:"); skip.
        if indent == 0:
            continue

        # indent 2, starts with "- " — new question entry.
        if indent == 2 and stripped.startswith("- "):
            if current is not None:
                questions.append(current)
            in_expected = False
            current = {}
            # The rest after "- " is the first key-value pair, e.g. "id: my-id".
            kv = stripped[2:].strip()
            if ":" in kv:
                k, _, v = kv.partition(":")
                current[k.strip()] = _unquote(v.strip())
            continue

        if current is None:
            continue  # not yet inside a question

        # indent 6+, starts with "- " — item in the current sub-list.
        if indent >= 6 and stripped.startswith("- "):
            if in_expected:
                current.setdefault("expected", []).append(_unquote(stripped[2:]))
            continue

        # indent 4+, key: value pair (not a list item).
        if indent >= 4 and not stripped.startswith("- ") and ":" in stripped:
            in_expected = False
            k, _, v = stripped.partition(":")
            k = k.strip()
            v = v.strip()
            if k == "expected":
                current["expected"] = []
                in_expected = True
            elif k == "must_cite":
                current[k] = v.lower() == "true"
            elif v:
                current[k] = _unquote(v)
            elif not v:
                # value-less key like "expected:" — already handled above.
                pass
            continue

        # Anything else at unexpected indentation — fail loud.
        if indent >= 4:
            raise ValueError(
                f"{path}:{lineno}: unexpected line format at indent {indent}: {raw!r}"
            )

    if current is not None:
        questions.append(current)

    if not questions:
        raise ValueError(f"{path}: no questions parsed — check YAML format")

    return questions


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


def _extract_json(output: str) -> dict:
    """Extract the first complete JSON object from subprocess stdout.

    wiki-weaver may emit coloured log lines (e.g. "! asking wiki at ...") before
    the JSON payload.  We scan forward to the first '{' and use the stdlib decoder's
    raw_decode() which stops at the end of the first complete value.

    Raises ValueError if no JSON object can be found or parsed.
    """
    start = output.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in subprocess output:\n{output[:600]}")
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(output, start)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Could not decode JSON from subprocess output: {exc}\n"
            f"Output excerpt: {output[start : start + 400]}"
        ) from exc
    if not isinstance(obj, dict):
        raise ValueError(f"Expected a JSON object, got {type(obj).__name__}")
    return obj


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------


def run_questions(
    corpus: str,
    questions_file: str,
    out_dir: str,
) -> int:
    """Run all questions and write outputs to out_dir.

    Returns 0 if all questions were attempted, 1 on a hard launch error.
    """
    q_path = Path(questions_file)
    out = Path(out_dir).resolve()
    answers_dir = out / "answers"
    answers_dir.mkdir(parents=True, exist_ok=True)

    try:
        questions = _parse_questions_yaml(q_path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"[eval] {len(questions)} question(s) loaded from {q_path}")
    print(f"[eval] corpus  : {corpus}")
    print(f"[eval] out dir : {out}\n")

    index: list[dict] = []
    any_launch_error = False

    for i, q in enumerate(questions, 1):
        qid = q.get("id", f"question-{i}")
        question_text = q.get("question", "")
        kind = q.get("kind", "unknown")
        must_cite = q.get("must_cite", False)
        expected = q.get("expected", [])

        print(f"[{i}/{len(questions)}] {qid}  ({kind})", end="  ", flush=True)

        cmd = [
            "repo-weaver",
            "ask",
            question_text,
            "--corpus",
            corpus,
            "--json",
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            print("ERROR: repo-weaver not found on PATH", file=sys.stderr)
            any_launch_error = True
            print("LAUNCH_ERROR")
            continue
        except subprocess.TimeoutExpired:
            print("TIMEOUT")
            record: dict = {
                "id": qid,
                "question": question_text,
                "kind": kind,
                "must_cite": must_cite,
                "expected": expected,
                "answer": None,
                "pages_used": [],
                "refused": None,
                "error": "subprocess timed out after 120 s",
            }
            _write_answer(answers_dir, qid, record)
            index.append({"id": qid, "file": f"answers/{qid}.json"})
            continue

        # Combine stdout and stderr for extraction (log lines can go to either).
        combined = proc.stdout + proc.stderr

        try:
            payload = _extract_json(combined)
            answer = payload.get("answer")
            pages_used = payload.get("pages_used", [])
            refused = payload.get("refused", False)
            error = None
            print(f"ok  (exit={proc.returncode}, refused={refused})")
        except ValueError as exc:
            answer = None
            pages_used = []
            refused = None
            error = str(exc)
            print(f"PARSE_ERROR  exit={proc.returncode}")

        record = {
            "id": qid,
            "question": question_text,
            "kind": kind,
            "must_cite": must_cite,
            "expected": expected,
            "answer": answer,
            "pages_used": pages_used,
            "refused": refused,
        }
        if error is not None:
            record["error"] = error

        _write_answer(answers_dir, qid, record)
        index.append({"id": qid, "file": f"answers/{qid}.json"})

    # Write index
    index_path = out / "answers.index.json"
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"\n[eval] index written: {index_path}")
    print(f"[eval] answers in   : {answers_dir}/")

    return 1 if any_launch_error else 0


def _write_answer(answers_dir: Path, qid: str, record: dict) -> None:
    out_path = answers_dir / f"{qid}.json"
    out_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.run_questions",
        description=(
            "Run the eval question set against a built corpus and save raw "
            "outputs for a downstream judge agent."
        ),
    )
    parser.add_argument(
        "--corpus",
        required=True,
        metavar="DIR",
        help="Path to the wiki corpus directory.",
    )
    parser.add_argument(
        "--questions",
        default="eval/questions.yaml",
        metavar="YAML",
        help="Path to questions.yaml (default: eval/questions.yaml).",
    )
    parser.add_argument(
        "--out",
        default="./eval-out",
        metavar="DIR",
        help="Output directory for answer files (default: ./eval-out).",
    )
    args = parser.parse_args(argv)

    return run_questions(
        corpus=args.corpus,
        questions_file=args.questions,
        out_dir=args.out,
    )


if __name__ == "__main__":
    sys.exit(main())
