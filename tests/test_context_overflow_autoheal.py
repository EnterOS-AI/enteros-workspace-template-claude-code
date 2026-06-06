"""Context-window-overflow auto-heal for the Claude Code runtime.

The Kimi wedge: once the accumulated claude-code session transcript grows
past the model's context window, every subsequent dispatch resumes that
oversized session and the upstream rejects it identically
(`token limit 262144 requested 268132`). The agent is stuck — no message
ever succeeds — until a manual workspace restart.

This suite pins the durable self-heal built into `claude_sdk_executor.py`:

  (a) DETECT the overflow whether it surfaces as a raised SDK exception OR
      as an ``is_error`` ResultMessage (the model-proxy 400 path).
  (b) RESET the session (resume=None + purge the bloated on-disk
      transcript) on detection.
  (c) RETRY once on the fresh session.
  (d) LOG loudly (ERROR-level "auto-heal: session reset on context-overflow").
  (e) CAP the heal at one reset per dispatch — a second overflow on a
      fresh session is a hard error, not an infinite reset loop.
  (f) classify a rate-limit as NOT an overflow (opposite remedy).
  (g) PREVENT proactively: pin CLAUDE_CODE_MAX_CONTEXT_TOKENS from the
      model's real window so claude-code's auto-compact uses the right
      number.

No network: the SDK ``query`` is a stub async generator returning scripted
messages. Mirrors tests/test_dev_channels_flag.py's stub-install pattern.
"""

import os
import sys
import types
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest


