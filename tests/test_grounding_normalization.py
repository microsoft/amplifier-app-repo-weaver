"""Tests for Change 2: normalization in the grounding tracer.

Verifies that _classify() treats trivially-equivalent forms as the same
fact (v8.0.16 == 8.0.16) without masking genuinely-absent facts (9.9.9).

All tests are pure unit tests — no files, no corpus, no network.
"""

from __future__ import annotations

from eval.trace_grounding import _classify, _normalize_for_match


# ---------------------------------------------------------------------------
# _normalize_for_match unit tests
# ---------------------------------------------------------------------------


def test_normalize_strips_v_prefix_from_semver():
    """v8.0.16 → 8.0.16."""
    assert _normalize_for_match("v8.0.16") == "8.0.16"


def test_normalize_strips_uppercase_v_prefix():
    """V8.0.16 → 8.0.16."""
    assert _normalize_for_match("V8.0.16") == "8.0.16"


def test_normalize_does_not_strip_v_from_word():
    """'version' must not be mangled."""
    result = _normalize_for_match("version")
    assert "ersion" in result  # 'v' only stripped before a digit


def test_normalize_preserves_bare_version_unchanged():
    """8.0.16 (no prefix) passes through unchanged (modulo case-fold)."""
    assert _normalize_for_match("8.0.16") == "8.0.16"


def test_normalize_keeps_distinct_versions_distinct():
    """8.0.16 and 9.9.9 must not normalize to the same form."""
    assert _normalize_for_match("8.0.16") != _normalize_for_match("9.9.9")


def test_normalize_case_folds():
    """ASCII case is folded."""
    assert _normalize_for_match("GRPC") == _normalize_for_match("grpc")


def test_normalize_smart_quotes_to_ascii():
    """Smart curly quotes become ASCII equivalents."""
    assert _normalize_for_match("\u2018hello\u2019") == "'hello'"
    assert _normalize_for_match("\u201chello\u201d") == '"hello"'


def test_normalize_collapses_whitespace():
    """Multiple whitespace chars are collapsed to one space."""
    assert _normalize_for_match("hello   world") == "hello world"


def test_normalize_v_prefix_short_version():
    """v2 → 2  (single-segment version)."""
    assert _normalize_for_match("v2") == "2"


# ---------------------------------------------------------------------------
# _classify normalization integration tests
# ---------------------------------------------------------------------------


def test_v_prefixed_version_grounded_when_source_has_bare():
    """v8.0.16 in answer is GROUNDED when source contains 8.0.16.

    This is the exact class of false-UNGROUNDED that triggered Change 2:
    the answer said 'v8.0.16' but the source digest said '8.0.16',
    causing the grounding tracer to (incorrectly) mark it UNGROUNDED.
    """
    source_docs = {"digest.md": "Released version 8.0.16 of the SDK."}
    wiki_pages: dict[str, str] = {}

    classification, found_in = _classify("v8.0.16", source_docs, wiki_pages)

    assert classification == "GROUNDED", (
        f"Expected GROUNDED for 'v8.0.16' when source contains '8.0.16'; "
        f"got {classification!r} (found_in={found_in!r}).  "
        "Normalization must strip the leading 'v' before matching."
    )
    assert "_archive/digest.md" in found_in


def test_bare_version_grounded_when_source_has_v_prefix():
    """8.0.16 in answer is GROUNDED when source contains v8.0.16."""
    source_docs = {"digest.md": "Shipped v8.0.16 yesterday."}
    wiki_pages: dict[str, str] = {}

    classification, _ = _classify("8.0.16", source_docs, wiki_pages)
    assert classification == "GROUNDED", (
        f"Expected GROUNDED for '8.0.16' when source contains 'v8.0.16'; "
        f"got {classification!r}"
    )


def test_genuinely_absent_version_is_ungrounded():
    """9.9.9 not present in source or wiki is correctly UNGROUNDED.

    Normalization must never conflate different version numbers.
    """
    source_docs = {"digest.md": "Released version 8.0.16 of the SDK."}
    wiki_pages: dict[str, str] = {}

    classification, found_in = _classify("9.9.9", source_docs, wiki_pages)

    assert classification == "UNGROUNDED", (
        f"Expected UNGROUNDED for '9.9.9' (not in source or wiki); "
        f"got {classification!r} (found_in={found_in!r}).  "
        "Normalization must NOT conflate 8.0.16 with 9.9.9."
    )
    assert found_in == ""


def test_v_prefixed_absent_version_is_ungrounded():
    """v9.9.9 not present anywhere is UNGROUNDED after normalization."""
    source_docs = {"digest.md": "Released version 8.0.16 of the SDK."}
    wiki_pages: dict[str, str] = {}

    classification, _ = _classify("v9.9.9", source_docs, wiki_pages)
    assert classification == "UNGROUNDED", (
        f"v9.9.9 should be UNGROUNDED since 9.9.9 is not in the source; got {classification!r}"
    )


def test_smart_quote_in_answer_grounded_when_source_has_ascii():
    """Smart-quoted token matches ASCII equivalent in source."""
    source_docs = {"wiki.md": "The 'main' branch is the default."}
    wiki_pages: dict[str, str] = {}

    # Answer uses smart quotes (as LLMs sometimes output)
    classification, _ = _classify("\u2018main\u2019", source_docs, wiki_pages)
    assert classification == "GROUNDED", (
        "Smart-quoted 'main' should be GROUNDED when source has ASCII 'main'"
    )


def test_case_insensitive_match():
    """Case differences between answer and source do not cause UNGROUNDED."""
    source_docs = {"digest.md": "The GRPC transport layer was updated."}
    wiki_pages: dict[str, str] = {}

    classification, _ = _classify("grpc", source_docs, wiki_pages)
    assert classification == "GROUNDED", (
        "Case difference alone must not cause UNGROUNDED"
    )


def test_synthesized_only_when_in_wiki_not_source():
    """Token in wiki but not source → SYNTHESIZED_ONLY (normalization preserved)."""
    source_docs: dict[str, str] = {}
    wiki_pages = {"module.md": "The component uses version 8.0.16."}

    classification, found_in = _classify("v8.0.16", source_docs, wiki_pages)
    assert classification == "SYNTHESIZED_ONLY", (
        f"Expected SYNTHESIZED_ONLY when token only in wiki; got {classification!r}"
    )
    assert "module.md" in found_in
