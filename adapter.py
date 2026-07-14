"""Claude Code adapter — wraps the Claude Code CLI as an agent runtime."""

import json
import os
import logging
from pathlib import Path
from urllib.parse import urlparse

from molecule_runtime.adapters.base import BaseAdapter, AdapterConfig, RuntimeCapabilities
from a2a.server.agent_execution import AgentExecutor

logger = logging.getLogger(__name__)

# Cap one transcript response at 1000 lines so a paranoid client can't OOM
# the workspace by polling /transcript?limit=999999.
_TRANSCRIPT_MAX_LIMIT = 1000

# Auth env names to audit at boot. Order is informational; presence/absence
# of each is logged so the operator can see at a glance which key the
# workspace was started with vs which is missing. NEVER log values — just
# the boolean "set"/"unset" per name. Adding a new vendor: add its env
# name here so the audit reports it too. Keep in sync with the matching
# list in entrypoint.sh's log_boot_context().
_AUTH_ENV_AUDIT = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "MINIMAX_API_KEY",
    "GLM_API_KEY",
    "KIMI_API_KEY",
    "DEEPSEEK_API_KEY",
)


def _audit_auth_env_presence() -> None:
    """Log a one-line snapshot of which auth env names are set.

    Logs NAMES + presence ("set"/"unset"), never VALUES. Lets an
    operator reading docker logs answer "is this a missing key
    problem or a routing problem?" in one glance. The boot-banner in
    setup() answers "which provider got picked"; this audit answers
    "is the env even there for it." Together they make the
    crash-loop diagnosis path that bit us 2026-05-02 a one-line read.
    """
    snapshot = ", ".join(
        f"{name}={'set' if os.environ.get(name) else 'unset'}"
        for name in _AUTH_ENV_AUDIT
    )
    logger.info("auth env audit: %s", snapshot)


# Harness model-tier env vars (internal#702). The Claude Code harness reads
# these to pick the model for its NON-main tiers — the background/"small-fast"
# tier (title-gen, conversation summarization, quota probes), the
# haiku/sonnet/opus aliases, and the subagent tier. When NONE of them is set
# the harness falls back to a literal `claude-3-5-haiku`; on a non-Anthropic
# platform_managed workspace that `claude-*` id inherits ANTHROPIC_BASE_URL
# (the CP proxy) and the proxy routes any `claude*` slug to real Anthropic →
# the depleted platform key → "credit balance too low". CP now injects the
# correct same-provider values for these (see molecule-controlplane
# tenant_config.go `platformManagedHarnessModelEnv`); the adapter's job is
# ONLY to make sure they survive to the spawned `claude` process.
#
# PASSTHROUGH CONTRACT: the adapter does NOT build the SDK options with an
# `env=` allow-list — `ClaudeSDKExecutor._build_options` constructs
# `ClaudeAgentOptions` without an `env` kwarg, so the claude-agent-sdk
# subprocess transport inherits the full container `os.environ`. These names
# are therefore forwarded automatically, exactly like ANTHROPIC_API_KEY /
# ANTHROPIC_BASE_URL. They are listed here so (a) the boot audit reports them
# and (b) test_harness_model_env_passthrough pins the no-allow-list contract,
# so a future refactor that adds an `env=` filter cannot silently re-open
# the internal#702 leak. The adapter NEVER hardcodes model ids — it forwards
# whatever CP injected.
_HARNESS_MODEL_ENV = (
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "ENABLE_TOOL_SEARCH",
)


def _audit_harness_model_env() -> None:
    """Log a one-line snapshot of the harness model-tier env (internal#702).

    Logs NAMES + presence ("set"/"unset"), never VALUES — same contract as
    _audit_auth_env_presence. When every name reads `unset` on a
    non-Anthropic workspace, the harness's background tier will fall back to
    `claude-3-5-haiku` and leak to real Anthropic (internal#702); this audit
    makes that diagnosable from one log line.
    """
    snapshot = ", ".join(
        f"{name}={'set' if os.environ.get(name) else 'unset'}"
        for name in _HARNESS_MODEL_ENV
    )
    logger.info("harness model-tier env audit: %s", snapshot)


# Auth-mode constants — provider entries use one of these strings.
# Drives validation behavior in setup() (third-party requires base_url
# resolution; oauth/anthropic-api leave base_url=None for CLI defaults).
_AUTH_MODE_OAUTH = "oauth"
_AUTH_MODE_ANTHROPIC_API = "anthropic_api"
_AUTH_MODE_THIRD_PARTY = "third_party_anthropic_compat"

# Built-in provider registry — used as a fallback when /configs/config.yaml
# doesn't define `providers:`. The canonical registry is the YAML file: it
# becomes the single source of truth read by both this adapter (for boot-time
# routing) and the canvas Config tab (Provider dropdown). Adding a new
# provider should be a one-line YAML edit, not a code change. This builtin
# exists so a workspace with a malformed/missing config.yaml still boots
# with sensible defaults instead of failing.
_BUILTIN_PROVIDERS = (
    {
        "name": "anthropic-oauth",
        "auth_mode": _AUTH_MODE_OAUTH,
        "model_prefixes": (),
        "model_aliases": ("sonnet", "opus", "haiku"),
        "base_url": None,
        "auth_env": ("CLAUDE_CODE_OAUTH_TOKEN",),
    },
    {
        "name": "anthropic-api",
        "auth_mode": _AUTH_MODE_ANTHROPIC_API,
        "model_prefixes": ("claude-",),
        "model_aliases": (),
        "base_url": None,
        "auth_env": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    },
)


def _coerce_string_list(value, lowercase: bool = False) -> tuple:
    """Defensively coerce a YAML field expected to be a list-of-strings.

    Operator typos in config.yaml come in two shapes that both used to
    silently produce wrong routing:
      1. forgot brackets:  ``model_prefixes: mimo-``  (string, not list)
      2. mixed types:      ``model_prefixes: [mimo-, 123]``  (int slips in)

    Case 1 used to iterate over characters → ``('m','i','m','o','-')``,
    making the entry match every model whose id starts with any of those
    letters. Case 2 raised AttributeError mid-comprehension, killing the
    whole registry build and silently falling back to builtins-only —
    exactly the silent-fallback failure mode this PR was meant to fix.

    Returns an empty tuple for any non-list (treated as "no entries");
    drops non-string items in the list with a warning.

    ``lowercase`` controls case-folding: True for case-insensitive
    comparisons (model_prefixes, model_aliases — operators write
    ``MiniMax-M2`` in YAML, model id arrives lowercased downstream),
    False to preserve case (auth_env — env var names are
    case-sensitive: ``CLAUDE_CODE_OAUTH_TOKEN`` ≠
    ``claude_code_oauth_token``).
    """
    if not isinstance(value, list):
        return ()
    out = []
    for item in value:
        if not isinstance(item, str):
            logger.warning(
                "providers: skipping non-string list item %r (type %s)",
                item, type(item).__name__,
            )
            continue
        out.append(item.lower() if lowercase else item)
    return tuple(out)


