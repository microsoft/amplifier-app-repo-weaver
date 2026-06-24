"""Protocol-compliance tests for tool-repo-weaver.

These tests verify the Iron Law: mount() MUST call coordinator.mount() for
each tool, and the return value must be a metadata dict (not None).

Run via: pytest tests/test_mount.py (from the modules/tool-repo-weaver/ dir)
or:       cd repo-weaver && python -m pytest modules/tool-repo-weaver/tests/
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import path setup: the module package lives one level up from tests/
# ---------------------------------------------------------------------------

_MODULE_DIR = Path(__file__).parent.parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

# ---------------------------------------------------------------------------
# Mock amplifier_core and repo_weaver before importing the module so that
# tests can run without a full Amplifier installation.
# ---------------------------------------------------------------------------


class _MockToolResult:
    def __init__(self, *, success: bool, output: str) -> None:
        self.success = success
        self.output = output


_mock_amplifier_core = MagicMock()
_mock_amplifier_core.ToolResult = _MockToolResult
sys.modules.setdefault("amplifier_core", _mock_amplifier_core)

_mock_repo_weaver = MagicMock()
_mock_repo_weaver.init = MagicMock(return_value=0)
_mock_repo_weaver.weave = MagicMock(return_value=0)
_mock_repo_weaver.ask = MagicMock(return_value=0)
sys.modules.setdefault("repo_weaver", _mock_repo_weaver)

# Now we can import the module under test.
from amplifier_module_tool_repo_weaver import mount  # noqa: E402


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_registers_all_three_tools() -> None:
    """mount() MUST register all 3 tools via coordinator.mount() — Iron Law."""
    coordinator = MagicMock()
    coordinator.mount = AsyncMock()

    await mount(coordinator)

    # Three tools must be registered.
    assert coordinator.mount.call_count == 3, (
        f"Expected 3 coordinator.mount() calls, got {coordinator.mount.call_count}. "
        "Iron Law violation: mount() must register all tools."
    )

    # First positional arg of every call must be "tools".
    for call in coordinator.mount.call_args_list:
        assert call.args[0] == "tools", (
            f"Expected first arg 'tools', got {call.args[0]!r}"
        )


@pytest.mark.asyncio
async def test_mount_returns_metadata_dict_not_none() -> None:
    """mount() must return a metadata dict — not None."""
    coordinator = MagicMock()
    coordinator.mount = AsyncMock()

    result = await mount(coordinator)

    assert result is not None, "mount() returned None — Iron Law violation."
    assert isinstance(result, dict), f"mount() must return a dict, got {type(result)}"
    assert "name" in result, "metadata dict must have 'name' key"
    assert "provides" in result, "metadata dict must have 'provides' key"


@pytest.mark.asyncio
async def test_mount_provides_three_tool_names() -> None:
    """The 'provides' list must contain exactly 3 tool names."""
    coordinator = MagicMock()
    coordinator.mount = AsyncMock()

    result = await mount(coordinator)

    provides = result["provides"]
    assert len(provides) == 3, (
        f"Expected 3 provided tools, got {len(provides)}: {provides}"
    )
    assert "repo_weaver_init" in provides
    assert "repo_weaver_weave" in provides
    assert "repo_weaver_ask" in provides


@pytest.mark.asyncio
async def test_each_registered_tool_has_required_properties() -> None:
    """Each registered tool must have name, description, input_schema, execute."""
    coordinator = MagicMock()
    coordinator.mount = AsyncMock()

    await mount(coordinator)

    for call in coordinator.mount.call_args_list:
        tool = call.args[1]  # second positional arg is the tool instance
        assert isinstance(tool.name, str) and tool.name, (
            f"Tool must have a non-empty string 'name', got {tool.name!r}"
        )
        assert isinstance(tool.description, str) and tool.description, (
            f"Tool {tool.name!r} must have a non-empty 'description'"
        )
        assert isinstance(tool.input_schema, dict), (
            f"Tool {tool.name!r} must have a dict 'input_schema'"
        )
        assert callable(tool.execute), (
            f"Tool {tool.name!r} must have a callable 'execute'"
        )


@pytest.mark.asyncio
async def test_tool_names_match_registered_names() -> None:
    """The name= kwarg passed to coordinator.mount() must match tool.name."""
    coordinator = MagicMock()
    coordinator.mount = AsyncMock()

    await mount(coordinator)

    for call in coordinator.mount.call_args_list:
        tool = call.args[1]
        registered_name = call.kwargs.get("name")
        assert registered_name == tool.name, (
            f"Registered name {registered_name!r} does not match tool.name {tool.name!r}"
        )
