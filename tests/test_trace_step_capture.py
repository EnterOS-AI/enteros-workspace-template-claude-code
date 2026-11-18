"""Langfuse step capture (runtime #305): the claude-code executor records the
turn's ordered thinking + tool_call steps (SSOT AgentTrace.steps) onto
`_last_steps` / `_last_tool_uses` / `_last_tool_calls`, which
molecule_runtime.tracing.TracingExecutor reads off the wrapped inner.

Tests the REAL block-parsing path (`_block_to_step` + `_accumulate_message`) —
not a fake that bypasses it — so a green test means the actual Agent-SDK block
stream produces conformant steps. Mirrors the SDK/a2a/runtime stub harness in
tests/test_loaded_mcp_tools_capture.py.
"""
import os
import sys
import types
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest


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


# Class NAME must be exactly ToolUseBlock — the executor dispatches on
# type(block).__name__ (the real SDK class isn't importable in CI).
@dataclass
class ToolUseBlock:
    name: str = ""
    input: dict = field(default_factory=dict)
    id: str = "tu-1"


@dataclass
class _ThinkingBlock:
    thinking: str = ""


def _install_stubs() -> None:
    sdk = _ensure_module("claude_agent_sdk")
    _ensure_attr(sdk, "ClaudeAgentOptions", MagicMock(name="ClaudeAgentOptions"))
    _ensure_attr(sdk, "AssistantMessage", _StubAssistantMessage)
    _ensure_attr(sdk, "TextBlock", _StubTextBlock)
    _ensure_attr(sdk, "ResultMessage", _StubResultMessage)
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

    for name in ("auto_push_hook", "commit_memory", "set_current_task"):
        _ensure_attr(helpers, name, _async_noop)
    _ensure_attr(helpers, "brief_summary", lambda *a, **kw: "")
    _ensure_attr(helpers, "collect_outbound_files", lambda *a, **kw: [])
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
    _ensure_attr(helpers, "sanitize_agent_error",
                 lambda exc=None, category=None, stderr=None: f"Agent error: {exc}")
    _ensure_attr(helpers, "error_detail_for_external", lambda exc: str(exc) or None)


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
        system_prompt=None, config_path=str(tmp_path), heartbeat=None, model="sonnet",
    )


# --------------------------- _block_to_step (pure) ---------------------------


def test_block_to_step_thinking_object(tmp_path):
    mod = _load_executor()
    assert mod._block_to_step(_ThinkingBlock(thinking="let me look")) == {
        "kind": "thinking", "text": "let me look",
    }


def test_block_to_step_thinking_dict(tmp_path):
    mod = _load_executor()
    assert mod._block_to_step({"type": "thinking", "thinking": "hmm"}) == {
        "kind": "thinking", "text": "hmm",
    }


def test_block_to_step_tool_use_with_input(tmp_path):
    mod = _load_executor()
    step = mod._block_to_step(ToolUseBlock(name="Bash", input={"cmd": "ls"}))
    assert step["kind"] == "tool_call"
    assert step["name"] == "Bash"
    assert step["input"] == "{'cmd': 'ls'}"        # serialized to a string per contract
    assert "result" not in step                    # absent by contract on this runtime


def test_block_to_step_tool_use_without_input(tmp_path):
    mod = _load_executor()
    step = mod._block_to_step(ToolUseBlock(name="list_peers", input={}))
    assert step == {"kind": "tool_call", "name": "list_peers"}  # empty input omitted


def test_block_to_step_text_and_unknown_return_none(tmp_path):
    mod = _load_executor()
    assert mod._block_to_step(_StubTextBlock(text="hi")) is None
    assert mod._block_to_step(object()) is None


# ---------------------- _accumulate_message (gated path) ---------------------


@pytest.mark.asyncio
async def test_accumulate_message_builds_ordered_steps(tmp_path, monkeypatch):
    """The SDK streams reasoning, tool calls, and final text as SEPARATE
    AssistantMessages; _accumulate_message folds each into the shared acc.
    Steps accumulate in stream order across the calls."""
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    ex = _make_executor(mod, tmp_path)

    async def _noop(_block):
        return None

    monkeypatch.setattr(mod, "_report_tool_use", _noop)  # no fire-and-forget HTTP

    acc = ex._StreamAccumulator()
    for block in (
        _ThinkingBlock(thinking="I'll list peers"),
        ToolUseBlock(name="list_peers", input={"scope": "org"}),
        _StubTextBlock(text="Here's who's around."),
    ):
        await ex._accumulate_message(sdk.AssistantMessage(content=[block]), acc)

    # Ordered thinking → tool_call captured; TextBlock is output, not a step.
    assert acc.steps == [
        {"kind": "thinking", "text": "I'll list peers"},
        {"kind": "tool_call", "name": "list_peers", "input": "{'scope': 'org'}"},
    ]
    assert acc.tool_uses == ["list_peers"]                 # existing behavior intact
    # (thinking text is also folded into assistant_chunks by pre-existing
    # behavior; the reply text is present — we don't over-assert on that here.)
    assert "Here's who's around." in acc.assistant_chunks


@pytest.mark.asyncio
async def test_accumulate_message_captures_tool_use_after_thinking_same_message(
    tmp_path, monkeypatch
):
    """Regression: a single AssistantMessage carrying [thinking, tool_use] must
    capture BOTH — a prior `return` (vs `continue`) after the thinking block
    exited the message early and dropped the following tool_use (its step, its
    tool_uses entry, and its _report_tool_use telemetry). Aligns
    _accumulate_message with the fast _run_query path."""
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    ex = _make_executor(mod, tmp_path)

    reported: list = []

    async def _spy(block):
        reported.append(getattr(block, "name", ""))

    monkeypatch.setattr(mod, "_report_tool_use", _spy)

    acc = ex._StreamAccumulator()
    await ex._accumulate_message(
        sdk.AssistantMessage(content=[
            _ThinkingBlock(thinking="I'll check first"),
            ToolUseBlock(name="delegate_task", input={"task": "x"}),
        ]),
        acc,
    )

    assert acc.steps == [
        {"kind": "thinking", "text": "I'll check first"},
        {"kind": "tool_call", "name": "delegate_task", "input": "{'task': 'x'}"},
    ]
    assert acc.tool_uses == ["delegate_task"]      # was dropped before the fix
    assert reported == ["delegate_task"]           # telemetry no longer skipped
