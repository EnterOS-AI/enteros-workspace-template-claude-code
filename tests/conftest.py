"""Shared pytest fixtures + import shims for the adapter test suite.

`adapter.py` imports at module load:
  - molecule_runtime.adapters.base (BaseAdapter, AdapterConfig, RuntimeCapabilities)
  - molecule_runtime.plugins (lazy in setup(), but stubbed proactively)
  - a2a.server.agent_execution (AgentExecutor)
  - claude_sdk_executor (lazy in create_executor(), stubbed proactively)

In production those arrive transitively via molecule-ai-workspace-runtime.
The CI runner only installs `pytest pytest-asyncio pyyaml`, so the import
chain would fail with ModuleNotFoundError before any test collects —
exactly the failure that broke CI on the #180 fix branch (PR #4) and
caused the merge wall to block on a green local but red Gitea CI.

Putting the stub installer here (collected before any test module is
imported, per pytest semantics) means every test file can do
`from adapter import ...` at module top without a per-file boilerplate
copy. It also forces a single shape for the stubs so two files can't
silently disagree on whether `BaseAdapter` has
`install_plugins_via_registry` (see test_adapter_prevalidate's
async-setup tests, which need the method to exist on the parent class).
"""

import os
import sys
import types
from dataclasses import dataclass, field
from unittest.mock import MagicMock


@dataclass
class _StubRuntimeCapabilities:
    provides_native_session: bool = False


@dataclass
class _StubAdapterConfig:
    runtime_config: object = None
    config_path: str = "/tmp/configs"
    system_prompt: str = ""
    heartbeat: object = None
    prompt_files: list = field(default_factory=list)
    workspace_id: str = ""


class _StubBaseAdapter:
    async def install_plugins_via_registry(self, *_args, **_kwargs):
        pass


def _stub_build_system_prompt(
    config_path,
    workspace_id="",
    loaded_skills=None,
    peers=None,
    *,
    prompt_files=None,
    plugin_rules=None,
    plugin_prompts=None,
    **_kwargs,
):
    """Faithful-enough stand-in for molecule_runtime.prompt.build_system_prompt.

    Honors prompt_files (the SSOT behavior under test): loads the declared
    files in order, else falls back to system-prompt.md. Always prefixes the
    base platform frame so prompt-presence assertions match production shape.
    """
    parts = ["# You are a workspace on the Molecule AI platform"]
    files = list(prompt_files or []) or ["system-prompt.md"]
    for fname in files:
        fpath = os.path.join(config_path, fname)
        if os.path.exists(fpath):
            with open(fpath) as fh:
                content = fh.read().strip()
            if content:
                parts.append(content)
    # Include plugin fragments so tests can assert the hot-reload path threads
    # plugin_rules/plugin_prompts through the same builder as setup() (#185).
    if plugin_rules:
        parts.append(f"[plugin_rules]\n{plugin_rules}")
    for pp in plugin_prompts or []:
        if pp:
            parts.append(f"[plugin_prompt]\n{pp}")
    return "\n\n".join(parts)


def _install_stubs() -> None:
    """Install the smallest set of import shims that adapter.py needs."""
    if "molecule_runtime" not in sys.modules:
        mr = types.ModuleType("molecule_runtime")
        mr.adapters = types.ModuleType("molecule_runtime.adapters")
        mr.adapters.base = types.ModuleType("molecule_runtime.adapters.base")
        mr.adapters.base.BaseAdapter = _StubBaseAdapter
        mr.adapters.base.AdapterConfig = _StubAdapterConfig
        mr.adapters.base.RuntimeCapabilities = _StubRuntimeCapabilities
        mr.plugins = types.ModuleType("molecule_runtime.plugins")
        mr.plugins.load_plugins = lambda **_kwargs: []
        # adapter.setup() + the executor lazy-import
        # molecule_runtime.prompt.build_system_prompt to publish/derive the
        # SSOT prompt. Stub it to honor prompt_files so the SSOT behavior is
        # exercised without the real runtime installed.
        mr.prompt = types.ModuleType("molecule_runtime.prompt")
        mr.prompt.build_system_prompt = _stub_build_system_prompt
        sys.modules["molecule_runtime"] = mr
        sys.modules["molecule_runtime.adapters"] = mr.adapters
        sys.modules["molecule_runtime.adapters.base"] = mr.adapters.base
        sys.modules["molecule_runtime.plugins"] = mr.plugins
        sys.modules["molecule_runtime.prompt"] = mr.prompt
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


# Run at conftest import time — pytest collects conftest.py before any
# test module, so the stubs are in sys.modules before `from adapter
# import ...` ever executes.
_install_stubs()

# adapter.py lives in the parent dir of tests/ (template root). pytest's
# `--import-mode=importlib` + tests/pytest.ini anchoring rootdir at
# tests/ means the parent isn't on sys.path automatically. Add it here
# once so every test file can do `from adapter import ...` cleanly.
_PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)
