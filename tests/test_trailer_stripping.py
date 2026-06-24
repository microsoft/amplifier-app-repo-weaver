"""Tests for navigation-trailer stripping in the grounding tracer.

Verifies that trailing "Pages used:", "Pages consulted:", and
"Source: [.md]" lines are stripped from an answer *before* grounding
analysis so wiki-layer filenames never produce false-positive UNGROUNDED
tokens.

All tests are pure unit tests — no files, no corpus, no network.
"""

from __future__ import annotations

from eval.trace_grounding import _classify, _extract_tokens, _strip_navigation_trailer


# ---------------------------------------------------------------------------
# _strip_navigation_trailer — unit tests for the stripping function itself
# ---------------------------------------------------------------------------


def test_strip_pages_used_trailer() -> None:
    """'Pages used:' line at the tail is stripped."""
    text = "The project uses React.\n\nPages used: index.md, overview.md"
    result = _strip_navigation_trailer(text)
    assert "Pages used" not in result
    assert "The project uses React." in result


def test_strip_pages_consulted_trailer() -> None:
    """'Pages consulted:' label is stripped."""
    text = "The project uses React.\n\nPages consulted: index.md, overview.md"
    result = _strip_navigation_trailer(text)
    assert "Pages consulted" not in result
    assert "The project uses React." in result


def test_strip_pages_bare_label() -> None:
    """'Pages:' (no qualifier) at the tail is stripped."""
    text = "Answer body.\n\nPages: index.md, overview.md"
    result = _strip_navigation_trailer(text)
    assert "Pages:" not in result
    assert "Answer body." in result


def test_strip_bold_pages_used_trailer() -> None:
    """'**Pages used:**' (Markdown bold) variant is stripped."""
    text = "Answer body.\n\n**Pages used:** index.md, overview.md"
    result = _strip_navigation_trailer(text)
    assert "Pages used" not in result
    assert "Answer body." in result


def test_strip_bold_pages_consulted_trailer() -> None:
    """'**Pages consulted:**' (Markdown bold) variant is stripped."""
    text = "Answer body.\n\n**Pages consulted:** index.md, overview.md"
    result = _strip_navigation_trailer(text)
    assert "Pages consulted" not in result
    assert "Answer body." in result


def test_strip_multiline_pages_trailer() -> None:
    """Multi-line Pages used: block (header + continuation lines) is fully stripped."""
    text = (
        "Answer body.\n\nPages used:\n  index.md, overview.md\n  frontend-toolchain.md"
    )
    result = _strip_navigation_trailer(text)
    assert "Pages used" not in result
    assert "index.md" not in result
    assert "frontend-toolchain.md" not in result
    assert "Answer body." in result


def test_strip_pages_trailer_with_trailing_ellipsis() -> None:
    """Trailing '...' in the Pages consulted: list is handled."""
    text = "Answer body.\n\nPages consulted: index.md, overview.md, ..."
    result = _strip_navigation_trailer(text)
    assert "Pages consulted" not in result
    assert "Answer body." in result


def test_strip_source_nav_line_after_pages() -> None:
    """Source: [.md] line that follows Pages: is stripped together with it."""
    text = "Answer body.\n\nPages used: index.md\nSource: [index.md]"
    result = _strip_navigation_trailer(text)
    assert "Pages used" not in result
    assert "Source" not in result
    assert "Answer body." in result


def test_strip_standalone_source_nav_line() -> None:
    """Standalone Source: citation with only .md names at the tail is stripped."""
    text = "Answer body.\n\nSource: [index.md, overview.md]"
    result = _strip_navigation_trailer(text)
    assert "Source" not in result
    assert "Answer body." in result


def test_no_strip_prose_source_line() -> None:
    """Source: followed by prose (not .md filenames) is NOT stripped."""
    text = "Answer body.\n\nSource: The data comes from the primary project README."
    result = _strip_navigation_trailer(text)
    assert result == text, "Prose Source: line must be left untouched"


def test_no_strip_mid_body_pages_label() -> None:
    """A 'Pages used:' label in the body (not at the tail) is NOT stripped."""
    text = (
        "Pages used in the project span many modules.\n\n"
        "The runtime was updated on 2024-01-15."
    )
    result = _strip_navigation_trailer(text)
    assert result == text, "Mid-body Pages mention must not be stripped"


def test_no_trailer_text_unchanged() -> None:
    """Answer with no trailer is returned byte-for-byte unchanged."""
    text = "The version is 8.0.16. Released on 2024-01-15."
    result = _strip_navigation_trailer(text)
    assert result == text


def test_empty_string_unchanged() -> None:
    """Empty input returns empty output."""
    assert _strip_navigation_trailer("") == ""


def test_trailing_blank_lines_before_header_stripped() -> None:
    """Blank lines between body and Pages: header are absorbed into the strip."""
    text = "Body text.\n\n\n\nPages used: index.md"
    result = _strip_navigation_trailer(text)
    assert "Pages used" not in result
    assert "Body text." in result


# ---------------------------------------------------------------------------
# Integration tests — grounding analysis after stripping
# ---------------------------------------------------------------------------


