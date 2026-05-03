"""Tests for the per-vendor env routing helper (_project_vendor_auth).

Task #244 — third-party Anthropic-compat providers (MiniMax, GLM, Kimi,
DeepSeek) used to share ANTHROPIC_AUTH_TOKEN, so a user with multiple
vendor keys could only run one workspace at a time, AND a user who saved
only the canvas-shown vendor name (e.g. MINIMAX_API_KEY) hit a silent
401 on first call. The boot audit log even said ``MINIMAX_API_KEY=set``
which made root-causing this look like an SDK bug.

This file pins the projection contract:
  1. Vendor key set + AUTH_TOKEN unset -> projection happens
  2. AUTH_TOKEN already set -> never clobbered (operator override wins)
  3. First-party (oauth / anthropic-api) provider picked -> no
     projection (vendor names ignored even if set)
  4. The secret VALUE is never logged (mirrors the
     _audit_auth_env_presence guarantee from PR #32).
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
    """Load adapter.py with molecule_runtime + a2a stubbed.

    Same isolation strategy as test_adapter_logging.py — see that file's
    fixture comment for the rationale. We stub the heavy import deps so
    the module-level helpers can be exercised without installing the
    runtime wheel.
    """
    pkg = types.ModuleType("molecule_runtime")
    sub = types.ModuleType("molecule_runtime.adapters")
    base = types.ModuleType("molecule_runtime.adapters.base")
    base.BaseAdapter = type("BaseAdapter", (), {})
    base.AdapterConfig = type("AdapterConfig", (), {})
    base.RuntimeCapabilities = type("RuntimeCapabilities", (), {})
    monkeypatch.setitem(sys.modules, "molecule_runtime", pkg)
    monkeypatch.setitem(sys.modules, "molecule_runtime.adapters", sub)
    monkeypatch.setitem(sys.modules, "molecule_runtime.adapters.base", base)

    a2a = types.ModuleType("a2a")
    a2a_server = types.ModuleType("a2a.server")
    a2a_ax = types.ModuleType("a2a.server.agent_execution")
    a2a_ax.AgentExecutor = type("AgentExecutor", (), {})
    monkeypatch.setitem(sys.modules, "a2a", a2a)
    monkeypatch.setitem(sys.modules, "a2a.server", a2a_server)
    monkeypatch.setitem(sys.modules, "a2a.server.agent_execution", a2a_ax)

    template_dir = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(template_dir))

    sys.modules.pop("adapter", None)
    spec = importlib.util.spec_from_file_location("adapter", template_dir / "adapter.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Sentinel used across tests to verify the secret value never leaks
# into a log record. Distinctive enough that any substring match
# unambiguously means a regression.
_SENTINEL = "fake-vendor-secret-MUST-NOT-LEAK-244"


def _minimax_provider():
    """Return a minimax-shaped provider dict matching config.yaml's entry.

    Built inline (not loaded from YAML) so the test doesn't depend on
    config.yaml's exact contents — that keeps the test green if a
    reviewer reorders the YAML or renames the provider entry, while
    still pinning the routing contract on the helper itself.
    """
    return {
        "name": "minimax",
        "auth_mode": "third_party_anthropic_compat",
        "model_prefixes": ("minimax-",),
        "model_aliases": (),
        "base_url": "https://api.minimax.io/anthropic",
        "auth_env": ("MINIMAX_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"),
    }


def _oauth_provider():
    return {
        "name": "anthropic-oauth",
        "auth_mode": "oauth",
        "model_prefixes": (),
        "model_aliases": ("sonnet", "opus", "haiku"),
        "base_url": None,
        "auth_env": ("CLAUDE_CODE_OAUTH_TOKEN",),
    }


def _clear_all_auth_env(monkeypatch, adapter_module):
    """Strip every auth-relevant env var so the test starts from a clean slate."""
    for name in adapter_module._AUTH_ENV_AUDIT:
        monkeypatch.delenv(name, raising=False)


def test_vendor_key_projects_when_auth_token_unset(adapter_module, monkeypatch):
    """The headline #244 fix: MINIMAX_API_KEY set, AUTH_TOKEN unset -> projection."""
    _clear_all_auth_env(monkeypatch, adapter_module)
    monkeypatch.setenv("MINIMAX_API_KEY", _SENTINEL)

    adapter_module._project_vendor_auth(_minimax_provider())

    import os
    assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == _SENTINEL, (
        "MINIMAX_API_KEY value must be projected onto ANTHROPIC_AUTH_TOKEN "
        "so the claude-code-sdk finds the bearer token"
    )


