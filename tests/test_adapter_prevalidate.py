"""Unit tests for ClaudeCodeAdapter.create_executor pre-validation.

Pin the failure-mode-caught-on-2026-04-30 (workspaces with
ANTHROPIC_BASE_URL pointing at a MiniMax/OpenAI shim and no explicit
model hung on the SDK --print probe for 30s, eventually triggering
the platform's phantom-busy sweep).

These tests exercise the pre-validation branch in create_executor
without booting the actual ClaudeSDKExecutor — we mock the import
so we can drive the validation logic in isolation.
"""

import os
import sys
import types
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest


# ---- Test scaffolding ----
#
# adapter.py imports at module load:
#   - molecule_runtime.adapters.base (BaseAdapter, AdapterConfig, RuntimeCapabilities)
#   - a2a.server.agent_execution (AgentExecutor)
# create_executor lazily imports claude_sdk_executor.ClaudeSDKExecutor.
# We stub all four so the test file can run in CI without those packages
# installed. The pre-validation branch we care about runs BEFORE the
# executor instantiates, so the stub doesn't affect what we're testing.


@dataclass
class _StubRuntimeCapabilities:
    provides_native_session: bool = False


@dataclass
class _StubAdapterConfig:
    runtime_config: object = None
    config_path: str = "/tmp/configs"
    system_prompt: str = ""
    heartbeat: object = None


class _StubBaseAdapter:
    async def install_plugins_via_registry(self, *_args, **_kwargs):
        pass


def _install_stubs():
    """Install the smallest set of import shims that adapter.py needs."""
    if "molecule_runtime" not in sys.modules:
        mr = types.ModuleType("molecule_runtime")
        mr.adapters = types.ModuleType("molecule_runtime.adapters")
        mr.adapters.base = types.ModuleType("molecule_runtime.adapters.base")
        mr.adapters.base.BaseAdapter = _StubBaseAdapter
        mr.adapters.base.AdapterConfig = _StubAdapterConfig
        mr.adapters.base.RuntimeCapabilities = _StubRuntimeCapabilities
        sys.modules["molecule_runtime"] = mr
        sys.modules["molecule_runtime.adapters"] = mr.adapters
        sys.modules["molecule_runtime.adapters.base"] = mr.adapters.base
    if "a2a" not in sys.modules:
        a2a = types.ModuleType("a2a")
        a2a.server = types.ModuleType("a2a.server")
        a2a.server.agent_execution = types.ModuleType("a2a.server.agent_execution")
        a2a.server.agent_execution.AgentExecutor = type("AgentExecutor", (), {})
        sys.modules["a2a"] = a2a
        sys.modules["a2a.server"] = a2a.server
        sys.modules["a2a.server.agent_execution"] = a2a.server.agent_execution
    if "claude_sdk_executor" not in sys.modules:
        mod = types.ModuleType("claude_sdk_executor")
        mod.ClaudeSDKExecutor = MagicMock(name="ClaudeSDKExecutor")
        sys.modules["claude_sdk_executor"] = mod


@pytest.fixture
def adapter(monkeypatch):
    """Fresh ClaudeCodeAdapter with all imports stubbed."""
    _install_stubs()
    # adapter.py lives in the parent dir. tests/ has no __init__.py
    # because the template directory itself is a Python package
    # (production runtime imports it via the platform's adapter loader),
    # and adding tests/__init__.py would re-expose the same relative-
    # import collection problem we sidestepped by isolating tests here.
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    # Strip any cached module so the stubbed sys.modules entries take effect.
    sys.modules.pop("adapter", None)
    import adapter as adapter_module  # noqa: WPS433
    return adapter_module.ClaudeCodeAdapter()


# ---- Pre-validation tests ----