def test_pages_used_filenames_not_flagged_ungrounded() -> None:
    """Test 1: backtick-quoted page names in trailer → 0 UNGROUNDED tokens after strip.

    Before the fix, backtick-quoted filenames in the 'Pages used:' trailer
    were extracted as backtick_id tokens and classified UNGROUNDED because
    wiki navigation filenames don't appear in the raw _archive/ source docs.
    After the fix, the trailer is stripped first so those tokens are never
    analysed.
    """
    answer = (
        "The project uses gRPC for its transport layer. "
        "The release date was 2024-03-01.\n\n"
        "Pages used: `index.md`, `frontend-toolchain.md`"
    )
    source_docs = {
        "digest.md": ("The project uses gRPC for its transport layer. 2024-03-01.")
    }
    wiki_pages: dict[str, str] = {}

    # --- PRE-FIX BEHAVIOUR (without stripping) ---
    # Demonstrate that the filenames WOULD have been extracted from the
    # unstripped answer (proving the fix addresses a real false positive).
    tokens_unstripped = _extract_tokens(answer)
    backtick_tokens = {
        tok for (tok, cat, _) in tokens_unstripped if cat == "backtick_id"
    }
    assert "index.md" in backtick_tokens, (
        "Pre-fix: index.md must be extracted from the unstripped answer so "
        "this test proves the fix eliminates a real false positive"
    )
    assert "frontend-toolchain.md" in backtick_tokens, (
        "Pre-fix: frontend-toolchain.md must be extracted from the unstripped answer"
    )

    # --- POST-FIX BEHAVIOUR (with stripping) ---
    stripped = _strip_navigation_trailer(answer)

    assert "Pages used" not in stripped, "Trailer header must be removed"
    assert "`index.md`" not in stripped, "index.md must be stripped"
    assert "`frontend-toolchain.md`" not in stripped, (
        "frontend-toolchain.md must be stripped"
    )

    tokens_after = _extract_tokens(stripped)
    token_strs = {tok for (tok, _, _) in tokens_after}

    assert "index.md" not in token_strs, (
        "index.md must not appear as an extracted token after stripping; "
        f"token_strs={token_strs!r}"
    )
    assert "frontend-toolchain.md" not in token_strs, (
        "frontend-toolchain.md must not appear as an extracted token after stripping; "
        f"token_strs={token_strs!r}"
    )

    # Body tokens should be clean (no UNGROUNDED from trailer filenames)
    ungrounded = [
        tok
        for (tok, _cat, _snip) in tokens_after
        if _classify(tok, source_docs, wiki_pages)[0] == "UNGROUNDED"
    ]
    assert not any("index.md" in u for u in ungrounded), (
        f"index.md must not be UNGROUNDED; ungrounded={ungrounded!r}"
    )
    assert not any("frontend-toolchain.md" in u for u in ungrounded), (
        f"frontend-toolchain.md must not be UNGROUNDED; ungrounded={ungrounded!r}"
    )


def test_real_ungrounded_body_claim_still_flagged_with_trailer() -> None:
    """Test 2: fabricated body claim remains UNGROUNDED even when a trailer is present.

    Trailer stripping must only remove the navigation footer.  A version
    claim (`9.9.9`) that is absent from the source docs must still be
    classified UNGROUNDED — proving we stripped the trailer, not the analysis.
    """
    answer = (
        "The latest release is `9.9.9` which ships new features.\n\n"
        "Pages used: `index.md`, `frontend-toolchain.md`"
    )
    source_docs = {"digest.md": "Released version 8.0.16 of the SDK."}
    wiki_pages: dict[str, str] = {}

    stripped = _strip_navigation_trailer(answer)

    # Trailer gone, body intact
    assert "Pages used" not in stripped, "Trailer header must be removed"
    assert "9.9.9" in stripped, "Body claim 9.9.9 must survive the strip"

    # Classify tokens from the stripped text
    tokens = _extract_tokens(stripped)
    classifications: dict[str, str] = {
        tok: _classify(tok, source_docs, wiki_pages)[0] for (tok, _cat, _snip) in tokens
    }

    assert "9.9.9" in classifications, (
        "9.9.9 must still be extracted as a token from the stripped answer"
    )
    assert classifications["9.9.9"] == "UNGROUNDED", (
        f"9.9.9 must remain UNGROUNDED (not present as 8.0.16 in source); "
        f"got {classifications['9.9.9']!r}"
    )

    # Trailer filenames must NOT appear in the token list at all
    assert "index.md" not in classifications, (
        "index.md from the trailer must not be analysed after stripping"
    )
    assert "frontend-toolchain.md" not in classifications, (
        "frontend-toolchain.md from the trailer must not be analysed after stripping"
    )


def test_no_trailer_analysis_unchanged() -> None:
    """Test 3: answer with no trailer is analysed identically before and after stripping.

    Regression guard: stripping must be a no-op for clean answers.
    """
    answer = "The SDK was released as version `8.0.16` on 2024-03-01."
    source_docs = {"digest.md": "Version 8.0.16 released 2024-03-01."}
    wiki_pages: dict[str, str] = {}

    stripped = _strip_navigation_trailer(answer)

    # Text must be identical — no trailer to remove
    assert stripped == answer, (
        f"No-trailer answer must be byte-for-byte unchanged; "
        f"got {stripped!r} vs {answer!r}"
    )

    # Token extraction must be identical
    tokens_before = _extract_tokens(answer)
    tokens_after = _extract_tokens(stripped)
    assert tokens_before == tokens_after, (
        "Token list must not change for a no-trailer answer"
    )

    # All body tokens must still be GROUNDED
    for tok, _cat, _snip in tokens_after:
        cls, _ = _classify(tok, source_docs, wiki_pages)
        assert cls == "GROUNDED", (
            f"Token {tok!r} must be GROUNDED (present in source); got {cls!r}"
        )
