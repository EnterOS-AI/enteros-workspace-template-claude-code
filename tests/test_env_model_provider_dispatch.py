"""Tests for ``_resolve_model_and_provider_from_env`` — the env-vs-YAML
reconciliation that fixes the 2026-05-08 dev-tree wedge incident.

Symptom: 22/27 non-lead workspaces (minimax tier) wedged on
``Control request timeout: initialize`` because the runtime wheel's
``workspace/config.py`` interpreted ``MODEL_PROVIDER=minimax`` as the
*model id* instead of the provider slug. ``model="minimax"`` failed to
match the ``minimax-`` registry prefix, fell through to providers[0]
(anthropic-oauth), demanded ``CLAUDE_CODE_OAUTH_TOKEN`` (unset on
non-leads), and the claude CLI hung at SDK init.

The persona env files (``~/.molecule-ai/personas/<name>/env``) declare
the new convention:
  * ``MODEL`` — model id (e.g. ``MiniMax-M2.7-highspeed``)
  * ``MODEL_PROVIDER`` — provider slug (e.g. ``minimax``)

These tests cover the matrix of (env shape) × (YAML shape) so a future
contributor can't silently regress the wedge fix.
"""

import pytest

from adapter import (
    _BUILTIN_PROVIDERS,
    _resolve_model_and_provider_from_env,
)


# A registry that contains both anthropic-oauth (providers[0]) and
# minimax/zai (third-party slugs) — matches the shipped config.yaml.
_REGISTRY = _BUILTIN_PROVIDERS + (
    {
        "name": "minimax",
        "auth_mode": "third_party_anthropic_compat",
        "model_prefixes": ("minimax-",),
        "model_aliases": (),
        "base_url": "https://api.minimax.io/anthropic",
        "auth_env": ("MINIMAX_API_KEY",),
    },
    {
        "name": "zai",
        "auth_mode": "third_party_anthropic_compat",
        "model_prefixes": ("glm-",),
        "model_aliases": (),
        "base_url": "https://api.z.ai/api/anthropic",
        "auth_env": ("GLM_API_KEY",),
    },
)


def _clear_env(monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)


# ------------------------------------------------------------------
# Persona env convention: MODEL=<id>, MODEL_PROVIDER=<slug>
# ------------------------------------------------------------------

def test_persona_env_minimax_resolves_correctly(monkeypatch):
    """The 2026-05-08 wedge regression test: persona env shape must
    yield model=MiniMax-M2.7-highspeed (not "minimax") and explicit
    provider=minimax."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("MODEL", "MiniMax-M2.7-highspeed")
    monkeypatch.setenv("MODEL_PROVIDER", "minimax")
    model, provider = _resolve_model_and_provider_from_env(
        yaml_model="", yaml_provider="", providers=_REGISTRY,
    )
    assert model == "MiniMax-M2.7-highspeed"
    assert provider == "minimax"


def test_persona_env_lead_claude_code_resolves_correctly(monkeypatch):
    """Lead persona env (MODEL=opus, MODEL_PROVIDER=claude-code) —
    ``claude-code`` is the persona-friendly alias for the canonical
    ``anthropic-oauth`` registry name. Must resolve via the alias map
    so the lead boots through the OAuth subscription path even when
    MODEL is a non-Anthropic model id (e.g. an operator who picked
    MiniMax in canvas but whose persona env still pins claude-code)."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("MODEL", "opus")
    monkeypatch.setenv("MODEL_PROVIDER", "claude-code")
    model, provider = _resolve_model_and_provider_from_env(
        yaml_model="", yaml_provider="", providers=_REGISTRY,
    )
    assert model == "opus"
    # claude-code → anthropic-oauth via the alias map
    assert provider == "anthropic-oauth"


