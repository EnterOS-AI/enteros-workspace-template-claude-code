"""Unit tests for ClaudeCodeAdapter.setup + create_executor.

Two surfaces under test:
  1. setup() — provider-registry loading + auth-env validation +
     base_url resolution. Pins the post-2026-04-30 architecture where
     the model→provider mapping lives in /configs/config.yaml's
     `providers:` list (canonical) with `_BUILTIN_PROVIDERS` as the
     malformed-YAML fallback.
  2. create_executor() — the 2026-04-30 hang fix (custom upstream + no
     model = raise instead of silently passing 'sonnet' to the SDK).

These tests stub the import dependencies (molecule_runtime, a2a,
claude_sdk_executor) so they can run without the real packages installed.
"""

import os
import sys
import textwrap
import types
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest


# ---- Test scaffolding ----
#
# adapter.py imports at module load:
#   - molecule_runtime.adapters.base (BaseAdapter, AdapterConfig, RuntimeCapabilities)
#   - a2a.server.agent_execution (AgentExecutor)
# create_executor lazily imports claude_sdk_executor.ClaudeSDKExecutor.
# We stub all four so the test file can run in CI without those packages
# installed. The pre-validation branches we care about run BEFORE the
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
        # adapter.setup() lazy-imports molecule_runtime.plugins.load_plugins.
        # Stub it as a no-op returning [] so setup() pass-paths run cleanly
        # without needing the real runtime installed in the test env.
        mr.plugins = types.ModuleType("molecule_runtime.plugins")
        mr.plugins.load_plugins = lambda **_kwargs: []
        sys.modules["molecule_runtime"] = mr
        sys.modules["molecule_runtime.adapters"] = mr.adapters
        sys.modules["molecule_runtime.adapters.base"] = mr.adapters.base
        sys.modules["molecule_runtime.plugins"] = mr.plugins
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


# ---- Fixtures ----


# Canonical provider registry used by most setup() tests. Mirrors the
# real config.yaml's `providers:` list — kept inline here so a config.yaml
# rename/edit doesn't silently change test semantics. If the prod
# registry ever drifts from this fixture, the divergence is intentional
# and visible in the diff.
_FIXTURE_PROVIDERS_YAML = textwrap.dedent("""
    providers:
      - name: anthropic-oauth
        auth_mode: oauth
        model_prefixes: []
        model_aliases: [sonnet, opus, haiku]
        base_url: null
        auth_env: [CLAUDE_CODE_OAUTH_TOKEN]

      - name: anthropic-api
        auth_mode: anthropic_api
        model_prefixes: [claude-]
        model_aliases: []
        base_url: null
        auth_env: [ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN]

      - name: xiaomi-mimo
        auth_mode: third_party_anthropic_compat
        model_prefixes: [mimo-]
        model_aliases: []
        base_url: https://api.xiaomimimo.com/anthropic
        auth_env: [ANTHROPIC_AUTH_TOKEN, ANTHROPIC_API_KEY]

      - name: minimax
        auth_mode: third_party_anthropic_compat
        model_prefixes: [minimax-]
        model_aliases: []
        base_url: https://api.minimax.io/anthropic
        auth_env: [ANTHROPIC_AUTH_TOKEN, ANTHROPIC_API_KEY]

      - name: zai
        auth_mode: third_party_anthropic_compat
        model_prefixes: [glm-]
        model_aliases: []
        base_url: https://api.z.ai/api/anthropic
        auth_env: [ANTHROPIC_AUTH_TOKEN, ANTHROPIC_API_KEY]

      - name: moonshot
        auth_mode: third_party_anthropic_compat
        model_prefixes: [kimi-]
        model_aliases: []
        base_url: https://api.moonshot.ai/anthropic
        auth_env: [ANTHROPIC_AUTH_TOKEN, ANTHROPIC_API_KEY]

      - name: deepseek
        auth_mode: third_party_anthropic_compat
        model_prefixes: [deepseek-]
        model_aliases: []
        base_url: https://api.deepseek.com/anthropic
        auth_env: [ANTHROPIC_AUTH_TOKEN, ANTHROPIC_API_KEY]
""")


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


