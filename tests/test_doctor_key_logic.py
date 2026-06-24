"""Tests for the doctor LLM-provider API-key gate logic.

Contract
--------
* PASS (exit 0) when only ANTHROPIC_API_KEY is set.
* PASS (exit 0) when only GOOGLE_API_KEY is set.
* PASS (exit 0) when only OPENAI_API_KEY is set.
* FAIL (exit non-zero) when none of the three keys are set.
* PASS (exit 0) when multiple keys are set (superset is always fine).

Non-key failures (wiki-weaver missing, policy/schema.md absent, etc.) are NOT
tested here — those live in existing production-readiness tests.  These tests
isolate the key-gate behaviour by mocking everything else to succeed.

Implementation note
-------------------
``cmd_doctor`` is tested as a function (not via ``subprocess``) to stay fast
and avoid requiring real tool installations.  The tool checks, subprocess
calls, and ``_POLICY_SCHEMA.exists()`` are all patched to return "OK" so that
only the key-gate logic under test varies.
"""

from __future__ import annotations

import argparse
import os
from unittest.mock import MagicMock, patch

from repo_weaver.cli import cmd_doctor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALL_PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY")


def _make_doctor_args() -> argparse.Namespace:
    """Return a minimal Namespace that satisfies cmd_doctor's signature."""
    return argparse.Namespace(command="doctor")


def _run_doctor(key_values: dict[str, str]) -> int:
    """Run ``cmd_doctor`` with controlled env vars and mocked tool checks.

    Args:
        key_values: Mapping of provider key name → value.
                    Keys not present here are explicitly set to ``""``
                    so that real environment variables do not contaminate
                    the test.

    Returns:
        The integer exit code returned by ``cmd_doctor``.
    """
    # Build the env override: start with all keys absent (""), then apply
    # the caller's values.  This ensures the real environment (which might
    # have e.g. ANTHROPIC_API_KEY set) does not affect the result.
    env_override: dict[str, str] = {k: "" for k in _ALL_PROVIDER_KEYS}
    env_override.update(key_values)

    # Fake subprocess.run: returns a successful result for all calls
    # (_check_tool version commands and the gh auth status check).
    fake_proc = MagicMock()
    fake_proc.return_value = MagicMock(
        returncode=0,
        stdout="fake-tool 1.0.0\n",
        stderr="",
    )

    # Fake _POLICY_SCHEMA so the policy/schema.md check always passes.
    fake_schema = MagicMock()
    fake_schema.exists.return_value = True
    fake_schema.__str__ = MagicMock(return_value="/fake/policy/schema.md")

    with (
        # All tools found on PATH.
        patch("repo_weaver.cli.shutil.which", return_value="/fake/bin/tool"),
        # All subprocess calls succeed (wiki-weaver --version, git --version,
        # gh auth status).
        patch("repo_weaver.cli.subprocess.run", fake_proc),
        # _POLICY_SCHEMA.exists() → True so the schema check passes.
        patch("repo_weaver.cli._POLICY_SCHEMA", fake_schema),
        # Inject controlled env vars — overriding any real values.
        patch.dict(os.environ, env_override, clear=False),
    ):
        return cmd_doctor(_make_doctor_args())


# ---------------------------------------------------------------------------
# Positive cases: at least one key → PASS
# ---------------------------------------------------------------------------


def test_doctor_passes_with_only_anthropic_key():
    """PASS when only ANTHROPIC_API_KEY is set (wiki-weaver's default provider)."""
    rc = _run_doctor({"ANTHROPIC_API_KEY": "sk-ant-testkey"})
    assert rc == 0, (
        f"Expected exit 0 with only ANTHROPIC_API_KEY set, got {rc}. "
        "An anthropic-only deployment should not trip the doctor gate."
    )


def test_doctor_passes_with_only_google_key():
    """PASS when only GOOGLE_API_KEY is set."""
    rc = _run_doctor({"GOOGLE_API_KEY": "AIza-testkey"})
    assert rc == 0, f"Expected exit 0 with only GOOGLE_API_KEY set, got {rc}."


def test_doctor_passes_with_only_openai_key():
    """PASS when only OPENAI_API_KEY is set."""
    rc = _run_doctor({"OPENAI_API_KEY": "sk-openai-testkey"})
    assert rc == 0, f"Expected exit 0 with only OPENAI_API_KEY set, got {rc}."


def test_doctor_passes_with_all_keys_set():
    """PASS when all three keys are set (superset of any single-key case)."""
    rc = _run_doctor(
        {
            "ANTHROPIC_API_KEY": "sk-ant-testkey",
            "GOOGLE_API_KEY": "AIza-testkey",
            "OPENAI_API_KEY": "sk-openai-testkey",
        }
    )
    assert rc == 0, f"Expected exit 0 with all provider keys set, got {rc}."


def test_doctor_passes_with_anthropic_and_google():
    """PASS when two of the three keys are set."""
    rc = _run_doctor(
        {
            "ANTHROPIC_API_KEY": "sk-ant-testkey",
            "GOOGLE_API_KEY": "AIza-testkey",
        }
    )
    assert rc == 0, f"Expected exit 0 with ANTHROPIC + GOOGLE set, got {rc}."


# ---------------------------------------------------------------------------
# Negative case: no keys → FAIL
# ---------------------------------------------------------------------------


def test_doctor_fails_with_no_provider_keys():
    """FAIL (non-zero) when none of ANTHROPIC / GOOGLE / OPENAI keys are set."""
    rc = _run_doctor({})  # all three keys → "" (absent)
    assert rc != 0, (
        f"Expected non-zero exit when no LLM provider keys are set, got {rc}. "
        "wiki-weaver ingest/ask requires at least one provider key."
    )
