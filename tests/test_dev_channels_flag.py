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


def _channels_entry(extra_args):
    """Return (key, value) for the dev-channels flag, tolerating both shapes.

    - separate-value shape: {"dangerously-load-development-channels": "server:X"}
    - packed `=` shape (task #214 fix): {"dangerously-load-development-channels=server:X": None}
    """
    for k, v in extra_args.items():
        if k.split("=", 1)[0] == "dangerously-load-development-channels":
            return k, v
    return None, None


def test_build_options_forwards_tagged_dev_channels_flag(tmp_path):
    """``_build_options`` must pass the tagged ``server:molecule`` entry to
    ``--dangerously-load-development-channels``. The Claude Code 2.1.x CLI
    rejects bare server names ('molecule') and bare-switch values (None)
    with `entries must be tagged` / `argument missing` — the latter
    surfaces upstream as `Control request timeout: initialize` (caught
    live on workspace dd40faf8 on 2026-05-01, every A2A turn wedged).
    Live-verified that the tagged form unblocks both A2A AND host-side
    push UX (the host renders inbound messages as ``<channel>`` tags
    inline instead of dropping at the allowlist).
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
        "extra_args missing — host claude CLI will never see the dev-channels "
        "flag and notifications/claude/channel will be filtered at the allowlist"
    )
    key, value = _channels_entry(kwargs["extra_args"])
    # Resolve the tagged payload from whichever shape the executor used.
    tagged = value if value is not None else (key.split("=", 1)[1] if "=" in key else None)
    assert tagged == "server:molecule", (
        f"dev-channels entry must resolve to tagged 'server:molecule' to match "
        f"the workspace's MCP-server registration. The CLI rejects bare server "
        f"names with `entries must be tagged` and bare-switch values (None) "
        f"with `argument missing`; the latter wedges SDK initialize. "
        f"got key={key!r} value={value!r}"
    )


def test_build_options_dev_channels_value_is_not_bare_none(tmp_path):
    """Defense in depth against the original PR #25 bare-switch shape.

    A bare ``--dangerously-load-development-channels`` (no value, no
    ``=value`` packed into the key) renders as an argument-less flag,
    which the post-2.1.x CLI rejects with `argument missing`. Pin the
    invariant (the rendered payload is non-empty and tag-colon-shaped)
    so a regression to the old shape fails immediately at unit-test
    time instead of surfacing as a live `Control request timeout:
    initialize` wedge in production.
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

    key, value = _channels_entry(
        sdk.ClaudeAgentOptions.call_args.kwargs["extra_args"]
    )
    payload = key if value is None else f"{key}={value}"
    assert ":" in payload.split("=", 1)[-1], (
        f"flag payload must be tagged (server:<name> or plugin:<name>@<marketplace>); "
        f"got key={key!r} value={value!r} which the CLI rejects with "
        f"`entries must be tagged` or `argument missing`"
    )


def test_dev_channels_does_not_swallow_print_prompt_cli_2_1_143(tmp_path):
    """Task #214 regression — claude-code CLI 2.1.143.

    CLI 2.1.143 made ``--dangerously-load-development-channels`` variadic
    (``nargs='+'``).  claude-agent-sdk's renderer (subprocess_cli.py:340)
    emits ``{flag: value}`` as TWO argv elements, so the channels parser
    greedily absorbs the following ``--print <prompt>`` argv pair as
    channel entries and the SDK wedges at initialize.  Fix: pack ``=``
    into the key so the renderer's ``None``-value path emits ONE argv —
    ``--dangerously-load-development-channels=server:molecule`` — that
    the variadic parser cannot reach across.  Both argv orderings
    around ``--print <prompt>`` (channels-then-print, print-then-
    channels) must keep the prompt argv adjacent to ``--print``.
    """
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    sdk.ClaudeAgentOptions.reset_mock()
    executor = mod.ClaudeSDKExecutor(
        system_prompt=None, config_path=str(tmp_path), heartbeat=None, model="sonnet",
    )
    executor._build_options()
    extra_args = sdk.ClaudeAgentOptions.call_args.kwargs["extra_args"]

    # Mirror claude_agent_sdk/_internal/transport/subprocess_cli.py:340.
    channels_argv = []
    for flag, val in extra_args.items():
        channels_argv.append(f"--{flag}") if val is None else channels_argv.extend([f"--{flag}", str(val)])

    slots = [a for a in channels_argv if a.startswith("--dangerously-load-development-channels")]
    assert len(slots) == 1 and "=" in slots[0] and channels_argv == slots, (
        f"channels flag must render as a single argv with `=value` packed in so "
        f"CLI 2.1.143's nargs='+' parser cannot swallow --print <prompt>; "
        f"got channels_argv={channels_argv!r}"
    )
    for orientation, full_argv in (
        ("channels_then_print", channels_argv + ["--print", "hello world"]),
        ("print_then_channels", ["--print", "hello world"] + channels_argv),
    ):
        idx = full_argv.index("--print")
        assert full_argv[idx + 1] == "hello world", (
            f"--print prompt argv must stay adjacent ({orientation}); got {full_argv!r}"
        )
