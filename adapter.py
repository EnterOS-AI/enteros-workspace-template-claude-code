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


def _load_providers(config_path: str) -> tuple:
    """Load the provider registry from /configs/config.yaml.

    The YAML's top-level ``providers:`` list is the canonical source —
    canvas Config tab reads the same list to populate its Provider
    dropdown so the UI and the adapter never disagree on what's
    available. Falls back to ``_BUILTIN_PROVIDERS`` (oauth + anthropic-api)
    if the file is missing, malformed, or has no providers section, so a
    bare-bones workspace still boots with the historical defaults.

    Per-entry isolation: a single bad provider entry is dropped with a
    warning; the rest of the registry survives. Used to be a generator
    inside tuple(...) that propagated any AttributeError out and reverted
    the whole registry to builtins — exactly the silent-fallback failure
    mode this file's existence was meant to fix.
    """
    yaml_path = os.path.join(config_path, "config.yaml")
    try:
        import yaml  # transitive dep via molecule-ai-workspace-runtime
        with open(yaml_path, "r") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.info("providers: %s not found, using builtin defaults", yaml_path)
        return _BUILTIN_PROVIDERS
    except Exception as exc:  # noqa: BLE001 — defensive: never block boot on YAML
        logger.warning("providers: failed to load from %s (%s); using builtins", yaml_path, exc)
        return _BUILTIN_PROVIDERS

    raw = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
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
        logger.warning("providers: no valid entries in %s; using builtins", yaml_path)
        return _BUILTIN_PROVIDERS
    return tuple(parsed)


def _resolve_provider(model: str, providers: tuple) -> dict:
    """Return the provider entry matching this model id.

    Match is case-insensitive: prefix wins over alias when both could
    apply. Unknown ids fall back to the first provider in the registry
    (by convention, the OAuth/safest default — anthropic-oauth in both
    _BUILTIN_PROVIDERS and the shipped config.yaml).

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
            picked_model = rc.get("model") or "sonnet"
        else:
            picked_model = getattr(rc, "model", None) or "sonnet"
        provider = _resolve_provider(picked_model, providers)
        auth_env_options = provider["auth_env"]

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
            explicit_model = rc.get("model") or ""
        else:
            explicit_model = getattr(rc, "model", None) or ""

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
                    "hang for 30s before timing out. Fix: set MODEL_PROVIDER "
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