# ---- SDK + dependency stubs (see test_dev_channels_flag.py rationale) ----


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
    reads: result (text), session_id, is_error."""
    result: str | None = None
    session_id: str | None = "sess-1"
    is_error: bool = False
    subtype: str = "success"


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
    _ensure_attr(sdk, "ResultMessage", _StubResultMessage)
    # query is overridden per-test; a default keeps import-time happy.
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
    _ensure_attr(helpers, "sanitize_agent_error", lambda e: f"Agent error: {e}")
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
    # Neutralize the best-effort notification so tests don't touch httpx.
    async def _noop_notify(_detail):
        return None

    ex._notify_context_overflow_heal = _noop_notify  # type: ignore[assignment]
    return ex


def _script_query(mod, scripts):
    """Install a stub `sdk.query` that returns scripted outcomes per call.

    `scripts` is a list of callables; the Nth `query()` invocation runs
    scripts[N]. Each callable either returns an iterable of messages to
    yield, or raises (to simulate a raised SDK exception).

    REAL-SDK SHAPE: a callable may return a `_YieldThenRaise(messages, exc)`
    sentinel instead of a plain iterable. The generator then yields each
    message FIRST and THEN raises `exc` — modeling the actual claude CLI /
    SDK 0.1.72 behavior on a terminal-error result, where `query()` yields
    the `is_error=True` ResultMessage and immediately afterwards raises
    `Exception("Command failed with exit code 1")` because the CLI exits
    non-zero. This is the shape the prior stub could NOT express, and the
    gap that hid the bug.
    """
    sdk = sys.modules["claude_agent_sdk"]
    calls = {"n": 0}

    def query(*_args, **_kwargs):
        idx = calls["n"]
        calls["n"] += 1
        script = scripts[min(idx, len(scripts) - 1)]
        produced = script()

        async def _gen():
            if isinstance(produced, _YieldThenRaise):
                for msg in produced.messages:
                    yield msg
                raise produced.exc
            for msg in produced:
                yield msg

        return _gen()

    sdk.query = query
    return calls


@dataclass
class _YieldThenRaise:
    """Sentinel for `_script_query`: yield `messages`, then raise `exc`.

    Models the real claude CLI 2.1.163 / SDK 0.1.72 terminal-error shape —
    the `is_error=True` ResultMessage is yielded AND THEN `query()` raises
    `Exception("Command failed with exit code 1")` (the CLI exits non-zero
    on any terminal error result)."""
    messages: list
    exc: BaseException


# Anthropic-native and proxy-shaped overflow strings the classifier must catch.
_OVERFLOW_TEXTS = [
    "token limit 262144 requested 268132",      # Kimi via molecule proxy
    "Prompt is too long",                         # Anthropic-native 400
    "input length and `max_tokens` exceed context window",
    "This model's maximum context length is 262144 tokens",
]


# ----------------------------- (a) detection -----------------------------


def test_classifier_matches_overflow_strings(tmp_path):
    mod = _load_executor()
    for text in _OVERFLOW_TEXTS:
        assert mod._is_context_overflow(text), f"missed overflow string: {text!r}"


def test_classifier_does_not_match_rate_limit(tmp_path):
    """A rate-limit is NOT a context overflow — it needs backoff+retry on
    the SAME session, not a session reset."""
    mod = _load_executor()
    for text in ("429 rate limit exceeded", "overloaded, try again", "capacity"):
        assert not mod._is_context_overflow(text), f"false overflow: {text!r}"


@pytest.mark.asyncio
async def test_is_error_result_message_raises(tmp_path):
    """The model-proxy 400 path: SDK emits is_error=True ResultMessage
    (no raise). _run_query must re-raise it as _ResultError so it reaches
    the heal classifier."""
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    ex = _make_executor(mod, tmp_path)

    def _err_stream():
        return [sdk.ResultMessage(
            result="token limit 262144 requested 268132",
            is_error=True,
            session_id="sess-bloated",
        )]

    _script_query(mod, [_err_stream])

    with pytest.raises(mod._ResultError) as ei:
        await ex._run_query(prompt="hi", options=object())
    assert "token limit" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_is_error_result_then_cli_raises_preserves_overflow_text(tmp_path):
    """REAL-SDK SHAPE (the bug). claude CLI 2.1.163 / SDK 0.1.72 yields the
    `is_error=True` ResultMessage AND THEN raises
    `Exception("Command failed with exit code 1")` because the CLI process
    exits non-zero on a terminal error result.

    `_run_query` must re-raise `_ResultError` carrying the captured overflow
    text IN PREFERENCE to the trailing generic CLI exception — otherwise the
    overflow text is lost and `_is_context_overflow` (which does NOT match
    'Command failed with exit code 1') never fires the heal.
    """
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    ex = _make_executor(mod, tmp_path)

    def _err_then_raise():
        return _YieldThenRaise(
            messages=[sdk.ResultMessage(
                result="token limit 262144 requested 268132",
                is_error=True,
                session_id="sess-bloated",
            )],
            exc=Exception("Command failed with exit code 1\nCheck stderr output for details"),
        )

    _script_query(mod, [_err_then_raise])

    with pytest.raises(mod._ResultError) as ei:
        await ex._run_query(prompt="hi", options=object())
    # The OVERFLOW text must be preserved, not the generic CLI exception.
    assert "token limit" in str(ei.value).lower(), (
        "overflow text was lost — the generic 'Command failed' exception "
        "pre-empted the captured _ResultError"
    )


# ------------------- (b)(c)(d) reset + retry + loud log ------------------


@pytest.mark.asyncio
async def test_overflow_heals_resets_and_retries(tmp_path, caplog):
    """First dispatch overflows (is_error result) → executor resets the
    session, retries, and the retry succeeds. End-to-end through
    _execute_locked."""
    import logging
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    ex = _make_executor(mod, tmp_path)
    ex._session_id = "sess-bloated"  # pretend a prior turn set a session

    def _overflow():
        return [sdk.ResultMessage(
            result="token limit 262144 requested 268132",
            is_error=True,
            session_id="sess-bloated",
        )]

    def _ok():
        return [sdk.ResultMessage(
            result="hello from a fresh session",
            is_error=False,
            session_id="sess-fresh",
        )]

    _script_query(mod, [_overflow, _ok])

    with caplog.at_level(logging.ERROR):
        out = await ex._execute_locked("do the thing")

    assert out == "hello from a fresh session"
    # (b) session was reset then re-set to the fresh id by the successful retry.
    assert ex._session_id == "sess-fresh"
    # (d) loud structured log.
    assert any(
        "auto-heal: session reset on context-overflow" in r.getMessage()
        for r in caplog.records
    ), "missing the loud auto-heal log line"


@pytest.mark.asyncio
async def test_overflow_heals_on_real_sdk_shape(tmp_path, caplog):
    """REAL-SDK SHAPE end-to-end (the bug, reproduced through the full
    `_execute_locked` heal path).

    First dispatch: yield the `is_error=True` overflow ResultMessage AND
    THEN raise `Exception("Command failed with exit code 1")` — exactly what
    the real claude CLI 2.1.163 / SDK 0.1.72 does on a context overflow.
    Asserts: the overflow IS detected, the session IS reset, exactly one
    retry happens on a fresh session, and the loud auto-heal log fires.

    FAILS against the buggy code (the generic CLI exception pre-empts the
    captured overflow text → `_is_context_overflow` never matches → no heal,
    the generic error is returned as the reply). PASSES after the fix.
    """
    import logging
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    ex = _make_executor(mod, tmp_path)
    ex._session_id = "sess-bloated"  # a prior turn bloated the session

    def _overflow_then_cli_raise():
        return _YieldThenRaise(
            messages=[sdk.ResultMessage(
                result="token limit 262144 requested 268132",
                is_error=True,
                session_id="sess-bloated",
            )],
            exc=Exception("Command failed with exit code 1\nCheck stderr output for details"),
        )

    def _ok():
        return [sdk.ResultMessage(
            result="hello from a fresh session",
            is_error=False,
            session_id="sess-fresh",
        )]

    calls = _script_query(mod, [_overflow_then_cli_raise, _ok])

    with caplog.at_level(logging.ERROR):
        out = await ex._execute_locked("do the thing")

    # (c) exactly one retry: original (overflow) + one fresh-session retry.
    assert calls["n"] == 2, (
        f"expected 2 query() calls (overflow + 1 heal retry), got {calls['n']}"
    )
    # (a)+(c) overflow detected and the fresh-session retry succeeded.
    assert out == "hello from a fresh session"
    # (b) session reset then re-set to the fresh id by the successful retry.
    assert ex._session_id == "sess-fresh"
    # (d) loud structured log fired (proves the heal path, not a fluke pass).
    assert any(
        "auto-heal: session reset on context-overflow" in r.getMessage()
        for r in caplog.records
    ), "missing the loud auto-heal log — overflow was not detected on the real shape"


@pytest.mark.asyncio
async def test_overflow_heal_purges_on_disk_transcripts(tmp_path, monkeypatch):
    """The bloated transcript on disk is the cause — the heal purges the
    session *.jsonl files so they can't be resumed."""
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    # Point the SDK config dir at a temp tree with a fake bloated transcript.
    cfg = tmp_path / "claude"
    proj = cfg / "projects" / "-workspace"
    proj.mkdir(parents=True)
    bloated = proj / "sess-bloated.jsonl"
    bloated.write_text('{"big": "transcript"}\n')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))

    ex = _make_executor(mod, tmp_path)
    ex._session_id = "sess-bloated"

    def _overflow():
        return [sdk.ResultMessage(result="Prompt is too long", is_error=True)]

    def _ok():
        return [sdk.ResultMessage(result="ok", is_error=False, session_id="s2")]

    _script_query(mod, [_overflow, _ok])
    await ex._execute_locked("hi")

    assert not bloated.exists(), "stale session transcript was not purged"


