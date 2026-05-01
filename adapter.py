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

# Auth-mode classification for a selected model id. The Claude Code CLI
# accepts three auth paths and the right env var differs per path; warning
# at boot about the wrong var (the pre-multi-provider behavior) misled
# operators who picked an API-key or third-party model. New third-party
# providers add a prefix → mode entry below + a model-prefix → base-URL
# mapping in entrypoint.sh until the data-driven `runtime_env` schema
# field lands platform-side.
_AUTH_MODE_OAUTH = "oauth"
_AUTH_MODE_ANTHROPIC_API = "anthropic_api"
_AUTH_MODE_THIRD_PARTY = "third_party_anthropic_compat"

_THIRD_PARTY_PREFIXES = ("mimo-",)
_OAUTH_ALIASES = frozenset({"sonnet", "opus", "haiku"})


def _detect_auth_mode(model: str) -> str:
    """Classify the picked model into one of three auth paths.

    Used by setup() to validate the right env var is set so operators see
    the misconfiguration at boot instead of on the first LLM call.
    Unknown ids default to OAuth — the historical default and the safest
    fallback for the warning path.
    """
    if not model:
        return _AUTH_MODE_OAUTH
    m = model.lower()
    if any(m.startswith(p) for p in _THIRD_PARTY_PREFIXES):
        return _AUTH_MODE_THIRD_PARTY
    if m.startswith("claude-"):
        return _AUTH_MODE_ANTHROPIC_API
    if m in _OAUTH_ALIASES:
        return _AUTH_MODE_OAUTH
    return _AUTH_MODE_OAUTH


def _required_env_for_mode(mode: str) -> str:
    """The env var the claude CLI needs to authenticate for a given mode."""
    if mode == _AUTH_MODE_OAUTH:
        return "CLAUDE_CODE_OAUTH_TOKEN"
    return "ANTHROPIC_API_KEY"


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
        # KI-001 fix, generalized for the three auth paths the CLI supports:
        # OAuth (CLAUDE_CODE_OAUTH_TOKEN), Anthropic API (ANTHROPIC_API_KEY),
        # and third-party Anthropic-API-compat (ANTHROPIC_API_KEY + provider
        # ANTHROPIC_BASE_URL). Detect the path from the picked model so the
        # warning targets the *right* env var — the pre-multi-provider code
        # always warned about CLAUDE_CODE_OAUTH_TOKEN even when the user had
        # legitimately picked an API-key model and set ANTHROPIC_API_KEY.
        rc = config.runtime_config
        if isinstance(rc, dict):
            picked_model = rc.get("model") or "sonnet"
        else:
            picked_model = getattr(rc, "model", None) or "sonnet"
        auth_mode = _detect_auth_mode(picked_model)
        required_var = _required_env_for_mode(auth_mode)

        # Single-line startup banner — operators reading boot logs can see
        # which provider path was selected and whether ANTHROPIC_BASE_URL
        # (set by entrypoint.sh for third-party mimo-*) took effect. URL is
        # logged as host-only; defensive against credential-shaped query
        # strings even though base_url shouldn't carry one.
        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        base_url_host = ""
        if base_url:
            try:
                base_url_host = urlparse(base_url).netloc or "<unparseable>"
            except Exception:
                base_url_host = "<unparseable>"
        logger.info(
            "Claude Code adapter starting: model=%s auth_mode=%s required_env=%s%s",
            picked_model, auth_mode, required_var,
            f" base_url_host={base_url_host}" if base_url_host else "",
        )

        if not os.environ.get(required_var):
            logger.warning(
                "%s is not set for model=%s (auth_mode=%s) — the adapter will fail "
                "on the first LLM call with an AuthenticationError. Set the env "
                "var or configure the key in your platform workspace settings.",
                required_var, picked_model, auth_mode,
            )

        # Third-party paths additionally need ANTHROPIC_BASE_URL; entrypoint.sh
        # sets it for known mimo-* prefixes. Fail fast on the missing-base-URL
        # combo — the symptom otherwise is the CLI silently hitting
        # api.anthropic.com with a non-Anthropic key, every LLM call 401s, and
        # the workspace looks "online" while being structurally broken.
        # Symmetric with create_executor's pre-validate raise on the inverse
        # combo (URL set, no model picked) — both unrecoverable misconfigs
        # that would put the workspace into a "boots but never works" state.
        if auth_mode == _AUTH_MODE_THIRD_PARTY and not base_url:
            raise ValueError(
                f"claude-code adapter: model={picked_model} is a third-party "
                "Anthropic-compat model but ANTHROPIC_BASE_URL is unset. "
                "Without it, requests land on api.anthropic.com with a "
                "non-Anthropic key and 401 every call. Fix: check "
                "entrypoint.sh's model→base-URL mapping for this model "
                "prefix, or set ANTHROPIC_BASE_URL as a workspace secret."
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