def _normalize_provider(entry: dict):
    """Coerce a YAML-loaded provider dict into the shape adapter logic expects.

    YAML gives us lists (not tuples) and may omit optional keys. Normalize
    to the union of all fields so downstream lookups work without scattered
    .get(...) calls. Returns ``None`` for entries that can't be salvaged
    (e.g. missing name) so the caller can drop them without poisoning the
    rest of the registry.
    """
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not name or not isinstance(name, str):
        logger.warning("providers: skipping entry without a string name: %r", entry)
        return None
    return {
        "name": name,
        "auth_mode": entry.get("auth_mode") or _AUTH_MODE_OAUTH,
        "model_prefixes": _coerce_string_list(entry.get("model_prefixes"), lowercase=True),
        "model_aliases": _coerce_string_list(entry.get("model_aliases"), lowercase=True),
        "base_url": entry.get("base_url") or None,
        "auth_env": _coerce_string_list(entry.get("auth_env"), lowercase=False),
        # Which env var the boot-time vendor-key projection writes the
        # vendor key INTO. Defaults to ANTHROPIC_AUTH_TOKEN (Bearer-style
        # — correct for MiniMax/GLM/DeepSeek Anthropic-compat shims).
        # Kimi For Coding's gateway authenticates with the x-api-key
        # header (per kimi.com's official Claude Code doc), which the
        # Anthropic SDK / claude CLI emits from ANTHROPIC_API_KEY — so
        # that provider's entry sets auth_token_env: ANTHROPIC_API_KEY.
        # Env-var names are case-sensitive; preserve case.
        "auth_token_env": (
            entry.get("auth_token_env")
            if isinstance(entry.get("auth_token_env"), str)
            and entry.get("auth_token_env").strip()
            else "ANTHROPIC_AUTH_TOKEN"
        ),
    }


# Legacy install path retained for older and self-managed layouts. The
# published image loads config.yaml beside adapter.py in /app; checking this
# compatibility path first also protects older site-packages installs.
_CANONICAL_ADAPTER_DIR = "/opt/adapter"

# Adjacent-to-adapter.py path. Module-level so tests can monkeypatch it
# to redirect the path-2 lookup at a controlled tmp dir. Production code
# resolves this once at import time and never touches it again — same
# semantics as before.
_TEMPLATE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_providers(config_path: str) -> tuple:
    """Load the provider registry from the template's bundled config.yaml.

    The providers list is a TEMPLATE concern — it describes which
    models/auth-modes this runtime image supports — and ships in the
    template's own config.yaml alongside adapter.py. The per-workspace
    ``${WORKSPACE_CONFIG_PATH}/config.yaml`` (default ``/configs/``)
    only contains workspace-specific overrides (model, runtime, skills,
    prompt files) and does NOT carry a providers section.

    Two-step incident history:
      • Pre-2026-05-04 09:00Z: only checked ``config_path``, fell back
        to ``_BUILTIN_PROVIDERS`` (oauth + anthropic-api). Every
        MiniMax / GLM / Kimi / DeepSeek model resolved to
        ``anthropic-oauth`` and crashed at first LLM call with
        "Not logged in. Please run /login". Fixed by adding a
        template-bundled lookup using
        ``os.path.dirname(os.path.abspath(__file__))``.
      • 2026-05-04 11:08Z: that ``__file__`` lookup missed on legacy
        host installs because the provisioner copied adapter.py to
        ``/opt/molecule-venv/lib/python3.12/site-packages/`` —
        site-packages wins over PYTHONPATH=/opt/adapter (which the
        host install doesn't set), so __file__ resolves to the venv
        path WITHOUT an adjacent config.yaml. Same silent fallback
        to anthropic-oauth + same "Not logged in" symptom.
      • 2026-05-08 (#129): the multi-path lookup that fixed both of
        the above was lost in a post-suspension migration cycle (the
        Gitea main branch never carried the fix even though the
        :latest image had it baked in from a prior build). Canary
        chronic red for 38h before this commit restored the lookup.

    Resolution order:
      1. ``/opt/adapter/config.yaml`` — compatibility path for older and
         explicitly self-managed installs. Robust against a site-packages
         copy that has no adjacent config.
      2. Adjacent to ``adapter.__file__`` — current published-image path
         (``/app/config.yaml``) and the normal dev/test path.
      3. Per-workspace ``${config_path}/config.yaml`` — fallback for
         operator-shipped overrides on a private deployment that
         wants a custom providers list.
      4. ``_BUILTIN_PROVIDERS`` — oauth + anthropic-api defaults so a
         bare-bones workspace still boots even with no config.yaml
         anywhere.

    Per-entry isolation: a single bad provider entry is dropped with
    a warning; the rest of the registry survives.
    """
    canonical_yaml = os.path.join(_CANONICAL_ADAPTER_DIR, "config.yaml")
    template_yaml = os.path.join(_TEMPLATE_DIR, "config.yaml")
    workspace_yaml = os.path.join(config_path, "config.yaml")
    # Deduplicate while preserving order — _CANONICAL_ADAPTER_DIR and
    # the __file__ dir collide in dev/test (when imported from
    # /opt/adapter directly), and workspace_yaml may also collide if
    # config_path == /opt/adapter in tests.
    seen = set()
    candidates = []
    for path in (canonical_yaml, template_yaml, workspace_yaml):
        if path not in seen:
            seen.add(path)
            candidates.append(path)

    raw = None
    chosen_path = None
    try:
        import yaml  # transitive dep via molecules-workspace-runtime
    except ImportError:
        logger.warning("providers: yaml import failed; using builtins")
        return _BUILTIN_PROVIDERS

    for yaml_path in candidates:
        try:
            with open(yaml_path, "r") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.info("providers: %s not found, trying next candidate", yaml_path)
            continue
        except Exception as exc:  # noqa: BLE001 — defensive: never block boot on YAML
            logger.warning(
                "providers: failed to load from %s (%s); trying next candidate",
                yaml_path, exc,
            )
            continue

        candidate_raw = data.get("providers") if isinstance(data, dict) else None
        if isinstance(candidate_raw, list) and candidate_raw:
            raw = candidate_raw
            chosen_path = yaml_path
            break

    if raw is None:
        logger.info(
            "providers: no providers section found in %s; using builtin defaults",
            " or ".join(candidates),
        )
        return _BUILTIN_PROVIDERS

    parsed = []
    for entry in raw:
        try:
            normalized = _normalize_provider(entry)
        except Exception as exc:  # noqa: BLE001 — per-entry isolation
            logger.warning("providers: dropping unparseable entry %r (%s)", entry, exc)
            continue
        if normalized is not None:
            parsed.append(normalized)

    if not parsed:
        logger.warning("providers: no valid entries in %s; using builtins", chosen_path)
        return _BUILTIN_PROVIDERS
    logger.info("providers: loaded %d entries from %s", len(parsed), chosen_path)
    return tuple(parsed)


# Aliases for `MODEL_PROVIDER` env values that should map to a registry
# provider name. The persona env files use shorter / friendlier slugs
# than the registry's canonical names — without this alias map a value
# like ``MODEL_PROVIDER=claude-code`` would fall through to YAML-based
# resolution and (when the YAML doesn't pin a provider) hit the
# model-prefix matcher with the operator-picked MODEL, mis-routing a
# lead workspace through MiniMax even though its CLAUDE_CODE_OAUTH_TOKEN
# was clearly meant to be used.
#
# Maintain this list in sync with the persona env file convention:
#   - ``claude-code``  → ``anthropic-oauth`` (Claude Code subscription path)
#   - ``anthropic``    → ``anthropic-api``  (direct Anthropic API key)
# Provider names already in the registry alias to themselves implicitly
# (the ``in registry`` check catches them before this map is consulted).
_PROVIDER_SLUG_ALIASES = {
    "claude-code": "anthropic-oauth",
    "anthropic": "anthropic-api",
}


