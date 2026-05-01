"""Pin --dangerously-load-development-channels into ClaudeAgentOptions.

The wheel-side push UX gates (capability + instructions, molecule-core
PR #2463) only fire if the host claude CLI loads non-allowlisted
channels. During the research preview the host requires the
``--dangerously-load-development-channels`` CLI flag to bypass its
allowlist; without it ``notifications/claude/channel`` arrives at the
host and is silently dropped. claude-agent-sdk forwards arbitrary
flags to the CLI subprocess via ``ClaudeAgentOptions.extra_args``, so
``_build_options`` must include this flag in every options object it
returns.

This test pins that the flag is wired by stubbing ``claude_agent_sdk``
to a recorder, then asserting the captured kwargs include the flag.
Regression-injection-checked: deleting the ``extra_args`` line from
``_build_options`` makes this test fail with a clear message naming the
missing flag.
"""

import os
import sys
import types
from unittest.mock import MagicMock


# ---- Stubs ----
#
# claude_sdk_executor.py imports a tall stack at module load:
#   - claude_agent_sdk (the SDK we're trying to inspect kwargs for)
#   - a2a.* (server.agent_execution, server.events, helpers)
#   - molecule_runtime.executor_helpers (a long re-export bundle)
#   - yaml
#
# yaml is real and available in CI. The rest get replaced with the
# minimum surface the executor module touches at import + ``__init__``
# + ``_build_options`` time. Any attribute access we miss surfaces as
# ``AttributeError`` immediately, not silent test pass.


def _ensure_module(dotted: str) -> types.ModuleType:
    """Return ``sys.modules[dotted]`` if real, else create + register a stub.

    Idempotent: re-running with the real package installed leaves it in
    place; we only ever ADD attributes (via ``_ensure_attr``), never
    overwrite anything the real module already exposed.
    """
    if dotted not in sys.modules:
        sys.modules[dotted] = types.ModuleType(dotted)
    return sys.modules[dotted]


def _ensure_attr(mod: types.ModuleType, name: str, value: object) -> None:
    """Install ``value`` as ``mod.name`` only if missing.

    Avoids clobbering a real package's symbols when the test runs in an
    environment where the real dep is installed (CI: stubs win;
    workstation: real package wins, we just fill in what's missing).
    """
    if not hasattr(mod, name):
        setattr(mod, name, value)


def _install_stubs():
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
    _ensure_attr(helpers, "auto_push_hook", lambda *a, **kw: None)
    _ensure_attr(helpers, "brief_summary", lambda *a, **kw: "")
    _ensure_attr(helpers, "collect_outbound_files", lambda *a, **kw: [])
    _ensure_attr(helpers, "commit_memory", lambda *a, **kw: None)
    _ensure_attr(helpers, "extract_attached_files", lambda *a, **kw: [])
    _ensure_attr(helpers, "extract_message_text", lambda *a, **kw: "")
    _ensure_attr(helpers, "get_a2a_instructions", lambda **kw: "")
    _ensure_attr(helpers, "get_hma_instructions", lambda *a, **kw: "")
    _ensure_attr(helpers, "get_mcp_server_path", lambda *a, **kw: "/dev/null")
    _ensure_attr(helpers, "get_system_prompt", lambda *a, **kw: "")
    _ensure_attr(helpers, "read_delegation_results", lambda *a, **kw: "")
    _ensure_attr(helpers, "recall_memories", lambda *a, **kw: "")
    _ensure_attr(helpers, "sanitize_agent_error", lambda e: str(e))
    _ensure_attr(helpers, "set_current_task", lambda *a, **kw: None)


def _load_executor():
    """Import claude_sdk_executor with stubs installed."""
    _install_stubs()
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    sys.modules.pop("claude_sdk_executor", None)
    import claude_sdk_executor  # noqa: WPS433
    return claude_sdk_executor


def test_build_options_forwards_dev_channels_flag(tmp_path):
    """``_build_options`` must include ``--dangerously-load-development-channels``
    in ``extra_args`` so the spawned claude CLI registers our experimental
    channel capability instead of silently dropping the notification.
    """
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    sdk.ClaudeAgentOptions.reset_mock()

    executor = mod.ClaudeSDKExecutor(
        system_prompt=None,
        config_path=str(tmp_path),
        heartbeat=None,
        model="sonnet",
    )
    executor._build_options()

    assert sdk.ClaudeAgentOptions.called, (
        "ClaudeAgentOptions was never called — _build_options likely raised "
        "before reaching the constructor"
    )
    kwargs = sdk.ClaudeAgentOptions.call_args.kwargs
    assert "extra_args" in kwargs, (
        "extra_args missing from ClaudeAgentOptions kwargs — the host "
        "claude CLI will never see --dangerously-load-development-channels "
        "and notifications/claude/channel will be filtered by the allowlist"
    )
    assert kwargs["extra_args"] == {
        "dangerously-load-development-channels": None,
    }, (
        "extra_args has wrong shape — claude-agent-sdk's "
        "subprocess_cli.py:340-346 reads {flag: None} as a bare CLI "
        "switch; got %r" % (kwargs["extra_args"],)
    )