def test_persona_env_lead_with_minimax_model_routes_via_oauth(monkeypatch):
    """Lead workspace whose persona pins MODEL_PROVIDER=claude-code but
    whose YAML/canvas selection happens to be a MiniMax model still
    routes via OAuth — the persona's provider pin wins over the
    model-prefix matcher. Without the alias map, the fall-through
    mis-routed leads to MiniMax even when their CLAUDE_CODE_OAUTH_TOKEN
    was set."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("MODEL", "MiniMax-M2.7")
    monkeypatch.setenv("MODEL_PROVIDER", "claude-code")
    model, provider = _resolve_model_and_provider_from_env(
        yaml_model="", yaml_provider="", providers=_REGISTRY,
    )
    assert model == "MiniMax-M2.7"
    assert provider == "anthropic-oauth"


def test_anthropic_alias_resolves_to_anthropic_api(monkeypatch):
    """``MODEL_PROVIDER=anthropic`` alias → ``anthropic-api`` (direct
    Anthropic API key path)."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("MODEL", "claude-opus-4-7")
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    model, provider = _resolve_model_and_provider_from_env(
        yaml_model="", yaml_provider="", providers=_REGISTRY,
    )
    assert model == "claude-opus-4-7"
    assert provider == "anthropic-api"


def test_persona_env_glm_resolves_correctly(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MODEL", "GLM-4.6")
    monkeypatch.setenv("MODEL_PROVIDER", "zai")
    model, provider = _resolve_model_and_provider_from_env(
        yaml_model="", yaml_provider="", providers=_REGISTRY,
    )
    assert model == "GLM-4.6"
    assert provider == "zai"


def test_env_provider_slug_case_insensitive(monkeypatch):
    """Operator typos like ``MiniMax`` (mixed case) still resolve."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("MODEL", "MiniMax-M2.7-highspeed")
    monkeypatch.setenv("MODEL_PROVIDER", "MiniMax")  # mixed case
    _, provider = _resolve_model_and_provider_from_env(
        yaml_model="", yaml_provider="", providers=_REGISTRY,
    )
    assert provider == "MiniMax"  # caller compares case-insensitively


# ------------------------------------------------------------------
# Legacy convention: MODEL_PROVIDER=<model-id>, MODEL unset
# ------------------------------------------------------------------

def test_legacy_model_provider_as_model_id_still_works(monkeypatch):
    """Pre-2026-05-08 canvas Save+Restart shape: MODEL_PROVIDER carried
    the model id directly (e.g. ``MODEL_PROVIDER=MiniMax-M2.7``) and
    no MODEL env. Must keep working so existing canvas users don't
    break overnight."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("MODEL_PROVIDER", "MiniMax-M2.7-highspeed")
    model, provider = _resolve_model_and_provider_from_env(
        yaml_model="", yaml_provider="", providers=_REGISTRY,
    )
    # MiniMax-M2.7-highspeed is not a registered provider name, so
    # it's treated as a legacy model-id-in-MODEL_PROVIDER value.
    assert model == "MiniMax-M2.7-highspeed"
    assert provider is None


# ------------------------------------------------------------------
# Env wins over YAML
# ------------------------------------------------------------------

def test_env_model_wins_over_yaml_model(monkeypatch):
    """When both env MODEL and YAML model are set, env wins."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("MODEL", "GLM-4.6")
    model, _ = _resolve_model_and_provider_from_env(
        yaml_model="MiniMax-M2.7", yaml_provider="", providers=_REGISTRY,
    )
    assert model == "GLM-4.6"


def test_env_provider_wins_over_yaml_provider(monkeypatch):
    """Env MODEL_PROVIDER (when a registered slug) wins over YAML provider."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("MODEL", "GLM-4.6")
    monkeypatch.setenv("MODEL_PROVIDER", "zai")
    _, provider = _resolve_model_and_provider_from_env(
        yaml_model="", yaml_provider="minimax", providers=_REGISTRY,
    )
    assert provider == "zai"


# ------------------------------------------------------------------
# YAML fallback (no env)
# ------------------------------------------------------------------

def test_no_env_falls_back_to_yaml(monkeypatch):
    """Workspace whose env doesn't set MODEL/MODEL_PROVIDER falls back
    to the YAML config — preserves existing operator workflows."""
    _clear_env(monkeypatch)
    model, provider = _resolve_model_and_provider_from_env(
        yaml_model="claude-sonnet-4-6",
        yaml_provider="anthropic-api",
        providers=_REGISTRY,
    )
    assert model == "claude-sonnet-4-6"
    assert provider == "anthropic-api"


def test_no_env_no_yaml_returns_empty(monkeypatch):
    """Pure default path — caller (setup) substitutes ``sonnet``."""
    _clear_env(monkeypatch)
    model, provider = _resolve_model_and_provider_from_env(
        yaml_model="", yaml_provider="", providers=_REGISTRY,
    )
    assert model == ""
    assert provider is None


def test_yaml_provider_anthropic_is_aliased_to_anthropic_api(monkeypatch):
    """Regression for 2026-05-09 staging-cplead-2 incident: every
    workspace booted ``configuration_status=not_configured`` because the
    molecule-runtime wheel auto-derives ``runtime_config.provider =
    "anthropic"`` from the default model slug ``anthropic:claude-opus-4-7``.
    The adapter received ``yaml_provider="anthropic"`` from the wheel and
    rejected it with ``ValueError: provider='anthropic' but it is not in
    the providers registry`` — but ``anthropic`` is already in
    ``_PROVIDER_SLUG_ALIASES`` for the env-var path. Mirror the alias map
    on the YAML path so the wheel default produces a registered provider
    name."""
    _clear_env(monkeypatch)
    _, provider = _resolve_model_and_provider_from_env(
        yaml_model="", yaml_provider="anthropic", providers=_REGISTRY,
    )
    assert provider == "anthropic-api", (
        f"yaml_provider='anthropic' must resolve through the alias map to "
        f"'anthropic-api'; got {provider!r}. Without this aliasing the "
        f"wheel-default workspace boot wedges at adapter.setup()."
    )


def test_yaml_provider_claude_code_is_aliased_to_anthropic_oauth(monkeypatch):
    """Symmetric coverage: persona-friendly ``claude-code`` slug from the
    YAML ``provider:`` field must alias to ``anthropic-oauth``, the same
    way the env-var path resolves it. Lead workspaces that pin the OAuth
    path in YAML (instead of via env) must not wedge."""
    _clear_env(monkeypatch)
    _, provider = _resolve_model_and_provider_from_env(
        yaml_model="", yaml_provider="claude-code", providers=_REGISTRY,
    )
    assert provider == "anthropic-oauth"


def test_yaml_provider_unknown_passes_through_for_actionable_error(monkeypatch):
    """An unaliased, unknown YAML provider (e.g. ``yaml_provider="mystery"``)
    must NOT be silently swapped to providers[0] — it must reach
    ``_resolve_provider`` so the adapter raises the actionable
    ``Known providers: ...`` error message. The alias map is a
    convenience for the two persona-convention slugs only; everything
    else must keep its original semantics."""
    _clear_env(monkeypatch)
    _, provider = _resolve_model_and_provider_from_env(
        yaml_model="", yaml_provider="mystery", providers=_REGISTRY,
    )
    assert provider == "mystery"


# ------------------------------------------------------------------
# Whitespace / empty-value defensive cases
# ------------------------------------------------------------------

def test_whitespace_only_env_treated_as_unset(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MODEL", "   ")
    monkeypatch.setenv("MODEL_PROVIDER", "  ")
    model, provider = _resolve_model_and_provider_from_env(
        yaml_model="opus", yaml_provider="", providers=_REGISTRY,
    )
    assert model == "opus"
    assert provider is None


def test_empty_env_value_treated_as_unset(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MODEL", "")
    monkeypatch.setenv("MODEL_PROVIDER", "")
    model, provider = _resolve_model_and_provider_from_env(
        yaml_model="sonnet", yaml_provider="", providers=_REGISTRY,
    )
    assert model == "sonnet"
    assert provider is None
