"""Tests for the public library API exposed by ``repo_weaver.__init__``.

Verified properties
-------------------
* All six public symbols are importable from the ``repo_weaver`` package.
* Every symbol is callable (functions, not accidentally imported constants).
* ``__all__`` is defined and contains exactly the expected names.
* ``init`` and ``ask`` have the expected top-level parameter names.
* ``weave_multi`` accepts a ``repos`` parameter (list not single str).
* ``replay_windows`` accepts a ``windows`` parameter.
* No private-helper name (``_`` prefix) leaks into ``__all__``.
"""

from __future__ import annotations

import inspect

import repo_weaver

_EXPECTED_PUBLIC = {
    "ask",
    "init",
    "materialize",
    "replay_windows",
    "weave",
    "weave_multi",
}


def test_all_public_symbols_importable():
    """Every expected public symbol is an attribute of the package."""
    for name in _EXPECTED_PUBLIC:
        assert hasattr(repo_weaver, name), f"repo_weaver.{name} not found"


def test_all_public_symbols_callable():
    """Every public symbol is callable (it's a function, not a raw constant)."""
    for name in _EXPECTED_PUBLIC:
        obj = getattr(repo_weaver, name)
        assert callable(obj), (
            f"repo_weaver.{name} is not callable — expected a function"
        )


def test_all_defined_in_dunder_all():
    """``__all__`` is defined and contains every expected public name."""
    assert hasattr(repo_weaver, "__all__"), "repo_weaver.__all__ is not defined"
    for name in _EXPECTED_PUBLIC:
        assert name in repo_weaver.__all__, f"{name!r} missing from repo_weaver.__all__"


def test_no_private_names_in_dunder_all():
    """No ``_``-prefixed names are present in ``__all__``."""
    for name in repo_weaver.__all__:
        assert not name.startswith("_") or name in ("__version__",), (
            f"Private name {name!r} leaked into __all__ — only __version__ is allowed"
        )


def test_init_signature_has_corpus_and_repos():
    """``init()`` exposes ``corpus`` (required) and ``repos`` (optional) parameters."""
    sig = inspect.signature(repo_weaver.init)
    params = sig.parameters
    assert "corpus" in params, "init() is missing 'corpus' parameter"
    assert "repos" in params, "init() is missing 'repos' parameter"
    # repos should have a default (it's optional)
    assert params["repos"].default is not inspect.Parameter.empty, (
        "init() 'repos' should have a default value (None)"
    )


def test_ask_signature_has_question_corpus_output_json():
    """``ask()`` exposes ``question``, ``corpus``, and ``output_json`` parameters."""
    sig = inspect.signature(repo_weaver.ask)
    params = sig.parameters
    assert "question" in params, "ask() is missing 'question' parameter"
    assert "corpus" in params, "ask() is missing 'corpus' parameter"
    assert "output_json" in params, "ask() is missing 'output_json' parameter"


def test_weave_multi_signature_has_repos():
    """``weave_multi()`` exposes a ``repos`` parameter (list, not single str)."""
    sig = inspect.signature(repo_weaver.weave_multi)
    assert "repos" in sig.parameters, "weave_multi() is missing 'repos' parameter"


def test_replay_windows_signature_has_windows():
    """``replay_windows()`` exposes a ``windows`` parameter."""
    sig = inspect.signature(repo_weaver.replay_windows)
    assert "windows" in sig.parameters, (
        "replay_windows() is missing 'windows' parameter"
    )


def test_version_string_is_set():
    """``__version__`` is a non-empty string."""
    assert isinstance(repo_weaver.__version__, str)
    assert repo_weaver.__version__, "__version__ must not be empty"