def _resolve_model_and_provider_from_env(
    yaml_model: str,
    yaml_provider: str,
    providers: tuple,
) -> tuple:
    """Reconcile model + provider from env vars vs YAML, with the persona-env
    convention winning over the legacy ``MODEL_PROVIDER``-as-model-id usage.

    The persona env files (``~/.molecule-ai/personas/<name>/env`` on the host,
    sourced into each workspace container at provision time) declare TWO env
    vars with distinct semantics:

      * ``MODEL`` — the model id (e.g. ``MiniMax-M2.7-highspeed``, ``opus``).
      * ``MODEL_PROVIDER`` — the provider slug (e.g. ``minimax``,
        ``claude-code``, ``anthropic``).

    The shared ``molecule_runtime/config.py`` (in molecules-workspace-runtime)
    historically interpreted ``MODEL_PROVIDER`` as the *model id* — a name
    chosen before there was a separate ``MODEL`` env var. When both env vars
    are set with the persona convention, the legacy code reads
    ``MODEL_PROVIDER=minimax`` into ``runtime_config.model``, which then
    fails to match any registry prefix (``minimax-`` requires a hyphen
    suffix) and silently falls through to providers[0] (``anthropic-oauth``).
    OAuth-token-less workspaces then wedge at ``query.initialize()`` because
    the claude CLI can't authenticate. This is the 2026-05-08 dev-tree
    incident — 22/27 non-lead workspaces stuck in ``degraded``.

    Resolution order (this function):
      1. ``MODEL`` env var → picked_model. Authoritative when set; the
         persona env always sets it alongside ``MODEL_PROVIDER`` so the
         model id never has to be inferred.
      2. ``MODEL_PROVIDER`` env var → explicit_provider, BUT only when the
         value matches a known provider name in the registry. This guards
         against the legacy case where some callers still set
         ``MODEL_PROVIDER`` to a model id (e.g. canvas Save+Restart prior to
         this fix). If the value isn't a registered provider name and YAML
         didn't supply a model, treat it as a model id for back-compat.
      3. YAML ``runtime_config.model`` / ``provider`` — used for any field
         the env didn't supply. Carries the operator's canvas selection
         on workspaces that haven't yet adopted the persona env shape.

    Returns ``(picked_model, explicit_provider_name)``. Either may be
    empty/None — the caller (``setup``) handles the empty cases via
    ``_resolve_provider``'s registry fallback.
    """
    env_model = (os.environ.get("MODEL") or "").strip()
    env_provider = (os.environ.get("MODEL_PROVIDER") or "").strip()
    provider_names_lower = {p.get("name", "").lower() for p in providers}

    # Detect whether MODEL_PROVIDER carries the persona-convention slug
    # (provider name) vs. the legacy convention (model id). Persona-
    # convention wins when the value matches a registered provider; we
    # fall back to legacy interpretation only when it doesn't.
    #
    # First, apply the alias map so persona-friendly slugs like
    # ``claude-code`` resolve to the canonical registry name
    # ``anthropic-oauth``. Without this, a lead workspace's
    # ``MODEL_PROVIDER=claude-code`` env would fall through to the model-
    # prefix matcher, see ``MODEL=MiniMax-M2.7`` and mis-route to MiniMax
    # even though the operator's intent (and the OAuth token they set)
    # was the OAuth subscription path.
    env_provider_resolved = _PROVIDER_SLUG_ALIASES.get(
        env_provider.lower(), env_provider,
    ) if env_provider else ""
    env_provider_is_slug = (
        bool(env_provider_resolved)
        and env_provider_resolved.lower() in provider_names_lower
    )

    # Picked model resolution
    if env_model:
        picked_model = env_model
    elif env_provider and not env_provider_is_slug:
        # Legacy: MODEL_PROVIDER env carried the model id. Honor it so
        # canvas Save+Restart workflows that predate this fix keep working.
        picked_model = env_provider
    else:
        picked_model = yaml_model or ""

    # Explicit provider resolution — env wins when it's a registered slug
    # (after alias mapping), otherwise fall back to YAML.
    #
    # YAML aliasing: the molecule-runtime wheel (config.py) auto-derives
    # ``runtime_config.provider`` from the YAML/default model slug — the
    # default model ``anthropic:claude-opus-4-7`` yields ``anthropic`` as
    # the inferred provider. Without applying the alias map here, that
    # auto-derived ``anthropic`` slug fails registry lookup and the
    # adapter raises ValueError ("provider='anthropic' but it is not in
    # the providers registry"), wedging the workspace at boot. The alias
    # map already handles this for the env-var path above; mirror the
    # same treatment for the YAML path so the runtime-wheel default
    # produces a registered provider name in both cases. Caught
    # 2026-05-09 on staging-cplead-2 — every workspace booted with
    # ``configuration_status=not_configured`` because the YAML provider
    # ``anthropic`` was passed through verbatim instead of being aliased
    # to ``anthropic-api``.
    if env_provider_is_slug:
        explicit_provider = env_provider_resolved
    elif yaml_provider:
        yp_lower = yaml_provider.lower()
        explicit_provider = _PROVIDER_SLUG_ALIASES.get(yp_lower, yaml_provider)
    else:
        explicit_provider = None

    return picked_model, explicit_provider


def _strip_provider_prefix(model: str) -> str:
    """Strip a known "<provider>:<model>" prefix from a model id.

    The molecule-runtime wheel's config.py defaults model to
    "anthropic:claude-opus-4-7" so runtime consumers get a uniform
    provider:model string out of the box. The claude CLI's
    --model arg expects the bare model id and silently exits 1 (no stderr)
    on prefixed strings — root cause of the 2026-05-01 claude-code adapter
    "Agent error (Exception)" bug.

    The strip also feeds _resolve_provider correctly: with the prefix
    intact, "anthropic:claude-opus-4-7" doesn't match the anthropic-api
    provider's model_prefixes=("claude-",) and falls back to the OAuth
    default — wrong for users on ANTHROPIC_API_KEY. Stripping makes both
    routing and CLI invocation see the same id.

    Only known-Claude prefixes are stripped. Unknown prefixes (e.g.
    "openai:gpt-4") pass through so the CLI fails loudly instead of being
    silently mangled into a model name it half-recognizes.
    """
    if not model:
        return model
    for prefix in ("anthropic:", "claude:"):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


# Vendor-specific env names that are SAFE to copy into ANTHROPIC_AUTH_TOKEN
# at boot. Limited to per-vendor names so a stray ANTHROPIC_API_KEY (which
# the SDK reads on its own path) is never misrouted into the AUTH_TOKEN
# slot. Keep in sync with the canvas-side env name suggestions.
_VENDOR_KEY_NAMES = frozenset({
    "MINIMAX_API_KEY",
    "GLM_API_KEY",
    "KIMI_API_KEY",
    "DEEPSEEK_API_KEY",
})


# ===========================================================================
# ADAPTER-OWNED MCP-CONFIG + PERSONA SEAM (ADR-004 §Decision-1)
# ===========================================================================
# Per ADR-004 (SDK owns the adapter socket + registry; the shared engine holds
# ZERO per-runtime dispatch) the claude-code per-runtime SHAPE — the native MCP
# path, the JSON `mcpServers` renderer, its inverse reader, the present-probe,
# and the persona materializer — lives HERE, in the adapter, not in the shared
# engine's `_RUNTIME_SPECS` / `_RUNTIME_READERS` / `_RUNTIME_PERSONA` dispatch
# tables. This block is a FAITHFUL, byte-identical copy of the engine's
# claude_code renderers/readers/materializer (mcp_render.render_claude_settings /
# _claude_path / _json_settings_has / _read_json_mcp_servers and
# persona_render.materialize_claude_persona / _claude_persona_path). The output
# MUST stay byte-for-byte identical to the engine's so onboarding — which works
# TODAY through the engine dispatch — keeps producing the same native config; the
# engine-migration phase (deleting the duplication from mcp_render/persona_render)
# depends on this equality. Do NOT "improve" the format here.