@pytest.fixture
def configs_dir(tmp_path):
    """Per-test /configs dir with the canonical provider registry written to
    config.yaml. Tests pass the path as ``config_path`` on _StubAdapterConfig
    so adapter.setup() reads our fixture rather than the host's real
    /configs/config.yaml (which doesn't exist in CI).
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_FIXTURE_PROVIDERS_YAML)
    return str(tmp_path)


@pytest.fixture
def empty_configs_dir(tmp_path):
    """A /configs dir with no config.yaml — exercises the FileNotFoundError
    fallback path in _load_providers (must yield _BUILTIN_PROVIDERS).
    """
    return str(tmp_path)


# ---- create_executor pre-validation tests ----
#
# These exercise the 2026-04-30 hang-fix branch: ANTHROPIC_BASE_URL
# pointed at a non-Anthropic shim with no model picked silently passes
# 'sonnet' to the SDK, which hangs for 30s on the --print probe. The
# adapter raises early instead.


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


# ---- setup() provider-registry tests ----
#
# Symmetric to create_executor's pre-validate: setup() raises on the
# inverse misconfig (third-party MODEL picked but ANTHROPIC_BASE_URL
# unset and the resolved provider has no default base_url). Both
# produce "boots but every LLM call fails" if not caught; raising at
# boot keeps the workspace from entering "online" status with
# structurally-broken auth.


@pytest.mark.asyncio
async def test_setup_passes_when_third_party_model_with_registered_base_url(
    adapter, monkeypatch, configs_dir
):
    """Third-party model + provider has default base_url in YAML →
    setup() auto-applies it (no operator URL needed) and runs cleanly
    through to plugin install. The Option B v2 happy path: pick mimo-
    or minimax- model in canvas, the registry handles routing.
    """
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    cfg = _StubAdapterConfig(
        runtime_config={"model": "mimo-v2-flash"}, config_path=configs_dir
    )

    await adapter.setup(cfg)

    # Registry-default base_url should now be in env for the SDK to pick up.
    assert os.environ.get("ANTHROPIC_BASE_URL") == "https://api.xiaomimimo.com/anthropic"


@pytest.mark.asyncio
async def test_setup_passes_for_minimax_model(adapter, monkeypatch, configs_dir):
    """MiniMax-M2 resolves to the minimax provider, auto-sets the MiniMax
    Anthropic-compat endpoint. Verifies registry adds new providers
    without code changes — the original motivation for the YAML registry.
    """
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    cfg = _StubAdapterConfig(
        runtime_config={"model": "MiniMax-M2"}, config_path=configs_dir
    )

    await adapter.setup(cfg)

    assert os.environ.get("ANTHROPIC_BASE_URL") == "https://api.minimax.io/anthropic"


@pytest.mark.asyncio
async def test_setup_minimax_case_insensitive_match(
    adapter, monkeypatch, configs_dir
):
    """MiniMax docs use mixed-case ids (MiniMax-M2.7); some operators may
    type minimax-m2.7. Both must resolve to the same provider — registry
    matches lowercased prefixes.
    """
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    cfg = _StubAdapterConfig(
        runtime_config={"model": "minimax-m2.7"}, config_path=configs_dir
    )

    await adapter.setup(cfg)

    assert os.environ.get("ANTHROPIC_BASE_URL") == "https://api.minimax.io/anthropic"


@pytest.mark.asyncio
async def test_setup_operator_base_url_overrides_registry_default(
    adapter, monkeypatch, configs_dir
):
    """Operator-set ANTHROPIC_BASE_URL wins over the provider's default —
    escape hatch for regional endpoints (Xiaomi token-plan-sgp.*,
    MiniMax api.minimaxi.com China endpoint). Pinning this so a future
    refactor can't quietly clobber the override.
    """
    monkeypatch.setenv(
        "ANTHROPIC_BASE_URL",
        "https://token-plan-sgp.xiaomimimo.com/anthropic",
    )
    cfg = _StubAdapterConfig(
        runtime_config={"model": "mimo-v2-flash"}, config_path=configs_dir
    )

    await adapter.setup(cfg)

    # Operator value untouched — adapter must not overwrite.
    assert (
        os.environ.get("ANTHROPIC_BASE_URL")
        == "https://token-plan-sgp.xiaomimimo.com/anthropic"
    )


@pytest.mark.asyncio
async def test_setup_passes_when_oauth_model_no_base_url(
    adapter, monkeypatch, configs_dir
):
    """OAuth-aliased models (sonnet/opus/haiku) are Anthropic-native; no
    base URL is required. setup() must not raise on the OAuth path even
    though base_url is unset — that's the historical happy path.
    """
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    cfg = _StubAdapterConfig(
        runtime_config={"model": "sonnet"}, config_path=configs_dir
    )

    await adapter.setup(cfg)


@pytest.mark.asyncio
async def test_setup_passes_when_anthropic_api_model_no_base_url(
    adapter, monkeypatch, configs_dir
):
    """claude-* versioned ids are Anthropic API-key path; base URL
    optional (defaults to api.anthropic.com). setup() must not raise.
    """
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    cfg = _StubAdapterConfig(
        runtime_config={"model": "claude-sonnet-4-6"},
        config_path=configs_dir,
    )

    await adapter.setup(cfg)


@pytest.mark.asyncio
async def test_setup_falls_back_to_builtin_when_yaml_missing(
    adapter, monkeypatch, empty_configs_dir
):
    """No config.yaml in the configs dir → _load_providers falls back to
    _BUILTIN_PROVIDERS (oauth + anthropic-api only). OAuth-aliased models
    must still resolve cleanly so a bare-bones workspace boots even if
    config.yaml is missing or malformed.
    """
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    cfg = _StubAdapterConfig(
        runtime_config={"model": "sonnet"}, config_path=empty_configs_dir
    )

    await adapter.setup(cfg)


@pytest.mark.asyncio
async def test_setup_raises_when_yaml_missing_and_third_party_model(
    adapter, monkeypatch, empty_configs_dir
):
    """No config.yaml + third-party model picked → builtin registry has no
    matching prefix → resolves to the OAuth fallback (provider[0]). The
    user picked a model the builtin can't route, so OAuth's auth_env
    won't have the right key, but it won't raise here — auth check is
    a warning, not an error. setup() should complete (no third-party
    misconfig fires because the fallback isn't third-party).

    Documented behavior: when YAML is missing, third-party models are
    silently downgraded to OAuth fallback. Operators must fix their
    config.yaml to get correct routing. This test pins that the failure
    mode is "warning + boots" rather than "raises" (helps debug-vs-recover
    triage when CI loses the YAML somehow).
    """
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    cfg = _StubAdapterConfig(
        runtime_config={"model": "mimo-v2-flash"}, config_path=empty_configs_dir
    )

    # No raise — falls back to OAuth provider, third-party gate doesn't fire.
    await adapter.setup(cfg)


@pytest.mark.asyncio
async def test_setup_auth_token_alone_satisfies_third_party_check(
    adapter, monkeypatch, configs_dir, caplog
):
    """MiniMax docs prefer ANTHROPIC_AUTH_TOKEN over ANTHROPIC_API_KEY.
    The provider entry lists both as accepted; setting only AUTH_TOKEN
    must NOT trigger the "no auth env set" warning.
    """
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-minimax-token")
    cfg = _StubAdapterConfig(
        runtime_config={"model": "MiniMax-M2"}, config_path=configs_dir
    )

    import logging
    with caplog.at_level(logging.WARNING):
        await adapter.setup(cfg)

    auth_warnings = [r for r in caplog.records if "AuthenticationError" in r.getMessage()]
    assert auth_warnings == [], (
        "ANTHROPIC_AUTH_TOKEN alone should satisfy minimax provider auth "
        "but adapter logged a missing-auth warning anyway"
    )


# ---- _load_providers / _resolve_provider unit tests ----


def test_load_providers_returns_builtin_when_yaml_missing(tmp_path):
    """FileNotFoundError path returns the in-code defaults verbatim."""
    _install_stubs()
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    sys.modules.pop("adapter", None)
    import adapter as adapter_module

    result = adapter_module._load_providers(str(tmp_path))
    assert result == adapter_module._BUILTIN_PROVIDERS


def test_load_providers_parses_yaml_and_normalizes(tmp_path):
    """YAML present + parses → normalized tuple of provider dicts."""
    _install_stubs()
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    sys.modules.pop("adapter", None)
    import adapter as adapter_module

    (tmp_path / "config.yaml").write_text(_FIXTURE_PROVIDERS_YAML)
    result = adapter_module._load_providers(str(tmp_path))

    assert len(result) == 7
    names = [p["name"] for p in result]
    assert names == [
        "anthropic-oauth", "anthropic-api", "xiaomi-mimo", "minimax",
        "zai", "moonshot", "deepseek",
    ]
    # YAML lists must be normalized to tuples for downstream lookup ergonomics.
    assert isinstance(result[0]["model_aliases"], tuple)
    assert isinstance(result[2]["model_prefixes"], tuple)


@pytest.mark.parametrize("model,expected_provider,expected_url", [
    ("GLM-4.6", "zai", "https://api.z.ai/api/anthropic"),
    ("glm-4.5", "zai", "https://api.z.ai/api/anthropic"),
    ("kimi-k2.5", "moonshot", "https://api.moonshot.ai/anthropic"),
    ("deepseek-v4-pro", "deepseek", "https://api.deepseek.com/anthropic"),
])
@pytest.mark.asyncio
async def test_setup_routes_extra_providers(
    adapter, monkeypatch, configs_dir, model, expected_provider, expected_url
):
    """The Z.ai / Moonshot / DeepSeek providers added in this PR must
    route correctly: model id → provider entry → ANTHROPIC_BASE_URL.
    Parametrized to keep the matrix coverage tight without 3 near-identical
    test bodies. Locks in the per-vendor base_url so a future YAML edit
    that mistypes z.ai's `/api/anthropic` suffix gets caught.
    """
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    cfg = _StubAdapterConfig(
        runtime_config={"model": model}, config_path=configs_dir
    )

    await adapter.setup(cfg)

    assert os.environ.get("ANTHROPIC_BASE_URL") == expected_url


def test_load_providers_falls_back_on_malformed_yaml(tmp_path, caplog):
    """Malformed YAML → log warning + fallback (don't kill boot)."""
    _install_stubs()
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    sys.modules.pop("adapter", None)
    import adapter as adapter_module

    (tmp_path / "config.yaml").write_text("providers: [not valid yaml: {{{")

    import logging
    with caplog.at_level(logging.WARNING):
        result = adapter_module._load_providers(str(tmp_path))

    assert result == adapter_module._BUILTIN_PROVIDERS


def test_resolve_provider_minimax_prefix_matches_minimax_provider():
    """The headline routing test: MiniMax-M2 lands on the minimax entry."""
    _install_stubs()
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    sys.modules.pop("adapter", None)
    import adapter as adapter_module

    providers = tuple(
        adapter_module._normalize_provider(p) for p in [
            {"name": "anthropic-oauth", "auth_mode": "oauth",
             "model_aliases": ["sonnet"], "auth_env": ["CLAUDE_CODE_OAUTH_TOKEN"]},
            {"name": "minimax", "auth_mode": "third_party_anthropic_compat",
             "model_prefixes": ["minimax-"],
             "base_url": "https://api.minimax.io/anthropic",
             "auth_env": ["ANTHROPIC_AUTH_TOKEN"]},
        ]
    )

    result = adapter_module._resolve_provider("MiniMax-M2", providers)
    assert result["name"] == "minimax"

    # Case insensitivity also exercised.
    result2 = adapter_module._resolve_provider("minimax-m2.7", providers)
    assert result2["name"] == "minimax"


def test_load_providers_drops_bad_entry_keeps_rest(tmp_path, caplog):
    """Per-entry isolation: one malformed entry shouldn't nuke the registry.

    Pre-fix: ``_load_providers`` built the registry via a generator inside
    ``tuple(...)``. A single AttributeError mid-comprehension propagated
    out and the broad except caught it, silently reverting to
    ``_BUILTIN_PROVIDERS`` (oauth + anthropic-api only). Every third-party
    model would then route to anthropic-oauth — exactly the silent-fallback
    failure mode this PR was meant to eliminate.

    Post-fix: per-entry try/except drops the bad entry with a warning,
    rest of the registry survives.
    """
    _install_stubs()
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    sys.modules.pop("adapter", None)
    import adapter as adapter_module

    yaml_with_typo = textwrap.dedent("""
        providers:
          - name: good-zai
            auth_mode: third_party_anthropic_compat
            model_prefixes: [glm-]
            base_url: https://api.z.ai/api/anthropic
            auth_env: [ANTHROPIC_AUTH_TOKEN]

          # Operator typo: forgot list brackets, ints slipped in.
          # Pre-fix: AttributeError on the int's .lower() killed the
          # whole tuple build → registry fell back to builtins.
          - name: bad-one
            auth_mode: third_party_anthropic_compat
            model_prefixes: [bad-, 123]
            base_url: https://example.com
            auth_env: [SOME_TOKEN]

          - name: good-anthropic
            auth_mode: anthropic_api
            model_prefixes: [claude-]
            auth_env: [ANTHROPIC_API_KEY]
    """)
    (tmp_path / "config.yaml").write_text(yaml_with_typo)

    import logging
    with caplog.at_level(logging.WARNING):
        result = adapter_module._load_providers(str(tmp_path))

    # All three entries survive — the integer is dropped, the rest of
    # the bad-one entry's prefix list is kept (just `bad-`).
    names = [p["name"] for p in result]
    assert names == ["good-zai", "bad-one", "good-anthropic"], (
        f"Expected all three entries to survive (with the int dropped from "
        f"bad-one's prefixes), got {names}"
    )

    # Confirm the int got skipped, not silently coerced or crash-bubbled.
    bad = next(p for p in result if p["name"] == "bad-one")
    assert bad["model_prefixes"] == ("bad-",), (
        f"Non-string list element should be dropped; got {bad['model_prefixes']}"
    )

    # Operator should see a warning so they can fix the YAML.
    assert any("non-string" in r.getMessage() for r in caplog.records), (
        "Expected a warning about the non-string list item"
    )


def test_load_providers_string_as_prefix_does_not_split_into_chars(tmp_path, caplog):
    """A YAML field declared as list-of-strings but written as a bare
    string (operator forgot brackets) used to silently iterate over
    characters → ``('m','i','m','o','-')``. Post-fix: non-list value
    coerces to empty tuple with no exception. The entry survives but
    matches nothing — operator notices in the boot banner instead of
    via mysteriously-misrouted requests.
    """
    _install_stubs()
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    sys.modules.pop("adapter", None)
    import adapter as adapter_module

    yaml_str_prefix = textwrap.dedent("""
        providers:
          - name: typo-prefix
            auth_mode: third_party_anthropic_compat
            model_prefixes: mimo-
            base_url: https://api.xiaomimimo.com/anthropic
            auth_env: [ANTHROPIC_AUTH_TOKEN]
    """)
    (tmp_path / "config.yaml").write_text(yaml_str_prefix)

    result = adapter_module._load_providers(str(tmp_path))
    typo = next(p for p in result if p["name"] == "typo-prefix")
    assert typo["model_prefixes"] == (), (
        f"String value (forgot brackets) must coerce to empty tuple, not "
        f"split into characters; got {typo['model_prefixes']}"
    )


def test_load_providers_drops_entry_without_name(tmp_path, caplog):
    """An entry without ``name`` is operator error — no silent fallback
    to ``<unnamed>``. Drop the entry with a warning so the boot log
    surfaces the typo.
    """
    _install_stubs()
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    sys.modules.pop("adapter", None)
    import adapter as adapter_module

    yaml_no_name = textwrap.dedent("""
        providers:
          - name: good
            auth_mode: oauth
            auth_env: [CLAUDE_CODE_OAUTH_TOKEN]
          - auth_mode: third_party_anthropic_compat
            model_prefixes: [foo-]
    """)
    (tmp_path / "config.yaml").write_text(yaml_no_name)

    import logging
    with caplog.at_level(logging.WARNING):
        result = adapter_module._load_providers(str(tmp_path))

    assert [p["name"] for p in result] == ["good"]
    assert any("without a string name" in r.getMessage() for r in caplog.records)


def test_resolve_provider_falls_back_to_first_when_unknown():
    """Unknown model id → fallback to first provider (OAuth by convention)."""
    _install_stubs()
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    sys.modules.pop("adapter", None)
    import adapter as adapter_module

    providers = tuple(
        adapter_module._normalize_provider(p) for p in [
            {"name": "anthropic-oauth", "auth_mode": "oauth",
             "auth_env": ["CLAUDE_CODE_OAUTH_TOKEN"]},
            {"name": "minimax", "auth_mode": "third_party_anthropic_compat",
             "model_prefixes": ["minimax-"],
             "auth_env": ["ANTHROPIC_AUTH_TOKEN"]},
        ]
    )

    result = adapter_module._resolve_provider("some-unknown-model", providers)
    assert result["name"] == "anthropic-oauth"


# ---- _strip_provider_prefix tests (2026-05-01 exit-1 root cause) ----
#
# Wheel's molecule_runtime/config.py defaults model to
# "anthropic:claude-opus-4-7" so langchain/crewai consumers get a uniform
# LangChain-style provider:model string. The claude CLI rejects prefixed
# strings and exits 1 silently. Adapter must strip known-Claude prefixes
# before either provider routing (setup) or CLI invocation (executor)
# touches the value.


def _adapter_module():
    _install_stubs()
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    sys.modules.pop("adapter", None)
    import adapter as adapter_module  # noqa: WPS433
    return adapter_module


def test_strip_provider_prefix_strips_anthropic():
    """The exact wheel default must reach downstream as the bare id."""
    mod = _adapter_module()
    assert mod._strip_provider_prefix("anthropic:claude-opus-4-7") == "claude-opus-4-7"


def test_strip_provider_prefix_strips_claude():
    """Operators sometimes write `claude:opus-4-7`; treat as the same prefix."""
    mod = _adapter_module()
    assert mod._strip_provider_prefix("claude:opus-4-7") == "opus-4-7"


def test_strip_provider_prefix_keeps_unprefixed():
    """Bare ids and aliases pass through unchanged."""
    mod = _adapter_module()
    assert mod._strip_provider_prefix("sonnet") == "sonnet"
    assert mod._strip_provider_prefix("claude-opus-4-7") == "claude-opus-4-7"
    assert mod._strip_provider_prefix("MiniMax-M2") == "MiniMax-M2"


def test_strip_provider_prefix_keeps_unknown_prefix():
    """Unknown prefixes (e.g. openai:) pass through so the CLI fails loudly
    instead of being silently mangled into a half-recognized name."""
    mod = _adapter_module()
    assert mod._strip_provider_prefix("openai:gpt-4") == "openai:gpt-4"
    assert mod._strip_provider_prefix("bedrock:claude-3") == "bedrock:claude-3"


def test_strip_provider_prefix_handles_empty():
    """Empty string returns empty — used by create_executor before the
    'or sonnet' fallback so the strip path can't crash on the missing-model
    code path."""
    mod = _adapter_module()
    assert mod._strip_provider_prefix("") == ""


@pytest.mark.asyncio
async def test_create_executor_strips_anthropic_prefix(adapter, monkeypatch):
    """End-to-end: the wheel default ("anthropic:claude-opus-4-7") reaches
    ClaudeSDKExecutor as the bare id. Without this strip the claude CLI
    silently exits 1 mid-A2A.
    """
    import claude_sdk_executor

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    claude_sdk_executor.ClaudeSDKExecutor.reset_mock()

    cfg = _StubAdapterConfig(
        runtime_config={"model": "anthropic:claude-opus-4-7"}
    )
    await adapter.create_executor(cfg)

    kwargs = claude_sdk_executor.ClaudeSDKExecutor.call_args.kwargs
    assert kwargs["model"] == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_create_executor_strips_anthropic_prefix_dataclass(
    adapter, monkeypatch
):
    """Symmetric coverage of dataclass-shaped runtime_config — the same
    wheel default arrives via that shape in production via main.py's
    load_config path.
    """
    import claude_sdk_executor

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    claude_sdk_executor.ClaudeSDKExecutor.reset_mock()

    @dataclass
    class _RC:
        model: str = "anthropic:claude-opus-4-7"

    cfg = _StubAdapterConfig(runtime_config=_RC())
    await adapter.create_executor(cfg)

    kwargs = claude_sdk_executor.ClaudeSDKExecutor.call_args.kwargs
    assert kwargs["model"] == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_setup_strip_routes_prefixed_anthropic_to_anthropic_api(
    adapter, monkeypatch, configs_dir, caplog
):
    """With the prefix intact, `anthropic:claude-opus-4-7` doesn't match
    anthropic-api's model_prefixes=("claude-",) and falls back to
    anthropic-oauth — wrong for users on ANTHROPIC_API_KEY. The strip in
    setup() must run BEFORE _resolve_provider so routing sees the bare id.
    """
    import logging

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    cfg = _StubAdapterConfig(
        runtime_config={"model": "anthropic:claude-opus-4-7"},
        config_path=configs_dir,
    )

    with caplog.at_level(logging.INFO, logger="adapter"):
        await adapter.setup(cfg)

    banner = next(
        (r.getMessage() for r in caplog.records
         if "Claude Code adapter starting" in r.getMessage()),
        "",
    )
    assert "provider=anthropic-api" in banner, (
        f"Expected provider=anthropic-api after stripping prefix; banner={banner!r}"
    )