# ------------------------------ (e) bounded ------------------------------


@pytest.mark.asyncio
async def test_overflow_heal_capped_at_one_reset(tmp_path):
    """A second overflow on the FRESH session means the single prompt is
    too big — a reset can't fix it. Must surface a hard error, NOT loop the
    reset. _run_query must have been called exactly twice (original +
    one heal retry), never a third time."""
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    ex = _make_executor(mod, tmp_path)

    def _overflow():
        return [sdk.ResultMessage(
            result="token limit 262144 requested 999999", is_error=True,
        )]

    calls = _script_query(mod, [_overflow, _overflow, _overflow])
    out = await ex._execute_locked("a huge single prompt")

    assert calls["n"] == 2, (
        f"expected exactly 2 query() calls (original + 1 heal retry), "
        f"got {calls['n']} — the reset is looping instead of being capped"
    )
    # Hard, sanitized error surfaced (not a silent hang, not a fake reply).
    assert "Agent error" in out


@pytest.mark.asyncio
async def test_overflow_heal_does_not_mark_wedge(tmp_path):
    """A context overflow self-recovers — it must NOT flip the workspace to
    `degraded` (that's for non-recoverable init wedges). The runtime wedge
    flag must stay clear after a successful heal."""
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    mod._reset_sdk_wedge_for_test()
    ex = _make_executor(mod, tmp_path)

    def _overflow():
        return [sdk.ResultMessage(result="Prompt is too long", is_error=True)]

    def _ok():
        return [sdk.ResultMessage(result="recovered", is_error=False, session_id="s")]

    _script_query(mod, [_overflow, _ok])
    await ex._execute_locked("hi")

    assert not mod.is_wedged(), "context-overflow heal must not mark a runtime wedge"


# ------------------ (g) proactive prevention (deeper fix) ----------------


def test_context_window_env_set_from_config(tmp_path, monkeypatch):
    """_maybe_set_context_window_env pins CLAUDE_CODE_MAX_CONTEXT_TOKENS from
    config.yaml so claude-code's auto-compact uses the model's REAL window
    instead of its 200k fallback for proxy-routed models."""
    mod = _load_executor()
    monkeypatch.delenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("MODEL_CONTEXT_WINDOW", raising=False)
    (tmp_path / "config.yaml").write_text("context_window: 262144\n")

    ex = _make_executor(mod, tmp_path)
    ex._maybe_set_context_window_env()

    assert os.environ.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") == "262144"


def test_context_window_env_prefers_explicit_env(tmp_path, monkeypatch):
    mod = _load_executor()
    monkeypatch.delenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW", "131072")
    (tmp_path / "config.yaml").write_text("context_window: 262144\n")

    ex = _make_executor(mod, tmp_path)
    ex._maybe_set_context_window_env()

    assert os.environ.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") == "131072"


def test_context_window_env_noop_when_unconfigured(tmp_path, monkeypatch):
    """No window configured (e.g. an Anthropic model whose resolver is
    already correct) → leave the env untouched, no regression."""
    mod = _load_executor()
    monkeypatch.delenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("MODEL_CONTEXT_WINDOW", raising=False)
    (tmp_path / "config.yaml").write_text("name: test\n")

    ex = _make_executor(mod, tmp_path, model="sonnet")
    ex._maybe_set_context_window_env()

    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in os.environ


def test_context_window_env_does_not_clobber_operator_pin(tmp_path, monkeypatch):
    mod = _load_executor()
    monkeypatch.setenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "500000")
    (tmp_path / "config.yaml").write_text("context_window: 262144\n")

    ex = _make_executor(mod, tmp_path)
    ex._maybe_set_context_window_env()

    assert os.environ.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") == "500000"