# The settings.json map key under which claude-code reads its MCP servers.
# Mirrors mcp_render.MCPSERVERS_KEY (the cross-repo delivery-contract `key`).
_MCPSERVERS_KEY = "mcpServers"

# Claude Code's native identity file (the system-prompt fallback file its
# create_executor reads). Mirrors persona_render.CLAUDE_PERSONA_FILE.
_CLAUDE_PERSONA_FILE = "system-prompt.md"


def _claude_native_mcp_path(config_path: "str | os.PathLike") -> Path:
    """Absolute native MCP-config file claude-code reads `mcpServers` from.

    ``<config_path>/.claude/settings.json``. Faithful copy of
    ``mcp_render._claude_path`` — claude-code (unlike codex/openclaw/hermes)
    resolves this from ``config.config_path``, NOT ``$HOME``.
    """
    return Path(config_path) / ".claude" / "settings.json"


def _render_claude_settings(settings_path: Path, name: str, spec: dict) -> None:
    """Additively merge ``name -> spec`` into the claude ``settings.json``
    ``mcpServers`` map. Idempotent; preserves every other key + server.

    Byte-identical to ``mcp_render.render_claude_settings``:
    ``json.dumps(data, indent=2) + "\\n"``. Additive (never evicts another
    server or a hand-written key) and idempotent (re-rendering the same
    descriptor rewrites identical bytes).
    """
    settings_path = Path(settings_path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text())
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
    else:
        data = {}

    servers = data.get(_MCPSERVERS_KEY)
    if not isinstance(servers, dict):
        servers = {}
    servers[name] = dict(spec)
    data[_MCPSERVERS_KEY] = servers

    settings_path.write_text(json.dumps(data, indent=2) + "\n")


def _claude_settings_has(settings_path: Path, name: str) -> bool:
    """True when the claude ``settings.json`` declares ``mcpServers.<name>``.

    Fail-closed by construction: a missing/unreadable/malformed/structurally-
    unexpected config yields False. Faithful copy of
    ``mcp_render._json_settings_has``.
    """
    try:
        data = json.loads(Path(settings_path).read_text())
    except (OSError, ValueError):
        return False
    servers = data.get(_MCPSERVERS_KEY) if isinstance(data, dict) else None
    return isinstance(servers, dict) and name in servers


def _read_claude_mcp_servers(settings_path: Path) -> dict:
    """Read the ``mcpServers`` map from the claude JSON settings file.

    Returns ``{name: spec}`` for every dict-valued entry (the inverse of the
    renderer); fail-closed ``{}`` on a missing/unreadable/malformed/structurally-
    unexpected file. Faithful copy of ``mcp_render._read_json_mcp_servers``.
    """
    try:
        data = json.loads(Path(settings_path).read_text())
    except (OSError, ValueError):
        return {}
    servers = data.get(_MCPSERVERS_KEY) if isinstance(data, dict) else None
    return {k: v for k, v in servers.items() if isinstance(v, dict)} if isinstance(servers, dict) else {}


def _materialize_claude_persona(config_path: Path, persona: str) -> Path:
    """Write ``persona`` to ``<config_path>/system-prompt.md`` (trailing newline).

    Claude-code's system-prompt fallback file. The executor prefers the
    base-assembled ``config.system_prompt``, so this is a no-regression native
    mirror. Faithful copy of ``persona_render.materialize_claude_persona`` +
    ``persona_render._write_persona_file`` (parents created, trailing newline
    appended only when absent).
    """
    target = Path(config_path) / _CLAUDE_PERSONA_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    body = persona if persona.endswith("\n") else persona + "\n"
    target.write_text(body, encoding="utf-8")
    return target


def _project_vendor_auth(provider: dict) -> None:
    """Project a per-vendor API key onto the provider's auth-token env at boot.

    Third-party Anthropic-compat providers (MiniMax, Z.ai, DeepSeek)
    reuse the Anthropic SDK's wire format with a Bearer token, which the
    ``claude`` CLI / claude-code-sdk reads from ``ANTHROPIC_AUTH_TOKEN``.
    Kimi For Coding's gateway instead authenticates with the
    ``x-api-key`` header (per kimi.com's official Claude Code
    integration doc), which the SDK emits from ``ANTHROPIC_API_KEY`` —
    so the projection target is per-provider, declared as
    ``auth_token_env`` in the registry (default ``ANTHROPIC_AUTH_TOKEN``
    preserves the existing MiniMax/GLM/DeepSeek behavior unchanged).

    Pre-#244 the canvas surfaced the vendor-specific name
    (``MINIMAX_API_KEY``, etc.) to the user — so a user who saved only
    that name hit a silent 401 on first call while the boot audit said
    ``MINIMAX_API_KEY=set``. Mirrors the hermes-side fix from task #249
    / hermes PR #38.

    Behavior:
      * Let ``target`` = the provider's ``auth_token_env`` (default
        ``ANTHROPIC_AUTH_TOKEN``).
      * If the matched provider's ``auth_env`` lists any of
        ``_VENDOR_KEY_NAMES`` and that var is set, copy its value into
        ``target`` so the SDK finds it.
      * **Idempotent**: if ``target`` is already set we do NOT
        overwrite — an explicit operator value (workspace secret)
        always wins over auto-projection.
      * Logs the projection by NAME (e.g. ``KIMI_API_KEY ->
        ANTHROPIC_API_KEY``); never logs the secret VALUE. Same
        contract as ``_audit_auth_env_presence``.
      * No-op for providers whose ``auth_env`` doesn't reference a
        vendor-specific name (oauth, anthropic-api, or a third-party
        entry that hasn't been added to the registry yet).
    """
    auth_env = provider.get("auth_env") or ()
    target = provider.get("auth_token_env") or "ANTHROPIC_AUTH_TOKEN"
    if os.environ.get(target):
        # Operator override wins — never clobber an explicit value.
        return
    for name in auth_env:
        if name not in _VENDOR_KEY_NAMES:
            continue
        value = os.environ.get(name)
        if not value:
            continue
        os.environ[target] = value
        logger.info(
            "auth env projection: %s -> %s (provider=%s)",
            name, target, provider.get("name", "<unknown>"),
        )
        return


