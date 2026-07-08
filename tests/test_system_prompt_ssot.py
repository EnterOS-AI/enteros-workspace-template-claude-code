"""System-prompt SSOT for the claude-code executor (task #76).

The drift these tests close: the executor used to build its per-turn system
prompt with ``get_system_prompt(config_path)``, which reads ONLY
``system-prompt.md`` and IGNORES ``config.yaml`` ``prompt_files`` — so the
concierge (the only workspace that declares prompt_files) booted identity-less.

The fix routes the per-turn build through the ONE canonical builder
(``molecule_runtime.prompt.build_system_prompt``), which honors ``prompt_files``
(with the legacy ``system-prompt.md`` fallback). Two invariants are pinned:

  1. The effective prompt = the prompt_files-honoring base build, NOT a
     system-prompt.md-only re-read that ignores prompt_files.
  2. Hot-reload is preserved: editing a prompt file changes the NEXT turn's
     prompt without a restart (the build re-reads from disk every turn).

The conftest stubs ``molecule_runtime.prompt.build_system_prompt`` with a
faithful stand-in (honors prompt_files, reads real files) so this runs in the
minimal CI env (pytest + pyyaml, no real runtime) exactly like the rest of the
suite. The prove-fail: revert the executor back to ``get_system_prompt`` and
``test_effective_prompt_honors_prompt_files`` fails because the stale
``system-prompt.md`` shadows the declared concierge identity.
"""

import os
import sys
import types
from unittest.mock import MagicMock


def _ensure_module(dotted: str) -> types.ModuleType:
    if dotted not in sys.modules:
        sys.modules[dotted] = types.ModuleType(dotted)
    return sys.modules[dotted]


def _ensure_attr(mod: types.ModuleType, name: str, value: object) -> None:
    if not hasattr(mod, name):
        setattr(mod, name, value)


def _install_stubs() -> None:
    """Stub the SDK + a2a + executor_helpers so claude_sdk_executor imports in
    the minimal CI env. Uses idempotent _ensure_attr (not an all-or-nothing
    `if module not in sys.modules` guard) so it FILLS gaps left by whatever
    other test installed a partial stub first. molecule_runtime (incl. .prompt)
    is stubbed by conftest._install_stubs at collection time."""
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

    async def _async_noop(*_a, **_kw):
        return None

    async def _recall(*_a, **_kw):
        return ""

    defaults = {
        "CONFIG_MOUNT": "/configs",
        "WORKSPACE_MOUNT": "/workspace",
        "MEMORY_CONTENT_MAX_CHARS": 10000,
        "auto_push_hook": _async_noop,
        "brief_summary": lambda *a, **kw: "",
        "collect_outbound_files": lambda *a, **kw: [],
        "commit_memory": _async_noop,
        "extract_attached_files": lambda *a, **kw: [],
        "extract_message_text": lambda *a, **kw: "",
        # claude-code-specific display instructions the executor appends; the
        # value is irrelevant to the prompt_files invariant under test.
        "get_display_instructions": lambda *a, **kw: "",
        "get_mcp_server_path": lambda *a, **kw: "/dev/null",
        "read_delegation_results": lambda *a, **kw: "",
        "recall_memories": _recall,
        "sanitize_agent_error": lambda exc=None, category=None, stderr=None: "err",
        "error_detail_for_external": lambda exc: str(exc) or None,
        "set_current_task": _async_noop,
    }
    for name, val in defaults.items():
        _ensure_attr(helpers, name, val)


def _load_executor():
    _install_stubs()
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    sys.modules.pop("claude_sdk_executor", None)
    import claude_sdk_executor  # noqa: WPS433

    return claude_sdk_executor


def _concierge_configs(tmp_path):
    """Concierge layout: identity at prompts/concierge.md (declared via
    prompt_files) + a STALE root system-prompt.md that must NOT shadow it."""
    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "concierge.md").write_text("ORG-CONCIERGE-IDENTITY")
    (tmp_path / "system-prompt.md").write_text("STALE-GENERIC-FALLBACK")
    return str(tmp_path)


def _make_executor(mod, config_path, prompt_files):
    return mod.ClaudeSDKExecutor(
        system_prompt="BOOT-PUBLISHED-FALLBACK",
        config_path=config_path,
        heartbeat=None,
        model="sonnet",
        prompt_files=prompt_files,
        workspace_id="ws-concierge",
    )


def test_effective_prompt_honors_prompt_files(tmp_path):
    """The executor's effective prompt loads the DECLARED prompt_files and does
    NOT fall back to the stale system-prompt.md (the concierge-identity drift)."""
    mod = _load_executor()
    config_path = _concierge_configs(tmp_path)
    ex = _make_executor(mod, config_path, prompt_files=["prompts/concierge.md"])

    prompt = ex._build_system_prompt()

    assert prompt is not None
    assert "ORG-CONCIERGE-IDENTITY" in prompt
    # prompt_files wins: the legacy single file is NOT loaded.
    assert "STALE-GENERIC-FALLBACK" not in prompt


def test_effective_prompt_hot_reloads_from_disk(tmp_path):
    """Editing a declared prompt file changes the NEXT turn's prompt without a
    restart — the load-bearing hot-reload, now routed through the SSOT builder."""
    mod = _load_executor()
    config_path = _concierge_configs(tmp_path)
    ex = _make_executor(mod, config_path, prompt_files=["prompts/concierge.md"])

    first = ex._build_system_prompt()
    assert "ORG-CONCIERGE-IDENTITY" in first

    # A freshly delivered/edited prompt takes effect on the next build.
    (tmp_path / "prompts" / "concierge.md").write_text("UPDATED-CONCIERGE-IDENTITY")
    second = ex._build_system_prompt()

    assert "UPDATED-CONCIERGE-IDENTITY" in second
    assert "ORG-CONCIERGE-IDENTITY" not in second


def test_legacy_system_prompt_md_still_loads_without_prompt_files(tmp_path):
    """Backwards-compat: a legacy workspace that ships only system-prompt.md and
    declares NO prompt_files still gets it (single builder, fallback honored)."""
    mod = _load_executor()
    (tmp_path / "system-prompt.md").write_text("LEGACY-SINGLE-FILE")
    ex = _make_executor(mod, str(tmp_path), prompt_files=[])

    prompt = ex._build_system_prompt()

    assert "LEGACY-SINGLE-FILE" in prompt


def test_hot_reload_preserves_plugin_fragments(tmp_path):
    """The executor's per-turn hot-reload rebuild threads plugin_rules and
    plugin_prompts through build_system_prompt just like setup() does, so a
    reloaded turn does NOT silently drop plugin fragments (#185)."""
    mod = _load_executor()
    config_path = _concierge_configs(tmp_path)
    ex = mod.ClaudeSDKExecutor(
        system_prompt="BOOT-PUBLISHED-FALLBACK",
        config_path=config_path,
        heartbeat=None,
        model="sonnet",
        prompt_files=["prompts/concierge.md"],
        workspace_id="ws-concierge",
        plugin_rules="RULE-FRAGMENT",
        plugin_prompts=["PROMPT-FRAGMENT-1", "PROMPT-FRAGMENT-2"],
    )

    prompt = ex._build_system_prompt()

    assert "ORG-CONCIERGE-IDENTITY" in prompt
    assert "RULE-FRAGMENT" in prompt
    assert "PROMPT-FRAGMENT-1" in prompt
    assert "PROMPT-FRAGMENT-2" in prompt
