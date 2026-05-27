"""Pin the internal#702 harness model-tier env passthrough contract.

Root cause (internal#702): for a `platform_managed` claude-code workspace on
a NON-Anthropic model (e.g. Kimi K2.6 -> Moonshot), the Claude Code harness's
background / "small-fast" tier (title-gen, summarization, quota probes)
falls back to a literal `claude-3-5-haiku` when `ANTHROPIC_SMALL_FAST_MODEL`
(and the alias tiers) are unset. That `claude-*` id inherits
`ANTHROPIC_BASE_URL=<cp proxy>` and the proxy routes any `claude*` slug to
real Anthropic -> the depleted platform key -> "credit balance too low".

CP now injects the correct same-provider values for these vars (see
molecule-controlplane tenant_config.go `platformManagedHarnessModelEnv`).
The ADAPTER's only job is to make sure they reach the spawned `claude`
process. This file pins TWO invariants of that passthrough:

  1. `_build_options()` constructs `ClaudeAgentOptions` with NO restrictive
     `env=` allow-list, so the claude-agent-sdk subprocess transport inherits
     the full container `os.environ` (which is where CP's injected vars
     live). A future refactor that adds an `env=` filter without including
     the harness model-tier names would silently re-open the leak — this
     test fails the instant that happens.

  2. `_audit_harness_model_env()` reports every harness model-tier var by
     NAME with set/unset status, and NEVER logs a VALUE (same contract as
     the auth-env audit).

Stub strategy mirrors test_dev_channels_flag.py — see that file's comments.
"""

import logging
import os
import sys
import types
from unittest.mock import MagicMock

import pytest


# ---- Stubs (mirror test_dev_channels_flag.py) -----------------------------
def _ensure_module(dotted: str) -> types.ModuleType:
    if dotted not in sys.modules:
        sys.modules[dotted] = types.ModuleType(dotted)
    return sys.modules[dotted]


def _ensure_attr(mod: types.ModuleType, name: str, value: object) -> None:
    if not hasattr(mod, name):
        setattr(mod, name, value)


def _install_executor_stubs():
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
    _ensure_attr(helpers, "get_display_instructions", lambda *a, **kw: "")
    _ensure_attr(helpers, "get_hma_instructions", lambda *a, **kw: "")
    _ensure_attr(helpers, "get_mcp_server_path", lambda *a, **kw: "/dev/null")
    _ensure_attr(helpers, "get_system_prompt", lambda *a, **kw: "")
    _ensure_attr(helpers, "read_delegation_results", lambda *a, **kw: "")
    _ensure_attr(helpers, "recall_memories", lambda *a, **kw: "")
    _ensure_attr(helpers, "sanitize_agent_error", lambda e: str(e))
    _ensure_attr(helpers, "set_current_task", lambda *a, **kw: None)


def _load_executor():
    _install_executor_stubs()
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    sys.modules.pop("claude_sdk_executor", None)
    import claude_sdk_executor  # noqa: WPS433
    return claude_sdk_executor


# adapter.py is import-stubbed for the whole suite by tests/conftest.py, so a
# plain `from adapter import ...` works here.
from adapter import _HARNESS_MODEL_ENV, _audit_harness_model_env  # noqa: E402


def test_build_options_has_no_restrictive_env_allowlist(tmp_path):
    """The SDK options must NOT carry an `env=` allow-list.

    The harness model-tier vars CP injects (ANTHROPIC_SMALL_FAST_MODEL +
    aliases + ENABLE_TOOL_SEARCH) live in the container os.environ. The
    claude-agent-sdk subprocess transport inherits os.environ ONLY because
    `_build_options` does not pass an `env` kwarg. If a refactor adds an
    `env=` filter that omits these names, the harness's background tier
    silently reverts to `claude-3-5-haiku` and re-opens the internal#702
    leak. This test is the fail-closed guard for that contract.
    """
    mod = _load_executor()
    sdk = sys.modules["claude_agent_sdk"]
    sdk.ClaudeAgentOptions.reset_mock()

    executor = mod.ClaudeSDKExecutor(
        system_prompt=None,
        config_path=str(tmp_path),
        heartbeat=None,
        model="kimi-k2.6",
    )
    executor._build_options()

    assert sdk.ClaudeAgentOptions.called, (
        "ClaudeAgentOptions was never called — _build_options likely raised "
        "before reaching the constructor"
    )
    kwargs = sdk.ClaudeAgentOptions.call_args.kwargs

    if "env" in kwargs and kwargs["env"] is not None:
        env_arg = kwargs["env"]
        # An env mapping is only safe if it FORWARDS every harness model-tier
        # var (anything less re-opens internal#702). A None/absent env means
        # full os.environ inheritance, which is the current (correct) shape.
        missing = [name for name in _HARNESS_MODEL_ENV if name not in env_arg]
        assert not missing, (
            "_build_options passed an env allow-list that drops harness "
            f"model-tier vars {missing}; the spawned claude process would "
            "then fall back to claude-3-5-haiku and leak to real Anthropic "
            "on non-anthropic platform_managed workspaces (internal#702). "
            "Either drop the env= filter (inherit os.environ) or include "
            "every name in adapter._HARNESS_MODEL_ENV."
        )


def test_audit_harness_model_env_reports_names_not_values(caplog):
    """`_audit_harness_model_env` logs NAME + set/unset, never the VALUE."""
    sentinel = "fake-model-id-MUST-NOT-LEAK-702"
    # Set one tier var to a sentinel; assert presence is reported but the
    # value never appears in the log record.
    os.environ["ANTHROPIC_SMALL_FAST_MODEL"] = sentinel
    # Make sure a representative "unset" name is genuinely unset.
    os.environ.pop("ENABLE_TOOL_SEARCH", None)
    try:
        with caplog.at_level(logging.INFO):
            _audit_harness_model_env()
    finally:
        os.environ.pop("ANTHROPIC_SMALL_FAST_MODEL", None)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "harness model-tier env audit:" in text
    assert "ANTHROPIC_SMALL_FAST_MODEL=set" in text
    assert "ENABLE_TOOL_SEARCH=unset" in text
    assert sentinel not in text, "audit leaked a model-id VALUE into the log"


@pytest.mark.parametrize("name", _HARNESS_MODEL_ENV)
def test_every_harness_model_tier_var_is_audited(name, caplog):
    """Every var CP injects for the harness tiers must appear in the audit."""
    with caplog.at_level(logging.INFO):
        _audit_harness_model_env()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert f"{name}=" in text, (
        f"{name} is not reported by _audit_harness_model_env — add it to "
        "adapter._HARNESS_MODEL_ENV so the boot audit covers it"
    )