def _resolve_provider(
    model: str,
    providers: tuple,
    explicit_provider: str = None,
) -> dict:
    """Return the provider entry matching this model id.

    Selection is flag-free: the ``platform`` arm (CP proxy, metered billing)
    is chosen exactly like every other provider — by the resolved provider
    (``explicit_provider``/``LLM_PROVIDER``/model→provider), NOT by a
    ``MOLECULE_LLM_BILLING_MODE`` env. ``provider==platform`` is the single
    signal that routes through the proxy (part of the org-wide
    ``llm_billing_mode`` removal; core injects ``LLM_PROVIDER=platform`` for
    platform-routed workspaces).

    If ``explicit_provider`` is given (set via the ``provider:`` field in
    workspace config.yaml or runtime_config), look up by name first. If the
    named provider is not in the registry, RAISE ``ValueError`` with an
    actionable message — silent fallback to ``providers[0]`` is the bug
    that motivated #180 (workspace operator picks ``provider: minimax``
    in the canvas Config tab, the adapter ignores it, the Claude SDK
    silently keeps using ``CLAUDE_CODE_OAUTH_TOKEN`` and the operator has
    no way to tell from the canvas that their provider switch did
    nothing).

    Without an explicit name: match is case-insensitive, prefix wins over
    alias when both could apply, and unknown ids fall back to the first
    provider in the registry (by convention, the OAuth/safest default —
    ``anthropic-oauth`` in both _BUILTIN_PROVIDERS and the shipped
    config.yaml).

    Pre-condition: ``providers`` is non-empty. _load_providers always
    returns at least one entry (built-ins when YAML is missing or every
    parsed entry was rejected).
    """
    if not providers:
        raise ValueError(
            "_resolve_provider called with empty providers tuple; "
            "_load_providers must always return at least one entry "
            "(falling back to _BUILTIN_PROVIDERS when needed)"
        )

    # Explicit provider name takes precedence — fail fast if it's not in
    # the registry. Anything else would silently route the operator's
    # picked provider through the wrong auth/base_url path. The error
    # message tells them exactly which two paths fix it.
    if explicit_provider:
        ep_lower = explicit_provider.lower()
        for provider in providers:
            if provider["name"].lower() == ep_lower:
                return provider
        names = ", ".join(p["name"] for p in providers)
        raise ValueError(
            f"claude-code adapter: workspace config picks "
            f"provider='{explicit_provider}' but it is not in the "
            f"providers registry.\n"
            f"\n"
            f"Known providers: {names}\n"
            f"\n"
            f"Two ways to fix:\n"
            f"  (a) Add '{explicit_provider}' to /configs/config.yaml as a "
            f"providers: entry. Required keys:\n"
            f"        providers:\n"
            f"          - name: {explicit_provider}\n"
            f"            auth_mode: third_party_anthropic_compat\n"
            f"            base_url: https://...   # provider's Anthropic-compat endpoint\n"
            f"            auth_env: [{explicit_provider.upper()}_API_KEY]\n"
            f"            model_prefixes: [...]\n"
            f"  (b) Switch to an official runtime whose provider registry "
            f"supports {explicit_provider} and routes it "
            f"without an Anthropic-compat shim.\n"
            f"\n"
            f"Note: claude-code SDK speaks the Anthropic API protocol. "
            f"Providers that only expose OpenAI-compatible endpoints "
            f"(MiniMax, GLM, Kimi, DeepSeek native APIs) need either an "
            f"Anthropic-compat proxy in front, or option (b)."
        )

    if not model:
        return providers[0]
    m = model.lower()
    for provider in providers:
        for prefix in provider["model_prefixes"]:
            if prefix and m.startswith(prefix):
                return provider
    for provider in providers:
        if m in provider["model_aliases"]:
            return provider
    return providers[0]


