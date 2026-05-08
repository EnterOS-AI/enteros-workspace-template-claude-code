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
    }


# Canonical install path the platform provisioner is contracted to clone
# the template repo into. Hardcoded so the adapter's config.yaml lookup
# is invariant across Docker (mounted /app→/opt/adapter) and EC2-host
# (cloned by molecule-controlplane's ec2.go) install paths — robust
# against the site-packages copy that bit us 2026-05-04 11:08Z.
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
      • 2026-05-04 11:08Z: that ``__file__`` lookup misses on EC2-host
        installs because the provisioner copies adapter.py to
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
      1. ``/opt/adapter/config.yaml`` — canonical provisioner-managed
         install dir. Hardcoded because the platform contract is
         "provisioner clones template repo into /opt/adapter"; this
         is invariant across Docker (mounted /app→/opt/adapter) and
         EC2-host (cloned by ec2.go) install paths. Robust against
         site-packages copy.
      2. Adjacent to ``adapter.__file__`` — works in dev/test where
         the canonical path doesn't exist. Also covers the Docker
         image's /app/config.yaml (bundled by Dockerfile #6).
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
        import yaml  # transitive dep via molecule-ai-workspace-runtime
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

    The legacy ``workspace/config.py`` (in molecule-ai-workspace-runtime)
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
    if env_provider_is_slug:
        explicit_provider = env_provider_resolved
    else:
        explicit_provider = yaml_provider or None

    return picked_model, explicit_provider


def _strip_provider_prefix(model: str) -> str:
    """Strip LangChain-style "<provider>:<model>" prefix from a model id.

    The molecule-runtime wheel's config.py defaults model to
    "anthropic:claude-opus-4-7" so langchain/crewai consumers get a uniform
    LangChain-style provider:model string out of the box. The claude CLI's
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


def _project_vendor_auth(provider: dict) -> None:
    """Project a per-vendor API key onto ANTHROPIC_AUTH_TOKEN at boot.

    Third-party Anthropic-compat providers (MiniMax, Z.ai, Moonshot,
    DeepSeek) all reuse the Anthropic SDK's wire format, which means the
    ``claude`` CLI / claude-code-sdk reads the bearer token from
    ``ANTHROPIC_AUTH_TOKEN`` no matter which vendor is being talked to.
    Pre-#244 the canvas surfaced the vendor-specific name
    (``MINIMAX_API_KEY``, etc.) to the user — so a user who saved only
    that name hit a silent 401 on first call while the boot audit said
    ``MINIMAX_API_KEY=set``. Mirrors the hermes-side fix from task #249
    / hermes PR #38.

    Behavior:
      * If the matched provider's ``auth_env`` lists any of
        ``_VENDOR_KEY_NAMES`` and that var is set, copy its value into
        ``ANTHROPIC_AUTH_TOKEN`` so the SDK finds it.
      * **Idempotent**: if ``ANTHROPIC_AUTH_TOKEN`` is already set we
        do NOT overwrite — an explicit operator value (workspace
        secret) always wins over auto-projection.
      * Logs the projection by NAME (e.g. ``MINIMAX_API_KEY ->
        ANTHROPIC_AUTH_TOKEN``); never logs the secret VALUE. Same
        contract as ``_audit_auth_env_presence``.
      * No-op for providers whose ``auth_env`` doesn't reference a
        vendor-specific name (oauth, anthropic-api, or a third-party
        entry that hasn't been added to the registry yet).
    """
    auth_env = provider.get("auth_env") or ()
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        # Operator override wins — never clobber an explicit value.
        return
    for name in auth_env:
        if name not in _VENDOR_KEY_NAMES:
            continue
        value = os.environ.get(name)
        if not value:
            continue
        os.environ["ANTHROPIC_AUTH_TOKEN"] = value
        logger.info(
            "auth env projection: %s -> ANTHROPIC_AUTH_TOKEN (provider=%s)",
            name, provider.get("name", "<unknown>"),
        )
        return


def _resolve_provider(
    model: str,
    providers: tuple,
    explicit_provider: str = None,
) -> dict:
    """Return the provider entry matching this model id.

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
            f"  (b) Switch the workspace runtime template to one that "
            f"natively supports {explicit_provider} (CrewAI, LangGraph, or "
            f"DeepAgents read provider/model from runtime_config and route "
            f"directly without needing an Anthropic-compat shim).\n"
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

    async def setup(self, config: AdapterConfig) -> None:
        """Install plugins via the per-runtime adaptor registry.

        The legacy claude-code-specific ``inject_plugins()`` override is gone:
        each plugin now ships (or has registered in the platform registry) a
        per-runtime adaptor, and ``BaseAdapter.install_plugins_via_registry``
        routes installs through it. The Claude Code SDK still reads
        ``CLAUDE.md`` and ``/configs/skills/`` natively, and the default
        :class:`AgentskillsAdaptor` writes to both.
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
                import yaml  # transitive dep via molecule-ai-workspace-runtime
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

    async def create_executor(self, config: AdapterConfig) -> AgentExecutor:
        from claude_sdk_executor import ClaudeSDKExecutor

        # Load system prompt if exists
        system_prompt = config.system_prompt
        if not system_prompt:
            prompt_file = os.path.join(config.config_path, "system-prompt.md")
            if os.path.exists(prompt_file):
                with open(prompt_file) as f:
                    system_prompt = f.read()

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
