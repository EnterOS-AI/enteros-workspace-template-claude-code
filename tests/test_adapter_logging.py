"""Tests for the adapter-side boot debug logging helpers.

The 2026-05-02 crash-loop diagnosis hinged on operators being able to see,
from `docker logs` alone, *which* auth env names were set vs unset at boot.
This test pins that contract — `_audit_auth_env_presence` must emit a
single INFO line listing every name in `_AUTH_ENV_AUDIT` with its presence
status, and must NEVER include the value.

Test isolation: adapter.py imports molecule_runtime + a2a at module load.
Neither is installed in this template's test env (the template ships its
own stripped-down test set so CI doesn't pull a heavy runtime wheel just
to lint the adapter helpers). We stub both with empty modules so the
audit helpers can import cleanly.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def adapter_module(monkeypatch):
    """Load the template's adapter module without its molecule_runtime + a2a deps.

    The full adapter requires a2a-sdk + molecule_runtime at import time,
    which aren't installed in the lean test env. We stub them with empty
    modules so the module-level helpers (_AUTH_ENV_AUDIT,
    _audit_auth_env_presence) can be imported in isolation.
    """
    # Stub molecule_runtime.adapters.base.BaseAdapter / AdapterConfig /
    # RuntimeCapabilities (all referenced at adapter.py module load).
    pkg = types.ModuleType("molecule_runtime")
    sub = types.ModuleType("molecule_runtime.adapters")
    base = types.ModuleType("molecule_runtime.adapters.base")
    base.BaseAdapter = type("BaseAdapter", (), {})
    base.AdapterConfig = type("AdapterConfig", (), {})
    base.RuntimeCapabilities = type("RuntimeCapabilities", (), {})
    monkeypatch.setitem(sys.modules, "molecule_runtime", pkg)
    monkeypatch.setitem(sys.modules, "molecule_runtime.adapters", sub)
    monkeypatch.setitem(sys.modules, "molecule_runtime.adapters.base", base)

    # Stub a2a.server.agent_execution.AgentExecutor
    a2a = types.ModuleType("a2a")
    a2a_server = types.ModuleType("a2a.server")
    a2a_ax = types.ModuleType("a2a.server.agent_execution")
    a2a_ax.AgentExecutor = type("AgentExecutor", (), {})
    monkeypatch.setitem(sys.modules, "a2a", a2a)
    monkeypatch.setitem(sys.modules, "a2a.server", a2a_server)
    monkeypatch.setitem(sys.modules, "a2a.server.agent_execution", a2a_ax)

    template_dir = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(template_dir))

    # Force-reload so the stubs take effect even if a sibling test
    # already imported the real (or partially-stubbed) module first.
    sys.modules.pop("adapter", None)
    spec = importlib.util.spec_from_file_location("adapter", template_dir / "adapter.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_audit_lists_every_name_with_presence(adapter_module, monkeypatch, caplog):
    """The audit log must enumerate every name in _AUTH_ENV_AUDIT, set or unset."""
    monkeypatch.setenv("MINIMAX_API_KEY", "fake-secret-MUST-NOT-LEAK")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with caplog.at_level(logging.INFO, logger="adapter"):
        adapter_module._audit_auth_env_presence()

    # Single log record, INFO level, prefix "auth env audit:"
    matching = [r for r in caplog.records if "auth env audit" in r.getMessage()]
    assert len(matching) == 1, f"expected exactly one audit record, got {len(matching)}"
    msg = matching[0].getMessage()

    # Every audited name appears with set/unset
    for name in adapter_module._AUTH_ENV_AUDIT:
        assert f"{name}=" in msg, f"audit message missing {name}: {msg!r}"

    # MINIMAX_API_KEY is set, others unset
    assert "MINIMAX_API_KEY=set" in msg
    assert "CLAUDE_CODE_OAUTH_TOKEN=unset" in msg
    assert "ANTHROPIC_API_KEY=unset" in msg

    # Critical security assertion: the SECRET VALUE itself must NOT appear.
    # If this regresses, the audit is leaking secrets to operator-visible
    # docker logs and (worse) to the platform's central log aggregator.
    assert "fake-secret-MUST-NOT-LEAK" not in msg, (
        "audit log leaked the env VALUE — must be names + set/unset only"
    )


def test_audit_with_all_unset(adapter_module, monkeypatch, caplog):
    """All names report 'unset' when no auth env is configured (the crash-loop scenario)."""
    for name in adapter_module._AUTH_ENV_AUDIT:
        monkeypatch.delenv(name, raising=False)

    with caplog.at_level(logging.INFO, logger="adapter"):
        adapter_module._audit_auth_env_presence()

    matching = [r for r in caplog.records if "auth env audit" in r.getMessage()]
    assert len(matching) == 1
    msg = matching[0].getMessage()
    for name in adapter_module._AUTH_ENV_AUDIT:
        assert f"{name}=unset" in msg


def test_audit_treats_empty_string_as_unset(adapter_module, monkeypatch, caplog):
    """Empty-string env values report as 'unset' — matches routing semantics.

    workspace-server's nil/empty handling could plausibly export
    MINIMAX_API_KEY="" instead of omitting it; the audit must report
    that as unset (it is, semantically) so the operator's "is the key
    present?" question gets the same answer as the routing layer's.
    """
    monkeypatch.setenv("MINIMAX_API_KEY", "")
    for name in adapter_module._AUTH_ENV_AUDIT:
        if name != "MINIMAX_API_KEY":
            monkeypatch.delenv(name, raising=False)

    with caplog.at_level(logging.INFO, logger="adapter"):
        adapter_module._audit_auth_env_presence()

    msg = [r.getMessage() for r in caplog.records if "auth env audit" in r.getMessage()][0]
    assert "MINIMAX_API_KEY=unset" in msg


def test_audit_env_list_matches_entrypoint_sh(adapter_module):
    """_AUTH_ENV_AUDIT in adapter.py must mirror the for-loop in entrypoint.sh.

    The entrypoint emits the same set of NAME=set/unset lines BEFORE the
    Python adapter ever runs (including the pre-gosu and post-gosu boot
    contexts), so an operator can correlate a missing key across the
    privilege drop. If the two lists drift, an env name added in one
    place but not the other becomes invisible at one tier — exactly the
    crash-loop diagnosis gap we just closed.

    Pin the union by parsing the shell loop and asserting set-equality.
    """
    template_dir = Path(__file__).resolve().parent.parent
    entrypoint = (template_dir / "entrypoint.sh").read_text()
    # The for-loop has the form: `for var in NAME1 NAME2 ... NAMEN; do`
    # Extract NAME1..NAMEN by finding the `for var in ... ; do` line that
    # references CLAUDE_CODE_OAUTH_TOKEN (so we don't grab unrelated loops).
    loop_line = next(
        (line for line in entrypoint.splitlines()
         if "for var in" in line and "CLAUDE_CODE_OAUTH_TOKEN" in line),
        None,
    )
    assert loop_line, "entrypoint.sh missing the auth-env audit for-loop"
    # `    for var in A B C; do` → ['A', 'B', 'C']
    names_in_shell = (
        loop_line.split("for var in", 1)[1]
        .split(";", 1)[0]
        .split()
    )
    assert set(names_in_shell) == set(adapter_module._AUTH_ENV_AUDIT), (
        f"adapter.py _AUTH_ENV_AUDIT ({set(adapter_module._AUTH_ENV_AUDIT)}) "
        f"and entrypoint.sh for-loop ({set(names_in_shell)}) disagree on the "
        "audit set — keep them in sync (see the comment in adapter.py)."
    )