def test_existing_auth_token_not_clobbered(adapter_module, monkeypatch):
    """Idempotency: an explicit ANTHROPIC_AUTH_TOKEN is the operator override and wins."""
    _clear_all_auth_env(monkeypatch, adapter_module)
    monkeypatch.setenv("MINIMAX_API_KEY", "vendor-value")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "operator-value")

    adapter_module._project_vendor_auth(_minimax_provider())

    import os
    assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == "operator-value", (
        "operator-set ANTHROPIC_AUTH_TOKEN must NEVER be overwritten by the "
        "vendor-key projection — that's the explicit-override escape hatch"
    )


def test_first_party_provider_skips_projection(adapter_module, monkeypatch):
    """OAuth/anthropic-api providers don't project even if a vendor key is set.

    A workspace running on Claude Code OAuth that *also* happens to have
    MINIMAX_API_KEY exported (e.g. a multi-vendor power user) must NOT
    have that vendor key bleed into ANTHROPIC_AUTH_TOKEN — the OAuth
    path uses a totally different token and projection would only cause
    confusion (and a confusing audit-log line).
    """
    _clear_all_auth_env(monkeypatch, adapter_module)
    monkeypatch.setenv("MINIMAX_API_KEY", _SENTINEL)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")

    adapter_module._project_vendor_auth(_oauth_provider())

    import os
    assert os.environ.get("ANTHROPIC_AUTH_TOKEN") is None, (
        "first-party provider (oauth) must not consume vendor-specific "
        "env keys — projection should be a no-op for non-third-party paths"
    )


def test_projection_logs_name_not_value(adapter_module, monkeypatch, caplog):
    """The secret value must NEVER appear in any log record.

    Mirrors the safety guarantee on _audit_auth_env_presence (pinned by
    test_adapter_logging.py::test_audit_lists_every_name_with_presence).
    Same threat model: docker logs + central log aggregator must not
    leak the bearer token.
    """
    _clear_all_auth_env(monkeypatch, adapter_module)
    monkeypatch.setenv("MINIMAX_API_KEY", _SENTINEL)

    with caplog.at_level(logging.INFO, logger="adapter"):
        adapter_module._project_vendor_auth(_minimax_provider())

    # The projection happened (precondition for the leak check to be meaningful).
    import os
    assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == _SENTINEL

    for record in caplog.records:
        msg = record.getMessage()
        assert _SENTINEL not in msg, (
            f"projection logged the secret VALUE: {msg!r} — must log the "
            "env NAME only (mirrors _audit_auth_env_presence contract)"
        )

    # Sanity: at least one log record mentioned the projection by NAME.
    assert any(
        "MINIMAX_API_KEY" in r.getMessage() and "ANTHROPIC_AUTH_TOKEN" in r.getMessage()
        for r in caplog.records
    ), "expected an INFO log line documenting the MINIMAX_API_KEY -> ANTHROPIC_AUTH_TOKEN projection"


def test_empty_vendor_key_treated_as_unset(adapter_module, monkeypatch):
    """Empty-string vendor env doesn't trigger projection.

    workspace-server's nil/empty handling can plausibly export
    MINIMAX_API_KEY="" instead of omitting it (matches the audit
    helper's empty-string handling — see test_adapter_logging.py).
    Projecting an empty string would silently corrupt
    ANTHROPIC_AUTH_TOKEN and turn a missing-key error into a 401 with
    no diagnostic trail.
    """
    _clear_all_auth_env(monkeypatch, adapter_module)
    monkeypatch.setenv("MINIMAX_API_KEY", "")

    adapter_module._project_vendor_auth(_minimax_provider())

    import os
    assert os.environ.get("ANTHROPIC_AUTH_TOKEN") is None, (
        "empty-string vendor env must not trigger projection — the right "
        "failure mode is the existing 'no auth env set' warning, not a "
        "silently-projected empty bearer token"
    )


def test_glm_kimi_deepseek_also_project(adapter_module, monkeypatch):
    """The other three vendor names project too — not just MiniMax.

    Parametrize-style coverage in one test so a future contributor adding
    a new vendor sees the pattern in one place. Each iteration uses an
    isolated provider dict + a freshly-cleared env.
    """
    cases = [
        ("zai", "GLM_API_KEY"),
        ("moonshot", "KIMI_API_KEY"),
        ("deepseek", "DEEPSEEK_API_KEY"),
    ]
    for provider_name, env_name in cases:
        _clear_all_auth_env(monkeypatch, adapter_module)
        sentinel = f"{env_name}-sentinel"
        monkeypatch.setenv(env_name, sentinel)
        provider = {
            "name": provider_name,
            "auth_mode": "third_party_anthropic_compat",
            "model_prefixes": (),
            "model_aliases": (),
            "base_url": "https://example.invalid/anthropic",
            "auth_env": (env_name, "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"),
        }

        adapter_module._project_vendor_auth(provider)

        import os
        assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == sentinel, (
            f"{env_name} must project onto ANTHROPIC_AUTH_TOKEN for "
            f"provider={provider_name}"
        )
