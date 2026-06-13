"""Unit tests for config-driven extra MCP servers (_apply_extra_mcp_servers).

The org-level platform agent declares a second MCP server (the platform-
management MCP) in its config.yaml under ``mcp_servers:``; ordinary workspaces
declare none. These pin: the platform server is merged, the built-in ``a2a``
server is always preserved and never overridden, env blocks pass through, and
malformed entries are skipped rather than crashing the executor.

(RFC: molecule-core docs/design/rfc-platform-agent.md)
"""

import os
import sys
import types
from unittest.mock import MagicMock


# ---- SDK + dependency stubs (see test_dev_channels_flag.py rationale) ----


def _ensure_module(dotted: str) -> types.ModuleType:
    if dotted not in sys.modules:
        sys.modules[dotted] = types.ModuleType(dotted)
    return sys.modules[dotted]


def _ensure_attr(mod: types.ModuleType, name: str, value: object) -> None:
    if not hasattr(mod, name):
        setattr(mod, name, value)


def _install_stubs() -> None:
    sdk = _ensure_module("claude_agent_sdk")
    _ensure_attr(sdk, "ClaudeAgentOptions", MagicMock(name="ClaudeAgentOptions"))
    _ensure_attr(sdk, "AssistantMessage", type("AssistantMessage", (), {}))
    _ensure_attr(sdk, "TextBlock", type("TextBlock", (), {}))
    _ensure_attr(sdk, "ResultMessage", type("ResultMessage", (), {}))
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


def _base():
    return {"a2a": {"command": "py", "args": ["/a2a"]}}


def test_no_extra_servers_is_noop():
    mod = _load_executor()
    assert list(mod._apply_extra_mcp_servers(_base(), {}).keys()) == ["a2a"]
    assert list(mod._apply_extra_mcp_servers(_base(), {"mcp_servers": []}).keys()) == ["a2a"]
    assert list(mod._apply_extra_mcp_servers(_base(), {"mcp_servers": None}).keys()) == ["a2a"]


def test_platform_server_merged():
    mod = _load_executor()
    cfg = {"mcp_servers": [{
        "name": "platform",
        "command": "node",
        "args": ["/opt/molecule-mcp-server/dist/index.js"],
    }]}
    out = mod._apply_extra_mcp_servers(_base(), cfg)
    assert set(out.keys()) == {"a2a", "platform"}
    assert out["platform"] == {"command": "node", "args": ["/opt/molecule-mcp-server/dist/index.js"]}


def test_env_block_passed_through():
    mod = _load_executor()
    cfg = {"mcp_servers": [{"name": "platform", "command": "node", "env": {"MOLECULE_API_KEY": "x"}}]}
    out = mod._apply_extra_mcp_servers(_base(), cfg)
    assert out["platform"]["env"] == {"MOLECULE_API_KEY": "x"}
    assert out["platform"]["args"] == []  # default when omitted


def test_malformed_skipped_and_a2a_protected():
    mod = _load_executor()
    cfg = {"mcp_servers": [
        {"name": "platform", "command": "node"},     # ok
        {"name": "no_command"},                       # skipped: no command
        {"command": "no_name"},                       # skipped: no name
        "not-a-dict",                                 # skipped: not a dict
        {"name": "a2a", "command": "evil"},           # must NOT override a2a
    ]}
    base = _base()
    out = mod._apply_extra_mcp_servers(base, cfg)
    assert out["a2a"]["args"] == ["/a2a"], "built-in a2a server must be protected"
    assert out["platform"] == {"command": "node", "args": []}
    assert "no_command" not in out
    assert "no_name" not in out


# ---- /configs/mcp_servers.yaml overlay fragment (core#2522) ----
#
# The provisioner ships the concierge's platform-MCP declaration as a
# standalone fragment because the SaaS restart-provision path cannot resolve
# a base config.yaml to append onto. The executor must read it with the same
# defensive posture as config.yaml: absent → {}, malformed → {}.


def _executor_with_config_path(mod, tmp_path):
    ex = object.__new__(mod.ClaudeSDKExecutor)
    ex.config_path = str(tmp_path)
    return ex


def test_load_mcp_fragment_reads_fragment(tmp_path):
    mod = _load_executor()
    (tmp_path / "mcp_servers.yaml").write_text(
        "mcp_servers:\n  - name: platform\n    command: molecule-mcp\n    env:\n      MOLECULE_MCP_MODE: management\n"
    )
    ex = _executor_with_config_path(mod, tmp_path)
    frag = ex._load_mcp_fragment()
    assert frag["mcp_servers"][0]["name"] == "platform"
    merged = mod._apply_extra_mcp_servers(_base(), frag)
    assert merged["platform"]["command"] == "molecule-mcp"
    assert merged["platform"]["env"] == {"MOLECULE_MCP_MODE": "management"}
    assert "a2a" in merged  # built-in never displaced


def test_load_mcp_fragment_absent_is_empty(tmp_path):
    mod = _load_executor()
    ex = _executor_with_config_path(mod, tmp_path)
    assert ex._load_mcp_fragment() == {}


def test_load_mcp_fragment_malformed_is_empty(tmp_path):
    mod = _load_executor()
    (tmp_path / "mcp_servers.yaml").write_text(": not [ yaml ::")
    ex = _executor_with_config_path(mod, tmp_path)
    assert ex._load_mcp_fragment() == {}


def test_fragment_wins_over_config_yaml_same_name():
    """Applied after config.yaml, a same-name fragment entry overrides it —
    the platform-authored declaration is authoritative."""
    mod = _load_executor()
    base = _base()
    mod._apply_extra_mcp_servers(base, {"mcp_servers": [{"name": "platform", "command": "stale-path"}]})
    mod._apply_extra_mcp_servers(base, {"mcp_servers": [{"name": "platform", "command": "molecule-mcp"}]})
    assert base["platform"]["command"] == "molecule-mcp"