@pytest.mark.asyncio
async def test_create_executor_raises_when_custom_base_url_and_no_model(
    adapter, monkeypatch
):
    """The 2026-04-30 incident shape: custom upstream + no explicit model.

    Adapter must raise ValueError with an actionable message instead of
    silently passing 'sonnet' to ClaudeSDKExecutor (which would hang
    for 30s on the SDK probe before timing out).
    """
    monkeypatch.setenv(
        "ANTHROPIC_BASE_URL", "https://api.xiaomimimo.com/anthropic"
    )
    cfg = _StubAdapterConfig(runtime_config={"model": ""})

    with pytest.raises(ValueError) as exc_info:
        await adapter.create_executor(cfg)

    msg = str(exc_info.value)
    assert "ANTHROPIC_BASE_URL" in msg
    assert "api.xiaomimimo.com" in msg
    assert "MODEL_PROVIDER" in msg or "runtime_config.model" in msg


@pytest.mark.asyncio
async def test_create_executor_passes_when_anthropic_native_and_no_model(
    adapter, monkeypatch
):
    """Anthropic-native users with no model picked still get the 'sonnet'
    fallback — that's correct behavior, never an error. The pre-validation
    only fires on non-Anthropic hosts.
    """
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    cfg = _StubAdapterConfig(runtime_config={"model": ""})

    # Should not raise — fallback to "sonnet" is the documented default.
    executor = await adapter.create_executor(cfg)
    assert executor is not None


@pytest.mark.asyncio
async def test_create_executor_passes_when_no_base_url_set(adapter, monkeypatch):
    """No ANTHROPIC_BASE_URL = SDK uses its built-in Anthropic default.
    That's the historical happy path. Pre-validation must not regress it.
    """
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    cfg = _StubAdapterConfig(runtime_config={"model": ""})

    executor = await adapter.create_executor(cfg)
    assert executor is not None


@pytest.mark.asyncio
async def test_create_executor_passes_when_custom_base_url_with_explicit_model(
    adapter, monkeypatch
):
    """The fix the user is supposed to apply: set both URL and model.
    Pre-validation must let this through cleanly. End-to-end success path
    for the MiniMax-shim use case after Option B PRs land.
    """
    monkeypatch.setenv(
        "ANTHROPIC_BASE_URL", "https://api.xiaomimimo.com/anthropic"
    )
    cfg = _StubAdapterConfig(
        runtime_config={"model": "MiniMax-M2"}
    )

    executor = await adapter.create_executor(cfg)
    assert executor is not None


@pytest.mark.asyncio
async def test_create_executor_passes_dataclass_runtime_config(adapter, monkeypatch):
    """runtime_config can arrive as a dataclass (the production shape via
    main.py's load_config) instead of a dict. The defensive read at line
    118-122 must work for both. Regression coverage for the read path.
    """
    monkeypatch.setenv(
        "ANTHROPIC_BASE_URL", "https://api.xiaomimimo.com/anthropic"
    )

    @dataclass
    class _RC:
        model: str = "MiniMax-M2"
        provider: str = "minimax"

    cfg = _StubAdapterConfig(runtime_config=_RC())
    executor = await adapter.create_executor(cfg)
    assert executor is not None


@pytest.mark.asyncio
async def test_create_executor_raises_when_dataclass_runtime_config_empty_model(
    adapter, monkeypatch
):
    """Dataclass shape with empty model triggers the same validation as
    dict shape with empty model. Symmetric behavior across both inputs.
    """
    monkeypatch.setenv(
        "ANTHROPIC_BASE_URL", "https://api.xiaomimimo.com/anthropic"
    )

    @dataclass
    class _RC:
        model: str = ""
        provider: str = ""

    cfg = _StubAdapterConfig(runtime_config=_RC())

    with pytest.raises(ValueError):
        await adapter.create_executor(cfg)


@pytest.mark.asyncio
async def test_create_executor_passes_when_unparseable_url(adapter, monkeypatch):
    """An unparseable URL value (no host extractable) shouldn't crash
    with AttributeError. Should still pass through to the SDK so the
    SDK gets to error on it itself — adapter doesn't take ownership
    of URL validation, just the missing-model invariant.
    """
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "://garbage")
    cfg = _StubAdapterConfig(runtime_config={"model": ""})

    # Empty hostname → pre-validation skips → reaches SDK with "sonnet"
    # fallback. The SDK will fail; that's not the adapter's job.
    executor = await adapter.create_executor(cfg)
    assert executor is not None
