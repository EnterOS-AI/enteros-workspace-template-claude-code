"""Stuck-MCP readiness gate + auto-heal for the Claude Code runtime.

The concierge bug: the org-level platform agent declares a slow `platform`
MCP server (the molecule-mcp-server, ~88 org-admin tools) whose `node`
handshake takes ~5-8s. The one-shot `claude_agent_sdk.query()` ships its
`init` message — and the tool list the LLM sees — the instant the CLI boots,
while `platform` is still `status: pending`, so create_workspace et al. are
hidden from that turn. A fresh `query()` re-races the same handshake, so the
concierge intermittently "loses" its org-admin tools.

This suite pins the durable fix in `claude_sdk_executor.py`:

  (a) When the config declares extra (non-`a2a`) MCP servers, the turn is
      routed through a persistent `ClaudeSDKClient` and GATED on
      `get_mcp_status()` until every declared server is `connected` AND the
      SSOT-required tool (`provision_workspace`) is present in its callable
      `tools` list BEFORE the prompt is sent.
  (b) Ordinary workspaces (no extra MCP servers) keep the fast one-shot
      `query()` path untouched.
  (c) If a declared server is not ready (or is connected but missing the
      required tool), the gate raises `_McpNotReadyError`; the executor
      reloads the MCP server via `client.reconnect_mcp_server()` and re-gates,
      bounded by `_MCP_HEAL_MAX_RETRIES`.
  (d) The reload, finding the server now connected with the required tool,
      succeeds.
  (e) The reload loop is CAPPPED; after exhausting the retries the workspace
      fails-degraded (marked wedged) so the platform surfaces a Restart hint.

No network: `sdk.ClaudeSDKClient` is a scriptable stub. Mirrors
tests/test_context_overflow_autoheal.py's stub-install pattern.
"""

import os
import sys
import types
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest


# ---- SDK + dependency stubs (see test_context_overflow_autoheal.py) ----


def _ensure_module(dotted: str) -> types.ModuleType:
    if dotted not in sys.modules:
        sys.modules[dotted] = types.ModuleType(dotted)
    return sys.modules[dotted]


def _ensure_attr(mod: types.ModuleType, name: str, value: object) -> None:
    if not hasattr(mod, name):
        setattr(mod, name, value)


@dataclass
class _StubResultMessage:
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
    # Force-overwrite message classes so this file's stubs win even when other
    # test modules (e.g. test_extra_mcp_servers.py) have installed narrower stubs.
    sdk.AssistantMessage = _StubAssistantMessage
    sdk.TextBlock = _StubTextBlock
    sdk.ResultMessage = _StubResultMessage
    _ensure_attr(sdk, "query", MagicMock(name="query"))
    # ClaudeSDKClient is overridden per-test via mod.sdk.ClaudeSDKClient.
    _ensure_attr(sdk, "ClaudeSDKClient", MagicMock(name="ClaudeSDKClient"))

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