class ClaudeCodeAdapter(BaseAdapter):

    @staticmethod
    def name() -> str:
        return "claude-code"

    @staticmethod
    def display_name() -> str:
        return "Claude Code"

    @staticmethod
    def description() -> str:
        return "Claude Code CLI — full agentic coding with hooks, CLAUDE.md, auto-memory, and MCP support"

    @staticmethod
    def get_config_schema() -> dict:
        return {
            "model": {"type": "string", "description": "Claude model (e.g. sonnet, opus, haiku)", "default": "sonnet"},
            "required_env": {"type": "array", "description": "Required env vars", "default": ["CLAUDE_CODE_OAUTH_TOKEN"]},
            "timeout": {"type": "integer", "description": "Timeout in seconds (0 = no timeout)", "default": 0},
        }

    def capabilities(self) -> RuntimeCapabilities:
        """Claude-code SDK owns session lifecycle natively — see project
        memory `project_runtime_native_pluggable.md`.

        provides_native_session=True
            The claude-agent-sdk maintains a long-lived streaming session
            with its own ClaudeSDKClient state. The platform's a2a_queue
            would double-buffer the same in-flight state — declaring
            native_session lets the platform skip enqueueing and dispatch
            directly. Validates capability primitive #5 once that
            consumer lands.

        Other capabilities stay False (platform fallback owns them):
        - provides_native_heartbeat: the SDK's session events don't map
          cleanly to our 30s heartbeat cadence; heartbeat.py keeps
          emitting WORKSPACE_HEARTBEAT so the canvas idle indicator and
          a2a_proxy idle-timer reset behavior keep working.
        - provides_native_scheduler: claude-code has no built-in cron;
          platform scheduler keeps owning it.
        - provides_native_status_mgmt: claude-code wedge detection IS
          adapter-driven (claude_sdk_executor sets is_wedged + heartbeat
          forwards as runtime_state="wedged"), but the rest of the
          status state machine (online/degraded recovery via error_rate)
          stays platform-owned. Reconsider once the SDK exposes its own
          ready-signal hook.
        - provides_native_retry / activity_decoration / channel_dispatch:
          not implemented in the SDK surface — platform fallback applies.
        """
        return RuntimeCapabilities(
            provides_native_session=True,
        )

    def idle_timeout_override(self) -> int:
        """Claude-code synthesis on Opus + multi-step tool use legitimately
        runs 8-10 min between broadcaster events. The pre-capability
        bug PR #2128 patched at the env-var layer hit this exact issue:
        `context canceled` mid-flight when the platform's 5min idle
        timer fired during a long packaging step. Override to 15 min
        to cover the long tail without leaving genuinely-wedged runs
        hanging too long.

        Capability primitive #2 — see workspace/adapter_base.py:
        idle_timeout_override and PR #2139 for the platform-side
        consumer in a2a_proxy.dispatchA2A.
        """
        return 900  # 15 minutes

    # ------------------------------------------------------------------
    # MCP-config seam (ADR-004 §3) — claude-code OWNS its per-runtime shape.
    # These override the BaseAdapter dispatch defaults so the adapter renders /
    # reads / present-probes / enumerates against its OWN native config
    # (.claude/settings.json) WITHOUT reaching into the shared engine's
    # per-runtime dispatch tables. Byte-identical output to the engine's
    # claude_code renderers (see the module-level `_render_claude_settings`
    # etc.) so onboarding stays byte-stable through the engine-migration phase.
    # ------------------------------------------------------------------
    def mcp_settings_path(self, config: "AdapterConfig") -> str:
        """Absolute native MCP-config file claude-code reads `mcpServers` from
        (``<config_path>/.claude/settings.json``). Always absolute; never another
        runtime's file."""
        return str(_claude_native_mcp_path(config.config_path))

    def register_mcp_server_hook(
        self, config: "AdapterConfig", name: str, spec: dict
    ) -> None:
        """Wire ``name -> spec`` into claude-code's native ``.claude/settings.json``
        ``mcpServers`` map (the MCP-wiring PORT).

        Additive + idempotent (never evicts another server or a hand-written key;
        re-rendering the same descriptor rewrites identical bytes), and writes
        ONLY the file claude-code reads (the #3159 guard). Enriches the privileged
        management-MCP spec via ``inject_privileged_env`` first — no-op for
        non-management names, idempotent, descriptor-wins — matching the base
        funnel so a direct caller (the self-heal path) is enriched too.
        """
        from molecule_runtime.privileged_mcp_env import inject_privileged_env

        spec = inject_privileged_env(name, spec)
        target = _claude_native_mcp_path(config.config_path)
        _render_claude_settings(target, name, spec)
        logger.info(
            "register_mcp_server_hook: wired MCP %r into %s (runtime=%s)",
            name, target, self.name(),
        )

    def management_mcp_present(self, config: "AdapterConfig") -> bool:
        """True when the privileged management MCP (``molecule-platform``) is
        declared in claude-code's ``.claude/settings.json``.

        The runtime-agnostic answer to the RCA#2970 online gate's "is the
        management MCP wired?" question, judged against the file claude-code
        actually reads. Fail-CLOSED: a missing/unreadable/malformed/structurally-
        unexpected config yields False."""
        from molecule_runtime.platform_agent_identity import MANAGEMENT_MCP_NAME

        return _claude_settings_has(
            _claude_native_mcp_path(config.config_path), MANAGEMENT_MCP_NAME
        )

    async def enumerate_loaded_mcp_tools(
        self, config: "AdapterConfig"
    ) -> "list[str] | None":
        """Enumerate the LOADED MCP tool ids claude-code actually has, or None.

        Reads claude-code's OWN native config (``.claude/settings.json``
        ``mcpServers``) via the adapter's reader, then hands the resolved
        ``{name: spec}`` map to the shared boot-safe stdio probe engine
        (``loaded_mcp_tools_probe.enumerate_from_specs_async``) — the runtime
        agnostic engine that STAYS in the shared runtime. This is the same
        adapter-owns-discovery pattern hermes uses, so the engine's per-runtime
        reader switch is never consulted for claude-code.

        TRI-STATE (identical to the loaded_mcp_tools producer contract):
          * ``None``  — nothing observed (no servers declared, or every probe
            failed/stalled/unreadable). Heartbeat omits the field → grace window.
          * ``[]``    — a server genuinely connected and advertised zero tools.
          * ``[ids]`` — deduped/sorted union of ``mcp__<server>__<tool>`` ids.

        BOOT-SAFE + NEVER-RAISES: ``enumerate_from_specs_async`` bounds the whole
        probe by the enumeration deadline and maps every failure to ``None``.
        """
        from molecule_runtime.loaded_mcp_tools_probe import enumerate_from_specs_async

        servers = _read_claude_mcp_servers(_claude_native_mcp_path(config.config_path))
        return await enumerate_from_specs_async(servers)

    # ------------------------------------------------------------------
    # Persona seam (ADR-004 §4) — claude-code OWNS its native identity file.
    # ------------------------------------------------------------------
    def materialize_persona(self, config: "AdapterConfig") -> "Path | None":
        """Materialize the workspace's CANONICAL PERSONA into claude-code's native
        identity file (``<config_path>/system-prompt.md``).

        Reads the persona runtime-agnostically from ``config.prompt_files`` via
        the shared ``persona_render.read_canonical_persona`` generic helper (the
        one runtime-name-free helper the engine keeps), then writes it into
        claude-code's own convention. Best-effort: returns ``None`` (no-op) when
        no persona is delivered, so claude-code's baked default is never clobbered
        with an empty identity. Returns the path written otherwise."""
        from molecule_runtime import persona_render

        persona = persona_render.read_canonical_persona(
            config.config_path, config.prompt_files
        )
        if not (persona or "").strip():
            logger.info(
                "materialize_persona: no canonical persona delivered for runtime "
                "%s — leaving the runtime's native default untouched",
                self.name(),
            )
            return None
        target = _materialize_claude_persona(Path(config.config_path), persona)
        logger.info(
            "materialize_persona: wrote %s persona (%d chars) to %s",
            self.name(), len(persona), target,
        )
        return target

    async def setup(self, config: AdapterConfig) -> None:
        """Install plugins via the per-runtime adaptor registry.

        The legacy claude-code-specific ``inject_plugins()`` override is gone:
        each plugin now ships (or has registered in the platform registry) a
        per-runtime adaptor, and ``BaseAdapter.install_plugins_via_registry``
        routes installs through it. The Claude Code SDK reads ``CLAUDE.md``
        natively; ``/configs/skills/`` (where the default
        :class:`AgentskillsAdaptor` writes plugin skills) reaches Claude Code
        via the ``~/.claude/skills`` symlink created by entrypoint.sh
        (``link_plugin_skills_into_claude_home`` — Claude Code only scans
        its own personal-skills dir, NOT /configs/skills directly).
        """
        # Load provider registry from /configs/config.yaml — canvas reads
        # the same YAML for its Config-tab Provider dropdown so adapter +
        # UI never disagree on what's available. Adding a new provider is
        # a one-line YAML edit (no code change in this file or entrypoint.sh).
        providers = _load_providers(config.config_path)

        # Resolve the picked model to a provider entry, then drive auth-env
        # validation + ANTHROPIC_BASE_URL routing from that single decision.
        rc = config.runtime_config
        if isinstance(rc, dict):
            yaml_model = rc.get("model") or ""
            yaml_provider_name = rc.get("provider") or ""
        else:
            yaml_model = getattr(rc, "model", None) or ""
            yaml_provider_name = getattr(rc, "provider", None) or ""

        # Also honor the top-level `provider:` field in /configs/config.yaml.
        # The canvas Config-tab Provider dropdown writes there (not into
        # runtime_config) on some legacy paths. Either source is canonical;
        # whichever is set wins. Root cause of #180: the adapter used to
        # ignore both, silently routing every non-Anthropic provider pick
        # through anthropic-oauth.
        if not yaml_provider_name:
            yaml_path = os.path.join(config.config_path, "config.yaml")
            try:
                import yaml  # transitive dep via molecules-workspace-runtime
                with open(yaml_path, "r") as f:
                    data = yaml.safe_load(f) or {}
                if isinstance(data, dict):
                    val = data.get("provider")
                    if isinstance(val, str) and val.strip():
                        yaml_provider_name = val.strip()
            except FileNotFoundError:
                pass
            except Exception as exc:  # noqa: BLE001 — defensive: never block boot
                logger.warning(
                    "providers: failed to read top-level provider: from %s (%s); "
                    "falling back to model-based resolution",
                    yaml_path, exc,
                )

        # Reconcile env vars (persona convention: MODEL=<id>,
        # MODEL_PROVIDER=<slug>) against YAML. Env wins over YAML — the
        # persona env files are the canonical per-agent provider mapping
        # (Phase 2 mapping 2026-05-08), and the workspace-runtime wheel's
        # legacy ``MODEL_PROVIDER``-as-model-id reading would otherwise
        # silently route non-leads to providers[0] = anthropic-oauth.
        # Documented in detail at _resolve_model_and_provider_from_env.
        picked_model, explicit_provider_name = _resolve_model_and_provider_from_env(
            yaml_model=yaml_model,
            yaml_provider=yaml_provider_name,
            providers=providers,
        )
        if not picked_model:
            picked_model = "sonnet"

        # SSOT signal — TOP PRECEDENCE. ``MOLECULE_RESOLVED_PROVIDER`` is the
        # single provider value core's workspace provisioner publishes after
        # resolving the provider ONCE (Go ``manifest.DeriveProvider``). When it
        # is set it overrides every other source here — the env
        # MODEL_PROVIDER/MODEL convention, the YAML/runtime_config ``provider:``
        # field, and model-prefix derivation — so claude-code selects exactly the
        # registry arm core resolved (``platform`` for the metered proxy, a byok
        # arm such as ``anthropic-api`` otherwise). It carries the registry arm
        # name verbatim, so it flows straight into ``explicit_provider`` and is
        # validated by ``_resolve_provider`` (which raises an actionable
        # ValueError if the name is not in the registry, same as #180). The
        # adapter falls back to the resolution above ONLY when the SSOT signal is
        # absent (back-compat for provisioners that predate it).
        resolved_provider = (os.environ.get("MOLECULE_RESOLVED_PROVIDER") or "").strip()
        if resolved_provider:
            explicit_provider_name = resolved_provider

        # NOTE: do NOT strip the provider prefix here. The pre-fix routing
        # behavior — `anthropic:claude-opus-4-7` falls through to
        # providers[0] (anthropic-oauth) when no model_prefixes match — is
        # actually correct for OAuth users (the realistic case for the
        # wheel default). Stripping in setup() routes OAuth users into
        # `anthropic-api` provider and the CLI then hangs at `initialize`
        # because ANTHROPIC_API_KEY isn't set. The strip belongs only at
        # the CLI invocation site (create_executor below).
        #
        # Pass the explicit provider name through so _resolve_provider
        # raises ValueError with an actionable message (instead of silently
        # routing to providers[0]) when an operator picks a provider that
        # isn't in the registry. See #180.
        provider = _resolve_provider(
            picked_model, providers,
            explicit_provider=explicit_provider_name,
        )
        auth_env_options = provider["auth_env"]

        # Project the per-vendor API key (MINIMAX_API_KEY, GLM_API_KEY,
        # KIMI_API_KEY, DEEPSEEK_API_KEY) onto ANTHROPIC_AUTH_TOKEN so the
        # claude-code-sdk finds the bearer token. Idempotent: explicit
        # ANTHROPIC_AUTH_TOKEN (operator override) is never clobbered.
        # Must run BEFORE the auth audit + auth check below so the audit
        # reflects the post-projection state and the check sees the right
        # value. Task #244; mirrors hermes PR #38 (task #249).
        _project_vendor_auth(provider)

        # Endpoint precedence: operator-set ANTHROPIC_BASE_URL wins (escape
        # hatch for custom regional endpoints — e.g. token-plan-sgp.* for
        # Xiaomi MiMo, api.minimaxi.com for MiniMax China). Otherwise the
        # provider's default base_url is auto-applied so the operator
        # picking a provider in the platform UI doesn't *also* have to
        # paste a URL. Anthropic-native paths (oauth, anthropic_api) leave
        # base_url=None and let the CLI's built-in default take effect.
        explicit_base_url = os.environ.get("ANTHROPIC_BASE_URL")
        if explicit_base_url:
            effective_base_url = explicit_base_url
            base_url_source = "operator-override"
        elif provider["base_url"]:
            os.environ["ANTHROPIC_BASE_URL"] = provider["base_url"]
            effective_base_url = provider["base_url"]
            base_url_source = f"provider={provider['name']}"
        else:
            effective_base_url = None
            base_url_source = "anthropic-default"

        # Boot banner — operators reading workspace logs see which provider
        # was selected, where the URL came from, and which auth env var
        # the adapter expects. Cheap diagnostic; cuts root-cause-finding
        # time when an LLM call fails downstream.
        base_url_host = ""
        if effective_base_url:
            try:
                base_url_host = urlparse(effective_base_url).netloc or "<unparseable>"
            except Exception:
                base_url_host = "<unparseable>"
        logger.info(
            "Claude Code adapter starting: model=%s provider=%s auth_mode=%s "
            "base_url=%s (%s) auth_env=%s",
            picked_model, provider["name"], provider["auth_mode"],
            base_url_host or "anthropic-default", base_url_source,
            "/".join(auth_env_options),
        )

        # Audit which auth-relevant env vars are actually present (NAMES
        # ONLY — never values). Boot-time visibility into "is the key
        # missing or wrong" was the #1 ask after the 2026-05-02
        # crash-loop incident: docker logs showed "missing X" with no
        # hint about which vendor envs WERE set, so an operator with
        # MINIMAX_API_KEY couldn't tell at a glance whether the
        # ANTHROPIC_AUTH_TOKEN gap was the cause. This one-line audit
        # closes that gap. See _audit_auth_env_presence above.
        _audit_auth_env_presence()

        # internal#702: also report the harness model-tier env so an
        # operator can see at a glance whether CP injected the small-fast +
        # alias models (which it does for non-anthropic platform_managed
        # workspaces). All-`unset` on a non-anthropic provider is the
        # fingerprint of the haiku-leak-to-Anthropic bug. These vars are
        # forwarded to the spawned `claude` process via plain os.environ
        # inheritance (see _HARNESS_MODEL_ENV passthrough contract); the
        # adapter does not resolve or rewrite them.
        _audit_harness_model_env()

        # Auth check — any of the provider's accepted env vars satisfies.
        # Warning (not raise) so a workspace can still boot for non-LLM
        # work (terminal, file editing) while the operator sets the key.
        if not any(os.environ.get(v) for v in auth_env_options):
            logger.warning(
                "None of %s set for model=%s (provider=%s) — the adapter "
                "will fail on the first LLM call with AuthenticationError. "
                "Set one of these env vars in workspace secrets.",
                "/".join(auth_env_options), picked_model, provider["name"],
            )

        # Third-party providers must end up with a base_url one way or
        # another (provider default OR operator override). If neither, the
        # CLI silently hits api.anthropic.com with a non-Anthropic key and
        # every call 401s — workspace looks "online" but is structurally
        # broken. Symmetric with create_executor's pre-validate raise on
        # the inverse misconfig. The provider registry guarantees a default
        # for every third-party we ship, so this fires only if a future
        # provider entry forgets to set base_url.
        if (provider["auth_mode"] == _AUTH_MODE_THIRD_PARTY
                and not effective_base_url):
            raise ValueError(
                f"claude-code adapter: model={picked_model} resolved to "
                f"third-party provider={provider['name']} but no "
                "ANTHROPIC_BASE_URL is configured (provider has no default "
                "and operator didn't set one). Add base_url to the provider "
                "entry in adapter.py or set ANTHROPIC_BASE_URL via secrets."
            )

        from molecule_runtime.plugins import load_plugins
        workspace_plugins_dir = os.path.join(config.config_path, "plugins")
        plugins = load_plugins(
            workspace_plugins_dir=workspace_plugins_dir,
            shared_plugins_dir=os.environ.get("PLUGINS_DIR", "/plugins"),
        )
        await self.install_plugins_via_registry(config, plugins)

        # Capture plugin fragments for create_executor() so the executor's
        # per-turn hot-reload rebuild threads the SAME plugin_rules/plugin_prompts
        # through build_system_prompt that setup() used (#185).
        self._plugin_rules = getattr(plugins, "rules", None)
        self._plugin_prompts = list(getattr(plugins, "prompt_fragments", []) or [])

        # --- SSOT: publish the single base-built system prompt onto config ---
        # config.system_prompt is BASE-OWNED and None until something fills it.
        # Build it HERE via the one canonical builder (``build_system_prompt``),
        # which honors ``config.prompt_files`` (with the legacy
        # ``system-prompt.md`` fallback baked in). This is the authoritative
        # boot value the executor receives + its hot-reload fallback; the
        # executor re-derives through the SAME builder per turn so a delivered
        # prompt still takes effect without a restart. Plugin rules/prompts
        # loaded above are folded in to match the base ``_common_setup`` shape.
        from molecule_runtime.prompt import build_system_prompt
        config.system_prompt = build_system_prompt(
            config.config_path,
            config.workspace_id,
            [],  # skills: /configs/skills reaches claude-code via the
            #     ~/.claude/skills symlink (entrypoint.sh)
            [],  # peers: discovered live via the a2a MCP, not baked
            prompt_files=config.prompt_files,
            plugin_rules=self._plugin_rules,
            plugin_prompts=self._plugin_prompts,
        )

    async def create_executor(self, config: AdapterConfig) -> AgentExecutor:
        from claude_sdk_executor import ClaudeSDKExecutor

        # The base-published prompt (setup() → build_system_prompt, honoring
        # prompt_files) is the authoritative SSOT value + the executor's
        # hot-reload fallback. No per-runtime system-prompt.md re-read here —
        # that re-read ignored prompt_files (the concierge-identity drift).
        system_prompt = config.system_prompt

        # runtime_config may arrive as a dict (from main.py vars(...)) or as a
        # RuntimeConfig dataclass. Read `model` defensively from either shape.
        rc = config.runtime_config
        if isinstance(rc, dict):
            yaml_model = rc.get("model") or ""
            yaml_provider = rc.get("provider") or ""
        else:
            yaml_model = getattr(rc, "model", None) or ""
            yaml_provider = getattr(rc, "provider", None) or ""

        # Reconcile against env vars (persona convention: MODEL=<id>,
        # MODEL_PROVIDER=<slug>) using the same helper that ``setup`` uses,
        # so the executor and the boot banner agree on the picked model.
        # Without this, a workspace whose env says ``MODEL=MiniMax-M2.7``
        # but whose runtime wheel pre-dates the persona-env fix would set
        # runtime_config.model="minimax" (the slug, mistakenly read by the
        # legacy ``MODEL_PROVIDER``-as-model-id path); this helper restores
        # the correct model id before it reaches the SDK.
        providers = _load_providers(config.config_path)
        explicit_model, _ = _resolve_model_and_provider_from_env(
            yaml_model=yaml_model,
            yaml_provider=yaml_provider,
            providers=providers,
        )
        explicit_model = _strip_provider_prefix(explicit_model)

        # Pre-validation: detect the misconfiguration combo that drove the
        # 2026-04-30 staging incident — ANTHROPIC_BASE_URL pointed at a
        # non-Anthropic upstream (MiniMax / OpenAI shim) but no explicit
        # model was set, so we'd silently fall back to "sonnet" and the
        # upstream would hang on the SDK --print probe for 30s before
        # timing out. The platform's phantom-busy sweep then resets the
        # workspace at the 10min mark — the user-visible failure is "every
        # workspace dead" but the root cause is one missing env var.
        #
        # Fail fast here with an actionable message so the operator sees
        # exactly what to fix instead of chasing ghosts in workspace logs.
        # We only fire when ALL three are true:
        #   1. ANTHROPIC_BASE_URL is set (custom upstream is in play)
        #   2. The host is NOT api.anthropic.com (real Anthropic accepts
        #      "sonnet" as a known alias, so the fallback is fine there)
        #   3. The user did NOT set an explicit model (the check we want)
        # Anthropic-native users with no model picked still get the
        # "sonnet" fallback — that's correct behavior, no error.
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
        if base_url and not explicit_model:
            host = urlparse(base_url).hostname or ""
            if host and host != "api.anthropic.com":
                raise ValueError(
                    "claude-code adapter: ANTHROPIC_BASE_URL points at a "
                    f"non-Anthropic host ({host}) but no model is configured. "
                    "The default fallback ('sonnet') is an Anthropic-native "
                    "alias; non-Anthropic shims (MiniMax, OpenAI gateways, "
                    "etc.) won't recognize it and the SDK --print probe will "
                    "hang for 30s before timing out. Fix: set MODEL "
                    "as a workspace secret (canvas: Save+Restart with model "
                    "picked) or set runtime_config.model in /configs/config.yaml."
                )

        model = explicit_model or "sonnet"
        # Surface what we resolved to in logs — when the workspace agent
        # eventually fails, this single line in the logs explains "the
        # adapter sent X to Y" without having to dig into the SDK
        # subprocess. Cheap diagnostic, no runtime cost.
        if base_url:
            logger.info(
                "claude-code: model=%s base_url_host=%s (custom upstream)",
                model,
                urlparse(base_url).hostname or "<unparseable>",
            )
        else:
            logger.info("claude-code: model=%s base_url=anthropic-default", model)

        return ClaudeSDKExecutor(
            system_prompt=system_prompt,
            config_path=config.config_path,
            heartbeat=config.heartbeat,
            model=model,
            # Thread prompt_files + workspace_id so the executor's per-turn
            # hot-reload re-derives through the SAME single builder
            # (build_system_prompt) that produced config.system_prompt —
            # honoring prompt_files instead of re-reading only system-prompt.md.
            prompt_files=config.prompt_files,
            workspace_id=config.workspace_id,
            # Thread plugin_rules/plugin_prompts for the same reason: without
            # them the hot-reload path would silently drop plugin fragments
            # (task #76 / #185).
            plugin_rules=getattr(self, "_plugin_rules", None),
            plugin_prompts=list(getattr(self, "_plugin_prompts", []) or []),
        )

    async def transcript_lines(self, since: int = 0, limit: int = 100) -> dict:
        """Read the live Claude Code session transcript.

        Claude Code writes every session to
        ``$HOME/.claude/projects/<cwd-as-dirname>/<session-uuid>.jsonl`` —
        every line is a JSON event (user/assistant/tool_use/attachment/etc).
        We pick the most-recently-modified .jsonl in the projects dir for
        the agent's working directory, then return ``[since:since+limit]``.

        Returns ``supported: True`` even if no .jsonl exists yet (empty
        ``lines`` + ``cursor=0``) so the canvas can show "agent hasn't
        produced output yet" instead of "feature unavailable".
        """
        limit = max(1, min(limit, _TRANSCRIPT_MAX_LIMIT))
        since = max(0, since)

        # Resolve the projects-dir name. Claude Code maps cwd → dirname by
        # replacing "/" with "-" (so "/configs" → "-configs"). The exact
        # rule lives inside the CLI binary, but the leading-dash + path-
        # without-trailing-slash pattern is stable across versions.
        #
        # Match ClaudeSDKExecutor._resolve_cwd: prefer /workspace if populated,
        # else /configs. Override via CLAUDE_PROJECT_CWD for tests.
        WORKSPACE_MOUNT = "/workspace"
        CONFIG_MOUNT = "/configs"
        cwd_override = os.environ.get("CLAUDE_PROJECT_CWD")
        if cwd_override:
            cwd = cwd_override
        elif os.path.isdir(WORKSPACE_MOUNT) and os.listdir(WORKSPACE_MOUNT):
            cwd = WORKSPACE_MOUNT
        else:
            cwd = CONFIG_MOUNT

        # Normalize: strip trailing slash, replace path separators with "-"
        cwd_norm = cwd.rstrip("/") or "/"
        projdir_name = cwd_norm.replace("/", "-")  # "/configs" → "-configs"

        home = Path(os.environ.get("HOME", "/home/agent"))
        projdir = home / ".claude" / "projects" / projdir_name
        result_base = {
            "runtime": self.name(),
            "supported": True,
            "lines": [],
            "cursor": since,
            "more": False,
            "source": str(projdir),
        }

        if not projdir.is_dir():
            return result_base

        # Pick most-recently-modified .jsonl
        candidates = sorted(projdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            return result_base
        target = candidates[0]
        result_base["source"] = str(target)

        lines = []
        more = False
        try:
            with target.open("r") as f:
                for i, raw in enumerate(f):
                    if i < since:
                        continue
                    if len(lines) >= limit:
                        more = True
                        break
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        lines.append(json.loads(raw))
                    except json.JSONDecodeError:
                        # Skip malformed lines but keep cursor advancing
                        lines.append({"_parse_error": True, "_raw": raw[:200]})
        except OSError as exc:
            logger.warning("transcript_lines: read failed for %s: %s", target, exc)
            return result_base

        result_base["lines"] = lines
        result_base["cursor"] = since + len(lines)
        result_base["more"] = more
        return result_base


Adapter = ClaudeCodeAdapter
