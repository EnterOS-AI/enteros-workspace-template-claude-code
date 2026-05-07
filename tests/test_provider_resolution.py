"""Tests for the provider-resolution path that was silent-failing on #180.

Regression coverage: when an operator picks a provider in the canvas Config
tab that isn't in the registry, the adapter must raise ValueError with an
actionable message — NOT silently fall through to providers[0]
(anthropic-oauth) and then have the Claude SDK hit the user's OAuth quota
under a different name.

These tests mirror the production failure mode reported by Hongming
2026-05-07 17:35: workspace config.yaml had `provider: minimax` set, the
adapter ignored it entirely, the SDK kept calling the Anthropic API with
CLAUDE_CODE_OAUTH_TOKEN, hit the OAuth quota, and the canvas surfaced
"Agent error (Exception)" with no clue why.
"""

import pytest

from adapter import (
    _BUILTIN_PROVIDERS,
    _resolve_provider,
)


def test_resolve_with_no_explicit_provider_falls_back_to_model_match():
    """No explicit provider → model-based prefix/alias matching, default to providers[0]."""
    p = _resolve_provider("claude-opus-4-7", _BUILTIN_PROVIDERS)
    assert p["name"] == "anthropic-api"  # matches model_prefixes=("claude-",)


def test_resolve_with_no_explicit_provider_falls_back_to_default():
    """Unknown model + no explicit provider → providers[0] (anthropic-oauth)."""
    p = _resolve_provider("unknown-model", _BUILTIN_PROVIDERS)
    assert p["name"] == "anthropic-oauth"


def test_resolve_with_explicit_provider_in_registry_returns_match():
    """Explicit name lookup wins over model-based resolution."""
    # Even though "claude-opus-4-7" would normally resolve to anthropic-api
    # via prefix matching, the explicit provider name wins.
    p = _resolve_provider(
        "claude-opus-4-7", _BUILTIN_PROVIDERS,
        explicit_provider="anthropic-oauth",
    )
    assert p["name"] == "anthropic-oauth"


def test_resolve_with_explicit_provider_case_insensitive():
    """Provider name match is case-insensitive (operators write 'Anthropic-OAuth' etc)."""
    p = _resolve_provider(
        "sonnet", _BUILTIN_PROVIDERS,
        explicit_provider="ANTHROPIC-OAUTH",
    )
    assert p["name"] == "anthropic-oauth"


def test_resolve_with_explicit_provider_not_in_registry_raises():
    """The #180 regression test: explicit non-registry provider must raise, not fall through."""
    with pytest.raises(ValueError) as exc_info:
        _resolve_provider(
            "MiniMax-M2.7-highspeed", _BUILTIN_PROVIDERS,
            explicit_provider="minimax",
        )
    msg = str(exc_info.value)
    # Must name the bad provider so operator knows what they typed
    assert "minimax" in msg
    # Must list known providers so operator knows what's available
    assert "anthropic-oauth" in msg
    assert "anthropic-api" in msg
    # Must give actionable next steps — NOT just "not found"
    assert "providers:" in msg or "Add" in msg
    assert "Switch" in msg or "runtime" in msg


def test_resolve_with_explicit_provider_does_not_silent_fallback():
    """Specifically: must not return providers[0] when explicit_provider is bogus.

    This is the exact silent-fallback path that caused the user-visible
    bug: operator picks 'minimax' → adapter returns anthropic-oauth →
    SDK uses CLAUDE_CODE_OAUTH_TOKEN → hits quota.
    """
    with pytest.raises(ValueError):
        result = _resolve_provider(
            "anything", _BUILTIN_PROVIDERS,
            explicit_provider="minimax",
        )
        # If the implementation regresses to silent fallback, this would
        # have returned providers[0] (anthropic-oauth) instead of raising.
        # Defense-in-depth: guard against accidental "return" inside the
        # error path.
        assert result["name"] not in {"anthropic-oauth", "anthropic-api"}, (
            "REGRESSION: silent fallback to default provider when explicit "
            "provider name is not in registry — this is the #180 bug."
        )


def test_resolve_with_explicit_provider_in_custom_registry():
    """When operator adds a third-party provider to the registry, explicit lookup finds it."""
    custom_registry = _BUILTIN_PROVIDERS + (
        {
            "name": "minimax",
            "auth_mode": "third_party_anthropic_compat",
            "model_prefixes": ("minimax-",),
            "model_aliases": (),
            "base_url": "https://api.minimaxi.com/anthropic-compat",
            "auth_env": ("MINIMAX_API_KEY",),
        },
    )
    p = _resolve_provider(
        "MiniMax-M2.7-highspeed", custom_registry,
        explicit_provider="minimax",
    )
    assert p["name"] == "minimax"
    assert p["base_url"] == "https://api.minimaxi.com/anthropic-compat"
    assert "MINIMAX_API_KEY" in p["auth_env"]


def test_resolve_empty_providers_raises():
    """Pre-condition: providers must be non-empty (existing behavior preserved)."""
    with pytest.raises(ValueError, match="empty providers tuple"):
        _resolve_provider("anything", ())


def test_resolve_explicit_empty_string_treated_as_no_explicit():
    """`provider: ''` (empty string) → fall back to model-based resolution, not raise."""
    # This shape can happen when the canvas writes an empty provider field.
    # Treating it as "no explicit pick" is more forgiving than raising,
    # since the user clearly didn't intend to break their workspace.
    p = _resolve_provider(
        "claude-opus-4-7", _BUILTIN_PROVIDERS,
        explicit_provider="",
    )
    assert p["name"] == "anthropic-api"  # fell through to model-based


def test_resolve_explicit_none_treated_as_no_explicit():
    """`explicit_provider=None` (default) → fall back to model-based resolution."""
    p = _resolve_provider(
        "claude-opus-4-7", _BUILTIN_PROVIDERS,
        explicit_provider=None,
    )
    assert p["name"] == "anthropic-api"
