"""The template default is platform-managed Gemini 4 Hydrogen (owner, 2026-11-17).

Gemini is the platform's model vendor: the Molecule proxy serves ``google/*``
through Google AI Studio, translating this runtime's Anthropic-protocol
requests (molecule-controlplane#3070). These tests pin the template half:

  * the default model, first on the menu, platform-managed with no personal
    key; Gemini 4 Carbon (the previous default) and Gemini 3.1 Flash-Lite (the
    cheapest, the e2e model) on the menu too;
  * every Gemini id routes to the ``platform`` provider (the proxy's Anthropic
    surface) by the ``google/`` prefix, and on the SSOT signal
    (MOLECULE_RESOLVED_PROVIDER=platform reaches ``_resolve_provider`` as the
    explicit provider name).

``_load_providers`` reads the template-bundled config.yaml (adjacent to
adapter.py), so this exercises the shipped registry, not a fixture.
"""

from pathlib import Path

import pytest
import yaml

from adapter import _load_providers, _resolve_provider

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
HYDROGEN = "google/gemini-4-hydrogen"
CARBON = "google/gemini-4-carbon"
FLASH_LITE = "google/gemini-3.1-flash-lite"
PROXY_ANTHROPIC = "https://api.moleculesai.app/api/v1/internal/llm/anthropic/v1"


def _config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def test_default_is_platform_gemini_hydrogen_first_on_the_menu():
    runtime_config = _config()["runtime_config"]
    assert runtime_config["model"] == HYDROGEN
    models = runtime_config["models"]
    assert models[0]["id"] == HYDROGEN
    by_id = {m["id"]: m for m in models}
    assert len(by_id) == len(models), "duplicate model ids on the menu"
    for model_id in (HYDROGEN, CARBON, FLASH_LITE):
        assert by_id[model_id]["provider"] == "platform"
        assert by_id[model_id]["required_env"] == []


def test_platform_provider_claims_the_google_namespace():
    platform = next(p for p in _config()["providers"] if p["name"] == "platform")
    assert "google/" in platform["model_prefixes"]
    assert platform["base_url"] == PROXY_ANTHROPIC


@pytest.mark.parametrize("model", [HYDROGEN, CARBON, FLASH_LITE])
def test_platform_gemini_resolves_to_the_proxy_by_prefix(model):
    provider = _resolve_provider(model, _load_providers("/nonexistent-configs"))
    assert provider["name"] == "platform"
    assert provider["base_url"] == PROXY_ANTHROPIC


@pytest.mark.parametrize("model", [HYDROGEN, CARBON, FLASH_LITE])
def test_platform_gemini_resolves_to_the_proxy_on_the_ssot_signal(model):
    provider = _resolve_provider(
        model, _load_providers("/nonexistent-configs"), explicit_provider="platform"
    )
    assert provider["name"] == "platform"
    assert provider["base_url"] == PROXY_ANTHROPIC
