"""#2748 observability: opaque engine `_ResultError` must self-diagnose.

The agents-team engines surfaced a useless
``Agent error (_ResultError) — see workspace logs for details`` whenever
the claude-agent-sdk yielded a terminal ``is_error=True`` ResultMessage
whose ``result`` field was ``None`` (the engine fault path, distinct from
the context-overflow path where ``result`` carries the proxy 400 text).

Root cause: the executor raised ``_ResultError(result_text or "")`` — an
EMPTY string — even though the same ResultMessage carried richer
diagnostic fields the runtime threw away:
  * ``subtype``           — e.g. "error_during_execution", "error_max_turns"
  * ``api_error_status``  — the upstream HTTP status (401/429/404/500/…)
  * ``errors``            — a list of short error strings

Fix: ``_result_error_detail()`` keeps ``result`` when present (so the
overflow classifier is unchanged) and otherwise builds a concise,
non-secret detail from ``subtype`` + ``api_error_status`` (+ a capped
``errors`` summary), so the opaque case becomes e.g.
``error_during_execution (api_error_status=401)``.

These tests pin that contract directly against ``_run_query`` (the
one-shot path) and the helper. No network: ``sdk.query`` is a scripted
stub async generator, mirroring tests/test_context_overflow_autoheal.py.
"""

import os
import sys
import types
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest


def _ensure_module(dotted: str) -> types.ModuleType:
    if dotted not in sys.modules:
        sys.modules[dotted] = types.ModuleType(dotted)
    return sys.modules[dotted]


def _ensure_attr(mod: types.ModuleType, name: str, value: object) -> None:
    if not hasattr(mod, name):
        setattr(mod, name, value)


@dataclass
class _StubResultMessage:
    """Mirrors the real claude_agent_sdk.ResultMessage fields the executor
    reads. Verified against claude-agent-sdk ResultMessage (subtype:str,
    is_error:bool, result:str|None, api_error_status:int|None,
    errors:list[str]|None)."""
    result: str | None = None
    session_id: str | None = "sess-1"
    is_error: bool = False
    subtype: str = "success"
    api_error_status: int | None = None
    errors: list | None = None


@dataclass
class _StubAssistantMessage:
    content: list = None


@dataclass
class _StubTextBlock:
    text: str = ""


def _install_stubs() -> None:
    sdk = _ensure_module("claude_agent_sdk")
    _ensure_attr(sdk, "ClaudeAgentOptions", MagicMock(name="ClaudeAgentOptions"))
    _ensure_attr(sdk, "AssistantMessage", _StubAssistantMessage)
    _ensure_attr(sdk, "TextBlock", _StubTextBlock)
    # Force our richer stub even if a sibling test module already
    # registered a leaner _StubResultMessage on the shared
    # `claude_agent_sdk` module (test-ordering coupling). Ours is a
    # strict superset (adds api_error_status + errors, both optional),
    # so overwriting is safe for every consumer.
    sdk.ResultMessage = _StubResultMessage
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
    _ensure_attr(helpers, "sanitize_agent_error", lambda exc=None, category=None, stderr=None: (f"Agent error ({type(exc).__name__}): {stderr}" if stderr else f"Agent error: {exc}"))
    _ensure_attr(helpers, "error_detail_for_external", lambda exc: str(exc) or None)
    _ensure_attr(helpers, "set_current_task", _async_noop)


def _load_executor():
    _install_stubs()
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    sys.modules.pop("claude_sdk_executor", None)
    import claude_sdk_executor  # noqa: WPS433
    return claude_sdk_executor


def _make_executor(mod, tmp_path, model="kimi-coding"):
    ex = mod.ClaudeSDKExecutor(
        system_prompt=None,
        config_path=str(tmp_path),
        heartbeat=None,
        model=model,
    )

    async def _noop_notify(_detail):
        return None

    ex._notify_context_overflow_heal = _noop_notify  # type: ignore[assignment]
    return ex


def _script_single(mod, messages):
    """Install a `sdk.query` stub that yields `messages` once (then repeats)."""
    sdk = sys.modules["claude_agent_sdk"]

    def query(*_args, **_kwargs):
        async def _gen():
            for m in messages:
                yield m

        return _gen()

    sdk.query = query


# --------------------------- helper unit tests ---------------------------


