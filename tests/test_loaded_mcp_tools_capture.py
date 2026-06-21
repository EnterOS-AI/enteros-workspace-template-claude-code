"""core#3082: the executor records the loaded MCP tool ids from the CLI `init`
system-message via molecule_runtime.platform_agent_identity.set_loaded_mcp_tools,
so the heartbeat can report `loaded_mcp_tools` and the platform online/degraded
gate can verify the management MCP's tools are actually LIVE (not just declared).

Self-contained SDK/a2a/runtime stubs (mirrors tests/test_resultmessage_detail.py)
plus a recording set_loaded_mcp_tools spy.
"""

import os
import sys
import types
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

# Records every set_loaded_mcp_tools(...) call the executor makes.
_recorded: list = []


def _ensure_module(dotted: str) -> types.ModuleType:
    if dotted not in sys.modules:
        sys.modules[dotted] = types.ModuleType(dotted)
    return sys.modules[dotted]


def _ensure_attr(mod: types.ModuleType, name: str, value: object) -> None:
    setattr(mod, name, value)


@dataclass
class _StubAssistantMessage:
    content: list = None


@dataclass
class _StubTextBlock:
    text: str = ""


@dataclass
class _StubResultMessage:
    result: str | None = None
    session_id: str | None = "sess-1"
    is_error: bool = False
    subtype: str = "success"
    api_error_status: int | None = None
    errors: list | None = None


# The class NAME must be exactly "SystemMessage" — the executor dispatches on
# type(message).__name__ (so a stub SDK that omits the class doesn't break).
@dataclass
class SystemMessage:
    subtype: str = "init"
    data: dict = None


def _install_stubs() -> None:
    sdk = _ensure_module("claude_agent_sdk")
    _ensure_attr(sdk, "ClaudeAgentOptions", MagicMock(name="ClaudeAgentOptions"))
    _ensure_attr(sdk, "AssistantMessage", _StubAssistantMessage)
    _ensure_attr(sdk, "TextBlock", _StubTextBlock)
    _ensure_attr(sdk, "ResultMessage", _StubResultMessage)
    _ensure_attr(sdk, "SystemMessage", SystemMessage)
    _ensure_attr(sdk, "query", MagicMock(name="query"))

    _ensure_module("a2a")
    _ensure_module("a2a.server")
    a2a_exec = _ensure_module("a2a.server.agent_execution")
    _ensure_attr(a2a_exec, "AgentExecutor", type("AgentExecutor", (), {}))
    _ensure_attr(a2a_exec, "RequestContext", type("RequestContext", (), {}))
    a2a_events = _ensure_module("a2a.server.events")
    _ensure_attr(a2a_events, "EventQueue", type("EventQueue", (), {}))
    a2a_helpers = _ensure_module("a2a.helpers")
    _ensure_attr(a2a_helpers, "new_text_message", lambda *_a, **_kw: None)

    _ensure_module("molecule_runtime")
    helpers = _ensure_module("molecule_runtime.executor_helpers")
    _ensure_attr(helpers, "CONFIG_MOUNT", "/configs")
    _ensure_attr(helpers, "WORKSPACE_MOUNT", "/workspace")
    _ensure_attr(helpers, "MEMORY_CONTENT_MAX_CHARS", 10000)

    async def _async_noop(*_a, **_kw):
        return None

    _ensure_attr(helpers, "auto_push_hook", _async_noop)
    _ensure_attr(helpers, "brief_summary", lambda *a, **kw: "")
    _ensure_attr(helpers, "collect_outbound_files", lambda *a, **kw: [])
    _ensure_attr(helpers, "commit_memory", _async_noop)
    _ensure_attr(helpers, "extract_attached_files", lambda *a, **kw: [])
    _ensure_attr(helpers, "extract_message_text", lambda *a, **kw: "")
    _ensure_attr(helpers, "get_a2a_instructions", lambda **kw: "")
    _ensure_attr(helpers, "get_display_instructions", lambda *a, **kw: "")
    _ensure_attr(helpers, "get_hma_instructions", lambda *a, **kw: "")
    _ensure_attr(helpers, "get_mcp_server_path", lambda *a, **kw: "/dev/null")
    _ensure_attr(helpers, "get_system_prompt", lambda *a, **kw: "")
    _ensure_attr(helpers, "read_delegation_results", lambda *a, **kw: "")

    async def _recall(*_a, **_kw):
        return ""

    _ensure_attr(helpers, "recall_memories", _recall)
    _ensure_attr(
        helpers,
        "sanitize_agent_error",
        lambda exc=None, category=None, stderr=None: f"Agent error: {exc}",
    )
    _ensure_attr(helpers, "error_detail_for_external", lambda exc: str(exc) or None)
    _ensure_attr(helpers, "set_current_task", _async_noop)

    # The unit under test: record what the executor reports as loaded tools.
    pai = _ensure_module("molecule_runtime.platform_agent_identity")
    _ensure_attr(pai, "set_loaded_mcp_tools", lambda tools: _recorded.append(list(tools)))


def _load_executor():
    _install_stubs()
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    sys.modules.pop("claude_sdk_executor", None)
    import claude_sdk_executor  # noqa: WPS433

    return claude_sdk_executor


def _make_executor(mod, tmp_path):
    return mod.ClaudeSDKExecutor(
        system_prompt=None,
        config_path=str(tmp_path),
        heartbeat=None,
        model="kimi-coding",
    )


@pytest.mark.asyncio
async def test_init_message_records_only_mcp_tools(tmp_path):
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    ex = _make_executor(mod, tmp_path)
    _recorded.clear()

    acc = ex._StreamAccumulator()
    msg = sdk.SystemMessage(
        subtype="init",
        data={
            "tools": [
                "Read",
                "Bash",
                "mcp__molecule-platform__create_workspace",
                "mcp__a2a__send_message",
            ]
        },
    )
    await ex._accumulate_message(msg, acc)

    # Built-in tools (Read/Bash) are filtered out; only mcp__* ids are reported.
    assert _recorded == [
        ["mcp__molecule-platform__create_workspace", "mcp__a2a__send_message"]
    ]


@pytest.mark.asyncio
async def test_non_init_system_message_is_ignored(tmp_path):
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    ex = _make_executor(mod, tmp_path)
    _recorded.clear()

    acc = ex._StreamAccumulator()
    await ex._accumulate_message(
        sdk.SystemMessage(subtype="status", data={"tools": ["mcp__x__y"]}), acc
    )
    assert _recorded == []


@pytest.mark.asyncio
async def test_init_message_with_no_tools_records_empty(tmp_path):
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    ex = _make_executor(mod, tmp_path)
    _recorded.clear()

    acc = ex._StreamAccumulator()
    await ex._accumulate_message(sdk.SystemMessage(subtype="init", data={"tools": []}), acc)
    # A turn ran but loaded no MCP tools — an empty list is a meaningful signal.
    assert _recorded == [[]]