def _write_platform_config(tmp_path) -> str:
    """Write a config.yaml that declares the `platform` MCP server so the
    executor takes the readiness-gated path."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "name: concierge\n"
        "mcp_servers:\n"
        "  - name: platform\n"
        "    command: node\n"
        "    args:\n"
        "      - /opt/molecule-mcp-server/dist/index.js\n"
    )
    return str(tmp_path)


def _make_executor(mod, config_path, model="sonnet"):
    ex = mod.ClaudeSDKExecutor(
        system_prompt=None,
        config_path=config_path,
        heartbeat=None,
        model=model,
    )

    async def _noop_notify(*_a, **_kw):
        return None

    # Neutralize the best-effort activity-row notifications (no httpx).
    ex._notify_context_overflow_heal = _noop_notify  # type: ignore[assignment]
    return ex


# ---- Scriptable ClaudeSDKClient stub --------------------------------------


@dataclass
class _ClientScript:
    """Per-connect() behavior for a single turn.

    status_sequence: list of mcpServers-list snapshots returned by successive
        get_mcp_status() polls (last one repeats if polls exceed the list).
    response: list of messages yielded by receive_response().
    """
    status_sequence: list
    response: list = field(default_factory=list)


class _StubClient:
    """Records calls + replays a scripted _ClientScript. Class-level `_scripts`
    list is consumed one per connect(); reconnect_mcp_server() consumes the
    next script to simulate a fresh server load without tearing down the CLI.
    """

    _scripts: list = []
    instances: list = []

    def __init__(self, options=None):
        self.options = options
        self._script = None
        self._poll = 0
        self.queried = None
        self.connected = False
        self.disconnected = False
        self.reconnects: list[str] = []
        _StubClient.instances.append(self)

    async def connect(self, prompt=None):
        self.connected = True
        self._script = _StubClient._scripts.pop(0)

    async def get_mcp_status(self):
        seq = self._script.status_sequence
        idx = min(self._poll, len(seq) - 1)
        self._poll += 1
        return {"mcpServers": seq[idx]}

    async def query(self, prompt):
        self.queried = prompt

    async def receive_response(self):
        for m in self._script.response:
            yield m

    async def disconnect(self):
        self.disconnected = True

    async def reconnect_mcp_server(self, server_name: str):
        """Simulate a server reload: consume the next queued script (if any)
        and reset the status poll so the new handshake sequence replays."""
        self.reconnects.append(server_name)
        if _StubClient._scripts:
            next_script = _StubClient._scripts.pop(0)
            self._script.status_sequence = next_script.status_sequence
            if next_script.response:
                self._script.response = next_script.response
        self._poll = 0


def _install_client_scripts(mod, scripts):
    _StubClient._scripts = list(scripts)
    _StubClient.instances = []
    mod.sdk.ClaudeSDKClient = _StubClient


# Module-level ResultMessage builder (uses the installed sdk stub).
def mod_RM(**kw):
    import claude_agent_sdk as sdk
    return sdk.ResultMessage(**kw)


CONNECTED = [{"name": "platform", "status": "connected", "tools": ["provision_workspace", "other"]}]
PENDING = [{"name": "platform", "status": "pending"}]
FAILED = [{"name": "platform", "status": "failed", "error": "boom"}]
DISABLED = [{"name": "platform", "status": "disabled"}]
MISSING_TOOL = [{"name": "platform", "status": "connected", "tools": ["other"]}]


# ---- Tests ----------------------------------------------------------------


def test_ordinary_workspace_keeps_query_path(tmp_path):
    """No extra MCP servers declared → fast one-shot query() path, gated path
    never used."""
    mod = _load_executor()
    # empty config dir → no mcp_servers
    ex = _make_executor(mod, str(tmp_path))
    assert ex._declared_extra_mcp_names() == []


def test_platform_config_triggers_gated_path(tmp_path):
    mod = _load_executor()
    cfgdir = _write_platform_config(tmp_path)
    ex = _make_executor(mod, cfgdir)
    assert ex._declared_extra_mcp_names() == ["platform"]


@pytest.mark.asyncio
async def test_gate_waits_for_connected_then_sends_prompt(tmp_path):
    """The gate polls until `connected`, THEN sends the prompt. The prompt is
    never sent while the server is pending."""
    mod = _load_executor()
    cfgdir = _write_platform_config(tmp_path)
    ex = _make_executor(mod, cfgdir)
    # pending for 2 polls, then connected.
    _install_client_scripts(mod, [
        _ClientScript(
            status_sequence=[PENDING, PENDING, CONNECTED],
            response=[mod_RM(result="hi", session_id="s1")],
        ),
    ])
    # Drop the poll interval so the test is instant.
    mod._MCP_READY_POLL_INTERVAL_S = 0
    res = await ex._run_query("create a workspace", ex._build_options())
    assert res.text == "hi"
    client = _StubClient.instances[0]
    assert client.queried == "create a workspace", "prompt must be sent after gate"
    assert client.disconnected, "client torn down after turn"
    # At least 3 polls happened (pending, pending, connected).
    assert client._poll >= 3


@pytest.mark.asyncio
async def test_stuck_pending_raises_not_ready(tmp_path):
    """Server stuck pending forever → gate exhausts budget → _McpNotReadyError."""
    mod = _load_executor()
    cfgdir = _write_platform_config(tmp_path)
    ex = _make_executor(mod, cfgdir)
    mod._MCP_READY_POLL_INTERVAL_S = 0
    mod._MCP_READY_MAX_POLLS = 3
    _install_client_scripts(mod, [
        _ClientScript(status_sequence=[PENDING], response=[]),
    ])
    with pytest.raises(mod._McpNotReadyError) as ei:
        await ex._run_query("x", ex._build_options())
    assert ei.value.server == "platform"
    assert ei.value.status == "pending"


@pytest.mark.asyncio
async def test_disabled_status_raises_early(tmp_path):
    """A `disabled` status is hard-terminal — gate stops immediately, no
    waiting (a config/auth problem no reload can fix)."""
    mod = _load_executor()
    cfgdir = _write_platform_config(tmp_path)
    ex = _make_executor(mod, cfgdir)
    mod._MCP_READY_POLL_INTERVAL_S = 0
    _install_client_scripts(mod, [
        _ClientScript(status_sequence=[DISABLED], response=[]),
    ])
    with pytest.raises(mod._McpNotReadyError) as ei:
        await ex._run_query("x", ex._build_options())
    assert ei.value.status == "disabled"


@pytest.mark.asyncio
async def test_connected_missing_required_tool_raises_not_ready(tmp_path):
    """A server can report `connected` but not expose the SSOT-required
    management tool. The gate must treat that as NOT ready so the reload
    heal can try again."""
    mod = _load_executor()
    cfgdir = _write_platform_config(tmp_path)
    ex = _make_executor(mod, cfgdir)
    mod._MCP_READY_POLL_INTERVAL_S = 0
    mod._MCP_READY_MAX_POLLS = 1
    _install_client_scripts(mod, [
        _ClientScript(status_sequence=[MISSING_TOOL], response=[]),
    ])
    with pytest.raises(mod._McpNotReadyError) as ei:
        await ex._run_query("x", ex._build_options())
    assert ei.value.server == "platform"
    assert "missing-provision_workspace" in ei.value.status


@pytest.mark.asyncio
async def test_failed_is_retryable_not_terminal(tmp_path):
    """A `failed` status is RETRYABLE (intermittent under load), not hard-
    terminal: the gate reloads the server with `reconnect_mcp_server()`,
    the reload handshake succeeds, and the turn completes."""
    mod = _load_executor()
    cfgdir = _write_platform_config(tmp_path)
    ex = _make_executor(mod, cfgdir)
    mod._MCP_READY_POLL_INTERVAL_S = 0
    mod._MCP_READY_MAX_POLLS = 1
    _install_client_scripts(mod, [
        # Initial load: server failed.
        _ClientScript(status_sequence=[FAILED], response=[]),
        # After reconnect: server healthy.
        _ClientScript(
            status_sequence=[CONNECTED],
            response=[mod_RM(result="recovered", session_id="s2")],
        ),
    ])
    out = await ex._execute_locked("create a workspace")
    assert out == "recovered"
    client = _StubClient.instances[0]
    assert client.reconnects == ["platform"]
    # Same CLI subprocess / client is reused across the reload.
    assert len(_StubClient.instances) == 1


@pytest.mark.asyncio
async def test_heal_reloads_server_then_succeeds(tmp_path, caplog):
    """First turn: stuck pending → _McpNotReadyError → reload server with
    reconnect_mcp_server(). Second load: connected → success. Exactly one
    reload, one client instance."""
    import logging
    mod = _load_executor()
    cfgdir = _write_platform_config(tmp_path)
    ex = _make_executor(mod, cfgdir)
    ex._session_id = "old-session"
    mod._MCP_READY_POLL_INTERVAL_S = 0
    mod._MCP_READY_MAX_POLLS = 2
    # Turn 1: stuck pending (reload fires). Turn 2: connected (succeeds).
    _install_client_scripts(mod, [
        _ClientScript(status_sequence=[PENDING], response=[]),
        _ClientScript(
            status_sequence=[CONNECTED],
            response=[mod_RM(result="created workspace e2e", session_id="s2")],
        ),
    ])
    with caplog.at_level(logging.ERROR):
        out = await ex._execute_locked("create a workspace")
    assert out == "created workspace e2e"
    assert ex._session_id == "s2"
    # Loud ERROR log on heal.
    assert any("reloading MCP server" in r.message for r in caplog.records)
    # One connect; reconnect reloads within the same client.
    assert len(_StubClient.instances) == 1
    assert _StubClient.instances[0].reconnects == ["platform"]


@pytest.mark.asyncio
async def test_heal_bounded_no_infinite_loop(tmp_path, caplog):
    """Server stuck pending on the original load AND every reload → no infinite
    loop; the workspace fails-degraded (wedged) after exactly
    `_MCP_HEAL_MAX_RETRIES` reload attempts."""
    import logging
    mod = _load_executor()
    mod._reset_sdk_wedge_for_test()
    cfgdir = _write_platform_config(tmp_path)
    ex = _make_executor(mod, cfgdir)
    mod._MCP_READY_POLL_INTERVAL_S = 0
    mod._MCP_READY_MAX_POLLS = 1
    # Original load + one script per allowed reload, all stuck pending.
    total_attempts = 1 + mod._MCP_HEAL_MAX_RETRIES
    _install_client_scripts(
        mod, [_ClientScript(status_sequence=[PENDING], response=[])] * total_attempts
    )
    with caplog.at_level(logging.ERROR):
        out = await ex._execute_locked("create a workspace")
    # Sanitized hard error, not an infinite loop.
    assert "Agent error" in out
    assert any("stuck-MCP auto-heal exhausted" in r.message for r in caplog.records)
    # Exactly `_MCP_HEAL_MAX_RETRIES` reconnect calls, all on one client.
    assert len(_StubClient.instances) == 1
    assert len(_StubClient.instances[0].reconnects) == mod._MCP_HEAL_MAX_RETRIES
    # Exhausted reloads fail-degraded.
    assert mod.is_wedged()


@pytest.mark.asyncio
async def test_heal_does_not_mark_wedge(tmp_path):
    """A stuck-MCP heal must NOT flip the workspace to degraded — it recovers
    on the retried turn."""
    mod = _load_executor()
    mod._reset_sdk_wedge_for_test()
    cfgdir = _write_platform_config(tmp_path)
    ex = _make_executor(mod, cfgdir)
    mod._MCP_READY_POLL_INTERVAL_S = 0
    mod._MCP_READY_MAX_POLLS = 1
    _install_client_scripts(mod, [
        _ClientScript(status_sequence=[PENDING], response=[]),
        _ClientScript(
            status_sequence=[CONNECTED],
            response=[mod_RM(result="ok", session_id="s")],
        ),
    ])
    await ex._execute_locked("create a workspace")
    assert not mod.is_wedged()
