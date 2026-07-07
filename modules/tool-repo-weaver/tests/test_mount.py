"""Protocol-compliance tests for tool-repo-weaver.

These tests verify the Iron Law: mount() MUST call coordinator.mount() for
each tool, and the return value must be a metadata dict (not None).

Run via: pytest tests/test_mount.py (from the modules/tool-repo-weaver/ dir)
or:       cd repo-weaver && python -m pytest modules/tool-repo-weaver/tests/
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import path setup: the module package lives one level up from tests/
# ---------------------------------------------------------------------------

_MODULE_DIR = Path(__file__).parent.parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

# ---------------------------------------------------------------------------
# Mock amplifier_core before importing the module so that tests can run
# without a full Amplifier installation.  repo_weaver is now a real
# installed package (wiki-weaver dep brings it in), so it does NOT need
# a mock — importing it directly is both possible and preferable.
# ---------------------------------------------------------------------------


class _MockToolResult:
    def __init__(self, *, success: bool, output: str) -> None:
        self.success = success
        self.output = output


_mock_amplifier_core = MagicMock()
_mock_amplifier_core.ToolResult = _MockToolResult
sys.modules.setdefault("amplifier_core", _mock_amplifier_core)

# Now we can import the module under test.
from amplifier_module_tool_repo_weaver import mount  # noqa: E402


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_registers_all_five_tools() -> None:
    """mount() MUST register all 5 tools via coordinator.mount() — Iron Law."""
    coordinator = MagicMock()
    coordinator.mount = AsyncMock()

    await mount(coordinator)

    # Five tools must be registered.
    assert coordinator.mount.call_count == 5, (
        f"Expected 5 coordinator.mount() calls, got {coordinator.mount.call_count}. "
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
async def test_mount_provides_five_tool_names() -> None:
    """The 'provides' list must contain exactly 5 tool names."""
    coordinator = MagicMock()
    coordinator.mount = AsyncMock()

    result = await mount(coordinator)

    provides = result["provides"]
    assert len(provides) == 5, (
        f"Expected 5 provided tools, got {len(provides)}: {provides}"
    )
    assert "repo_weaver_init" in provides
    assert "repo_weaver_weave" in provides
    assert "repo_weaver_ask" in provides
    assert "repo_weaver_sync" in provides
    assert "repo_weaver_discover" in provides


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


# ---------------------------------------------------------------------------
# Per-tool execute() tests for the two new tools (sync_corpus / discover_repos
# mocked at the import boundary, i.e. the names bound into
# amplifier_module_tool_repo_weaver's own module namespace).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_tool_execute_success() -> None:
    """RepoWeaverSyncTool.execute() succeeds and returns the result dict as JSON."""
    from amplifier_module_tool_repo_weaver import RepoWeaverSyncTool

    fake_result = {
        "last_sync": "2026-07-01",
        "until": "2026-07-07",
        "owners": {"microsoft": 0},
        "changed": [],
        "errors": [],
        "discovery_failed": [],
    }

    with patch(
        "amplifier_module_tool_repo_weaver.sync_corpus", return_value=fake_result
    ) as mock_sync:
        tool = RepoWeaverSyncTool()
        result = await tool.execute({"corpus": "/tmp/corpus"})

    mock_sync.assert_called_once()
    assert result.success is True
    assert isinstance(result.output, str)
    assert '"last_sync": "2026-07-01"' in result.output


@pytest.mark.asyncio
async def test_sync_tool_execute_discovery_failure() -> None:
    """RepoWeaverSyncTool.execute() fails when gh discovery failed for an owner.

    Mirrors repo_weaver.cli._sync_exit_code(): discovery_failed always means
    non-zero/failure, regardless of the (empty) changed list.
    """
    from amplifier_module_tool_repo_weaver import RepoWeaverSyncTool

    fake_result = {
        "last_sync": "2026-07-01",
        "until": "2026-07-07",
        "owners": {"microsoft": 0},
        "changed": [],
        "errors": ["microsoft: gh auth failed"],
        "discovery_failed": ["microsoft"],
    }

    with patch(
        "amplifier_module_tool_repo_weaver.sync_corpus", return_value=fake_result
    ):
        tool = RepoWeaverSyncTool()
        result = await tool.execute({"corpus": "/tmp/corpus"})

    assert result.success is False


@pytest.mark.asyncio
async def test_discover_tool_execute_success() -> None:
    """RepoWeaverDiscoverTool.execute() succeeds and returns matched repos as JSON."""
    from amplifier_module_tool_repo_weaver import RepoWeaverDiscoverTool

    fake_matched = [
        {
            "name": "amplifier-app-repo-weaver",
            "nameWithOwner": "microsoft/amplifier-app-repo-weaver",
        }
    ]

    with patch(
        "amplifier_module_tool_repo_weaver.discover_repos",
        return_value=(fake_matched, []),
    ) as mock_discover:
        tool = RepoWeaverDiscoverTool()
        result = await tool.execute(
            {"rules": [{"owner": "microsoft", "match": "amplifier*"}]}
        )

    mock_discover.assert_called_once()
    assert result.success is True
    assert isinstance(result.output, str)
    assert "amplifier-app-repo-weaver" in result.output


@pytest.mark.asyncio
async def test_discover_tool_execute_failure() -> None:
    """RepoWeaverDiscoverTool.execute() fails when a rule's gh call errored.

    Mirrors repo_weaver.cli.cmd_discover(): any error makes the run non-zero.
    """
    from amplifier_module_tool_repo_weaver import RepoWeaverDiscoverTool

    with patch(
        "amplifier_module_tool_repo_weaver.discover_repos",
        return_value=([], ["someowner: gh auth failed"]),
    ):
        tool = RepoWeaverDiscoverTool()
        result = await tool.execute(
            {"rules": [{"owner": "someowner", "match": "amplifier*"}]}
        )

    assert result.success is False