def test_detail_prefers_result_text_when_present(tmp_path):
    """When the ResultMessage carried a `result` string (overflow path),
    the helper returns it verbatim — the overflow classifier is unchanged."""
    mod = _load_executor()
    out = mod._result_error_detail(
        "token limit 262144 requested 268132",
        subtype="success",
        api_error_status=None,
    )
    assert out == "token limit 262144 requested 268132"


def test_detail_synthesizes_subtype_and_status_when_empty(tmp_path):
    """The opaque case: result=None but subtype + api_error_status present →
    a self-diagnosing, non-secret string."""
    mod = _load_executor()
    out = mod._result_error_detail(
        None,
        subtype="error_during_execution",
        api_error_status=401,
    )
    assert "error_during_execution" in out
    assert "401" in out
    assert "api_error_status" in out


def test_detail_subtype_only(tmp_path):
    """subtype present, no api_error_status (e.g. error_max_turns) → still
    better than empty."""
    mod = _load_executor()
    out = mod._result_error_detail(None, subtype="error_max_turns", api_error_status=None)
    assert out == "error_max_turns"


def test_detail_includes_capped_errors_summary(tmp_path):
    mod = _load_executor()
    out = mod._result_error_detail(
        None,
        subtype="error_during_execution",
        api_error_status=500,
        errors=["upstream 500", "retry budget exhausted"],
    )
    assert "error_during_execution" in out and "500" in out
    assert "upstream 500" in out
    # cap: a pathological errors list can't dominate the message.
    long = mod._result_error_detail(None, subtype="x", errors=["z" * 1000])
    assert len(long) < 400


def test_detail_no_detail_at_all_is_graceful(tmp_path):
    """No result, no subtype, no status — must NOT return "" (that's the
    opaque bug). A generic-but-honest marker is the floor."""
    mod = _load_executor()
    out = mod._result_error_detail(None, subtype=None, api_error_status=None, errors=None)
    assert out != ""
    assert "engine error" in out.lower()


# ----------------------- end-to-end via _run_query -----------------------


@pytest.mark.asyncio
async def test_opaque_is_error_result_carries_subtype_and_status(tmp_path):
    """THE BUG. SDK yields is_error=True with result=None (engine fault) +
    subtype="error_during_execution" + api_error_status=401. `_run_query`
    must raise a `_ResultError` whose text contains the subtype and status —
    no more empty `_ResultError("")` → no more "see workspace logs"."""
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    ex = _make_executor(mod, tmp_path)

    _script_single(mod, [sdk.ResultMessage(
        result=None,
        is_error=True,
        subtype="error_during_execution",
        api_error_status=401,
        session_id="sess-x",
    )])

    with pytest.raises(mod._ResultError) as ei:
        await ex._run_query(prompt="hi", options=object())

    text = ei.value.text
    assert "error_during_execution" in text
    assert "401" in text
    # The external rendering the A2A response surfaces (mirrors the runtime
    # `f"{type(exc).__name__}: {exc}"` shape) now self-diagnoses.
    assert text != ""


@pytest.mark.asyncio
async def test_result_present_is_unchanged(tmp_path):
    """Regression guard: when result IS present, behavior is identical to
    before — the overflow text is preserved verbatim."""
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    ex = _make_executor(mod, tmp_path)

    _script_single(mod, [sdk.ResultMessage(
        result="token limit 262144 requested 268132",
        is_error=True,
        subtype="success",
        session_id="sess-bloated",
    )])

    with pytest.raises(mod._ResultError) as ei:
        await ex._run_query(prompt="hi", options=object())
    assert ei.value.text == "token limit 262144 requested 268132"


@pytest.mark.asyncio
async def test_opaque_with_no_detail_still_better_than_empty(tmp_path):
    """Edge: is_error=True, result=None, subtype falsy, no status — the
    raised _ResultError must still carry a non-empty, honest marker so the
    A2A response is never the bare opaque string again."""
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    ex = _make_executor(mod, tmp_path)

    _script_single(mod, [sdk.ResultMessage(
        result=None,
        is_error=True,
        subtype="",
        api_error_status=None,
        session_id="sess-x",
    )])

    with pytest.raises(mod._ResultError) as ei:
        await ex._run_query(prompt="hi", options=object())
    assert ei.value.text != ""
