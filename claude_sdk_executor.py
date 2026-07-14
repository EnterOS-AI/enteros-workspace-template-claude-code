"""SDK-based agent executor for Claude Code runtime.

Uses the official `claude-agent-sdk` Python package to invoke the Claude Code
engine programmatically — no subprocess, no stdout parsing, no zombie reap.

Replaces CLIAgentExecutor for the `claude-code` runtime only. Other CLI runtimes
(codex, ollama) keep using `cli_executor.py`.

Benefits over CLI subprocess:
- No per-message ~500ms startup overhead
- No stdout buffering issues
- Native Python session management (no JSON parsing of stdout)
- Real message stream — can surface tool calls in future for live UX
- Cooperative cancel (closes the query async generator on cancel())
- Same Claude Code engine, so plugins / skills / CLAUDE.md still apply

Concurrency model
-----------------
Turns are serialized per-executor via an asyncio.Lock. The old CLI executor
serialized implicitly by spawning one subprocess per message and awaiting it;
the SDK removes that, so we re-introduce serialization explicitly. This keeps
session_id updates race-free and makes cancel() well-defined (there's at most
one active stream at any given moment).
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import shutil
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

import claude_agent_sdk as sdk

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.helpers import new_text_message

from molecule_runtime.executor_helpers import (
    CONFIG_MOUNT,
    MEMORY_CONTENT_MAX_CHARS,
    WORKSPACE_MOUNT,
    auto_push_hook,
    brief_summary,
    collect_outbound_files,
    commit_memory,
    error_detail_for_external,
    extract_attached_files,
    extract_message_text,
    get_display_instructions,
    get_mcp_server_path,
    read_delegation_results,
    recall_memories,
    sanitize_agent_error,
    set_current_task,
)

try:
    from molecule_runtime.platform_agent_identity import set_loaded_mcp_tools
except Exception:  # pragma: no cover - shim for stub test harnesses that fake
    # `molecule_runtime` as an empty module. In production the real runtime
    # always provides this; the no-op fallback keeps loaded_mcp_tools capture
    # from being a hard import dependency of the executor under test stubs.
    def set_loaded_mcp_tools(_tools):  # type: ignore[misc]
        return None

if TYPE_CHECKING:
    from molecule_runtime.heartbeat import HeartbeatLoop

logger = logging.getLogger(__name__)

_NO_TEXT_MSG = "Error: message contained no text content."
_NO_RESPONSE_MSG = "(no response generated)"

# SSOT for the platform-management MCP tool the concierge actually needs.
# The canonical source is workspace-server/internal/handlers/mcp_plugin_delivery_contract.go
# (RequiredTool), which reads from the same cross-repo contract JSON we ship here.
def _load_platform_mcp_required_tool() -> str:
    """Return the required management-MCP tool verb from the mirrored contract.

    Tries an importable Python SSOT first (future-proofing), then falls back to
    the contract JSON bundled with this runtime template. A hard-coded last
    resort of ``provision_workspace`` keeps the executor booting if the contract
    file is missing, but any drift from the real SSOT will be caught by the
    cross-repo drift gate.
    """
    try:
        # Future path: molecule_runtime may expose the contract as Python constants.
        from molecule_runtime.mcp_plugin_delivery_contract import REQUIRED_TOOL

        if isinstance(REQUIRED_TOOL, str) and REQUIRED_TOOL:
            return REQUIRED_TOOL
    except Exception:
        pass
    contract_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "contracts",
        "mcp-plugin-delivery.contract.json",
    )
    try:
        with open(contract_path, encoding="utf-8") as f:
            contract = json.load(f)
        tool = contract.get("required_tool")
        if isinstance(tool, str) and tool:
            return tool
    except Exception:
        logger.warning(
            "Could not load platform MCP required tool from %s; falling back "
            "to hard-coded default",
            contract_path,
            exc_info=True,
        )
    return "provision_workspace"


_PLATFORM_MCP_REQUIRED_TOOL = _load_platform_mcp_required_tool()


def _tool_names_from_mcp_server_status(server: dict) -> set[str]:
    """Normalize the optional ``tools`` field on an SDK McpServerStatus dict.

    The real SDK returns a list of ``McpToolInfo`` objects (each with a ``name``
    attr); the unit-test stub returns a list of plain strings. Accept all three
    shapes: objects with ``.name``, dicts with a ``name`` key, and plain strings.
    """
    tools = server.get("tools") or []
    names: set[str] = set()
    for tool in tools:
        if isinstance(tool, dict):
            name = tool.get("name")
        else:
            name = getattr(tool, "name", tool)
        if isinstance(name, str):
            names.add(name)
    return names


def _apply_extra_mcp_servers(mcp_servers: dict, config: dict) -> dict:
    """Merge config-declared MCP servers into the base ``mcp_servers`` dict.

    The org-level platform agent declares a second MCP server (the platform-
    management MCP) in its ``config.yaml`` under ``mcp_servers:`` so it can drive
    the org alongside the always-on ``a2a`` server. Each entry is
    ``{name, command, args?, env?}``. Ordinary workspaces declare none, so this
    is a no-op for them.

    Defensive: entries that are not dicts, are missing ``name``/``command``, or
    try to redefine the built-in ``a2a`` server are skipped — a malformed config
    can neither crash the executor nor shadow the A2A mesh.

    (RFC: molecule-core docs/design/rfc-platform-agent.md)
    """
    for entry in config.get("mcp_servers") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        command = entry.get("command")
        if not name or not command or name == "a2a":
            continue
        server: dict = {"command": command, "args": entry.get("args") or []}
        if entry.get("env"):
            server["env"] = entry["env"]
        mcp_servers[name] = server
    return mcp_servers


def _apply_settings_mcp_servers(mcp_servers: dict, settings: dict) -> dict:
    """Merge plugin-delivered MCP servers from a Claude-Code ``settings.json``
    ``mcpServers`` MAP into the base ``mcp_servers`` dict.

    The platform-management MCP can also arrive as a PLUGIN (RFC#3045,
    rfc-platform-mcp-as-plugin): the MCPServerAdaptor merges the plugin's
    server into ``/configs/.claude/settings.json`` under ``mcpServers`` — the
    Claude Code NATIVE shape, a ``name -> {command, args?, env?}`` MAP (distinct
    from config.yaml's ``mcp_servers`` LIST). The executor launches the CLI with
    ``--strict-mcp-config``, so the CLI ignores on-disk settings files entirely
    and honors ONLY the servers passed in the SDK options here. Without folding
    this map in, a plugin-delivered server (e.g. ``molecule-platform`` →
    ``provision_workspace``) is installed and written to settings.json but NEVER
    loaded by the agent. (core#3079)

    Same defensive posture as ``_apply_extra_mcp_servers``: non-dict specs,
    entries missing ``command``, and any attempt to redefine the built-in
    ``a2a`` server are skipped. Callers apply this AFTER the config.yaml + YAML
    fragment merges, so a plugin server wins on a same-name entry.
    """
    servers = settings.get("mcpServers")
    if not isinstance(servers, dict):
        return mcp_servers
    for name, spec in servers.items():
        if not name or name == "a2a" or not isinstance(spec, dict):
            continue
        command = spec.get("command")
        if not command:
            continue
        server: dict = {"command": command, "args": spec.get("args") or []}
        if spec.get("env"):
            server["env"] = spec["env"]
        mcp_servers[name] = server
    return mcp_servers
_MAX_RETRIES = 3
_BASE_RETRY_DELAY_S = 5
# Cap for stderr captured from the CLI subprocess in the executor log. Keeps
# log lines bounded while still surfacing enough context to diagnose crashes.
# Fixes #66 (previously the executor logged nothing beyond the generic
# "Check stderr output for details" message).
_PROCESS_ERROR_STDERR_MAX_CHARS = 4096

# Substrings in error messages that indicate a transient failure worth retrying.
_RETRYABLE_PATTERNS = (
    "rate",
    "limit",
    "429",
    "overloaded",
    "capacity",
    "exit code 1",
    "try again",
)

# ---------------------------------------------------------------------------
# Completion-stream idle cap (the platform-agent wedge fix)
# ---------------------------------------------------------------------------
# A hung LLM completion has no built-in deadline: the SDK's `query()` /
# `receive_response()` async iterators simply never yield the next message
# when the upstream stalls. Because `_execute_locked` holds the per-turn
# run-lock for the entire stream, ONE stalled completion blocks the lock
# forever — every subsequent inbound A2A message queues behind it, 524s, and
# strands, while the workspace heartbeat still reports "online" (the wedge is
# invisible).
#
# Match the guard in the shared native executor
# (molecule_runtime.a2a_executor, gated on A2A_COMPLETION_IDLE_TIMEOUT_SECONDS):
# cap the idle gap BETWEEN streamed messages — NOT the whole turn — so a long
# but legitimately-progressing tool turn is never killed, while a stream that
# goes silent for the cap is aborted with a TimeoutError. Same env knob and
# default (900s) as the native executor so operators tune one value across
# both runtimes.
_COMPLETION_IDLE_CAP_S = float(os.environ.get("A2A_COMPLETION_IDLE_TIMEOUT_SECONDS", "900"))
# Hard bound on the best-effort teardown of a stalled stream. A hung aclose()
# does NOT raise — try/except catches errors, not hangs — so without this cap the
# idle-cap cleanup below could block the recovery path indefinitely. Kept small
# by design (10–30s): just long enough for a healthy stream to close.
_ACLOSE_TIMEOUT_S = 15.0


async def _aiter_with_idle_cap(stream: Any) -> AsyncIterator[Any]:
    """Yield items from an async message stream, enforcing an idle cap
    BETWEEN items.

    Mirrors the native a2a_executor guard — its
    ``await asyncio.wait_for(_aiter.__anext__(), _idle_cap)`` loop. The
    timeout is reset on every yielded message, so it bounds the gap between
    messages, not the total turn duration: a long tool turn that keeps
    emitting events is never killed, but a completion that goes silent for
    ``_COMPLETION_IDLE_CAP_S`` seconds closes the underlying stream (so the
    stalled CLI subprocess coroutine sees the cancellation and cleans up) and
    raises a builtin ``TimeoutError`` carrying the "completion stalled" marker
    the executor's wedge-detection matches on.
    """
    aiter = stream.__aiter__()
    while True:
        try:
            item = await asyncio.wait_for(aiter.__anext__(), _COMPLETION_IDLE_CAP_S)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                # Bound the teardown explicitly. A hung aclose() never raises
                # (except catches errors, not hangs), so an unbounded await here
                # could block the recovery path below forever. wait_for makes the
                # cleanup best-effort AND time-bounded, so the terminal raise
                # (-> _mark_sdk_wedged -> slot freed -> queue drains) is always
                # reachable even when aclose hangs or errors.
                try:
                    await asyncio.wait_for(aclose(), timeout=_ACLOSE_TIMEOUT_S)
                except Exception:  # noqa: BLE001 - best-effort, bounded teardown
                    logger.debug(
                        "idle-cap stream.aclose() hung or errored (bounded at %ss, ignored)",
                        _ACLOSE_TIMEOUT_S,
                        exc_info=True,
                    )
            raise TimeoutError(
                f"completion stalled: no SDK message for {_COMPLETION_IDLE_CAP_S}s"
            )
        yield item


# Module-level SDK-wedge flag. When claude_agent_sdk's `query.initialize()`
# raises `Control request timeout: initialize`, the SDK's internal client-
# process state is corrupted for the rest of the Python process — every
# subsequent `_run_query()` call hits the same wedge and re-throws. The
# executor itself can't auto-recover (the underlying CLI subprocess and
# its read pipe are in an unrecoverable state); only a workspace restart
# clears it.
#
# Two consumers read these helpers:
#   1. Heartbeat (via molecule_runtime.runtime_wedge — see _mark_sdk_wedged
#      below). Reports `runtime_state="wedged"` to the platform, which
#      flips the workspace to `degraded` so the canvas surfaces a Restart
#      hint instead of leaving the user staring at a green dot while
#      every chat hangs.
#   2. Boot smoke (molecule-core task #131). When the publish-image
#      workflow boots the image with MOLECULE_SMOKE_MODE=1,
#      run_executor_smoke consults runtime_wedge.is_wedged() at the end
#      of every result path and upgrades a provisional PASS to FAIL when
#      the flag is set. Catches PR-25-class regressions (malformed CLI
#      argv → SDK init wedge) BEFORE the broken image ships to Gitea OCI.
#
# Module scope (not instance scope) is deliberate: the wedge is a
# property of the Python process, not the executor. A future per-org
# multi-executor design could move this to a shared registry, but with
# one executor per workspace process today the simplest lock-free
# read+write fits.
_sdk_wedged_reason: str | None = None


def is_wedged() -> bool:
    """True if the Claude SDK has hit a non-recoverable init wedge in
    this process. Sticky until process restart."""
    return _sdk_wedged_reason is not None


def wedge_reason() -> str:
    """Human-readable description of the wedge cause, or empty string
    when not wedged. Surfaced to the canvas via heartbeat sample_error."""
    return _sdk_wedged_reason or ""


def _mark_sdk_wedged(reason: str) -> None:
    """Internal — flag the SDK as wedged. Only the first call wins
    (subsequent identical wedges shouldn't overwrite a more specific
    reason). Tests use `_reset_sdk_wedge_for_test()` to clear.

    Mirrors the flag into molecule_runtime.runtime_wedge — that's the
    universal cross-cutting wedge holder that heartbeat.py reads (to
    flip the workspace to `degraded`) and that smoke_mode reads (to
    fail the publish-image gate on init wedges, task #131). Without
    this mirror the local sticky flag is unobserved by both consumers.
    Best-effort: a missing/older runtime that doesn't ship runtime_wedge
    silently no-ops the mirror — the local flag still gates
    is_wedged() inside this module so internal callers (retry loop,
    cancel handler) keep working.
    """
    global _sdk_wedged_reason
    if _sdk_wedged_reason is None:
        _sdk_wedged_reason = reason
        logger.error("SDK wedge detected: %s — workspace will report degraded until a successful query clears it", reason)
        # Catch is narrowed to import errors: a SIGNATURE drift
        # (mark_wedged renamed/removed) must surface so the smoke gate
        # + heartbeat aren't silently blind. The runtime's structural
        # snapshot test (molecule-core task #169) catches the rename
        # at PR-time. Older runtimes that don't ship runtime_wedge at
        # all hit ImportError here and silently no-op the mirror —
        # the local sticky flag still gates is_wedged() inside this
        # module so internal callers (retry loop, cancel handler)
        # keep working.
        try:
            from molecule_runtime.runtime_wedge import mark_wedged as _mark_runtime_wedged
        except (ImportError, ModuleNotFoundError):
            return
        try:
            _mark_runtime_wedged(reason)
        except Exception:
            # Mirror call (not import) is still best-effort — a
            # runtime_wedge internal raise must not silently suppress
            # the local wedge state. Logged loudly so the regression
            # is at least visible in the executor log.
            logger.exception("runtime_wedge.mark_wedged mirror failed — local SDK wedge flag is still set")


def _clear_sdk_wedge_on_success() -> None:
    """Auto-recovery — called from _run_query after a successful
    completion. The original wedge could be transient (a single network
    blip during the SDK's first-message handshake), and a sticky-only
    flag would lock the workspace into degraded forever even after the
    SDK started working again. Clearing on observed success means the
    next heartbeat after a working query reports `runtime_state` empty
    and the platform flips status back to online.

    Symmetric with _mark_sdk_wedged: also clears the universal
    runtime_wedge flag so heartbeat + smoke_mode see the same state.

    No-op when not wedged (the common case)."""
    global _sdk_wedged_reason
    if _sdk_wedged_reason is not None:
        logger.info("SDK wedge cleared after successful query — workspace will recover to online on next heartbeat")
        _sdk_wedged_reason = None
        # Same import-narrowing rationale as _mark_sdk_wedged above.
        try:
            from molecule_runtime.runtime_wedge import clear_wedge as _clear_runtime_wedge
        except (ImportError, ModuleNotFoundError):
            return
        try:
            _clear_runtime_wedge()
        except Exception:
            logger.exception("runtime_wedge.clear_wedge mirror failed — local clear succeeded")


def _reset_sdk_wedge_for_test() -> None:
    """Test-only escape hatch. Production code clears the wedge via
    `_clear_sdk_wedge_on_success` when a query succeeds; this helper
    is for unit tests that need to reset between cases."""
    global _sdk_wedged_reason
    _sdk_wedged_reason = None


# Per-tool-use summarizers. Reads the most-useful argument from each
# tool's input dict so the canvas progress feed shows
# `🛠 Read /tmp/foo` instead of the bare tool name. Anything not in the
# table falls through to a generic "🛠 <tool>(…)" line. Order keys by
# tool frequency so a future contributor can see the high-traffic
# tools first.
_TOOL_USE_SUMMARIZERS: dict[str, Callable[[dict], str]] = {
    "Read":  lambda i: f"📄 Read {i.get('file_path', '?')}",
    "Write": lambda i: f"✍️  Write {i.get('file_path', '?')}",
    "Edit":  lambda i: f"✏️  Edit {i.get('file_path', '?')}",
    "Bash":  lambda i: f"⚡ Bash: {(i.get('command') or '')[:80]}",
    "Glob":  lambda i: f"🔍 Glob {i.get('pattern', '?')}",
    "Grep":  lambda i: f"🔍 Grep {i.get('pattern', '?')}",
    "WebFetch": lambda i: f"🌐 WebFetch {i.get('url', '?')}",
    "WebSearch": lambda i: f"🌐 WebSearch {i.get('query', '?')}",
    "Task":  lambda i: f"🤖 Task: {(i.get('description') or '')[:60]}",
    "TodoWrite": lambda _i: "📝 TodoWrite",
}


def _summarize_tool_use(tool_name: str, tool_input: dict) -> str:
    summarizer = _TOOL_USE_SUMMARIZERS.get(tool_name)
    if summarizer:
        try:
            return summarizer(tool_input or {})[:200]
        except Exception:
            pass
    # Generic fallback. Truncated so a tool with a giant input dict
    # doesn't write a 10kB activity row per call.
    return f"🛠 {tool_name}(…)"[:200]


async def _report_tool_use(block: Any) -> None:
    """Fire-and-forget agent_log activity row per tool the SDK invoked,
    so the canvas's MyChat live-progress feed can render each step
    Claude is doing instead of staring at a single spinner.

    Posts directly to /workspaces/:id/activity rather than through
    a2a_tools.report_activity — that helper also pushes a current_task
    heartbeat which would duplicate as a TASK_UPDATED line in the
    chat feed. The workspace card's current_task is already set
    once per turn by the executor's set_current_task(brief_summary)
    call, so the per-tool telemetry stays a chat-only signal.

    Best-effort — any failure (network blip, platform unreachable, the
    block didn't have the attrs we expected) is swallowed silently.
    The tool will still execute regardless; only the progress
    telemetry is lost. Deliberately does NOT raise — a malformed
    block must not abort the message-stream iteration in
    `_run_query`.
    """
    try:
        # Lazy imports to keep this helper non-essential — the
        # executor must still run when the workspace's network/auth
        # plumbing isn't fully set up (e.g. unit tests).
        import httpx
        from molecule_runtime.a2a_client import PLATFORM_URL, WORKSPACE_ID
        from molecule_runtime.platform_auth import auth_headers
    except Exception:
        return
    try:
        tool_name = getattr(block, "name", "") or ""
        tool_input = getattr(block, "input", {}) or {}
        if not tool_name:
            return
        summary = _summarize_tool_use(tool_name, tool_input)
        # 5s budget — long enough to absorb a single platform GC
        # pause, short enough that a wedged platform doesn't slow
        # the tool-iteration cadence beyond noticeable.
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{PLATFORM_URL}/workspaces/{WORKSPACE_ID}/activity",
                json={
                    "activity_type": "agent_log",
                    "source_id": WORKSPACE_ID,
                    # target_id == source for self-actions. Matches the
                    # convention other self-logged activity rows use
                    # (a2a_receive when the workspace logs its own
                    # outbound reply) so DB consumers joining on
                    # target_id see a well-defined value.
                    "target_id": WORKSPACE_ID,
                    "summary": summary,
                    "status": "ok",
                    "method": tool_name,
                },
                headers=auth_headers(),
            )
    except Exception:
        # Telemetry failures must not break the conversation.
        return


# Substring patterns that classify an exception as the specific
# claude_agent_sdk init-timeout wedge (vs. a rate-limit, transient
# subprocess crash, etc.). Match is case-insensitive on the formatted
# error string. Adding a new pattern here MUST come with a test in
# tests/test_claude_sdk_executor.py — false-positives lock the
# workspace into degraded until the next successful query clears it.
#
# `:initialize` suffix-anchored — the SDK can theoretically time out
# on later control messages (in-flight tool callbacks), but those
# don't leave the SDK in the unrecoverable post-init state we're
# trying to detect. Limit the pattern to the specific wedge.
_WEDGE_ERROR_PATTERNS = (
    "control request timeout: initialize",
)


# Substrings that classify an error as a CONTEXT-WINDOW OVERFLOW — the
# accumulated session transcript grew past the model's context window, so
# the next request's input tokens alone exceed the limit and EVERY
# subsequent dispatch on the same (resumed) session re-overflows and fails.
# This is the Kimi wedge: claude-code routed at a 262144-token model
# reported `token limit 262144 requested 268132`; once the session crossed
# the window, every A2A turn 400'd identically and the agent was stuck.
#
# WHY claude-code's own auto-compact didn't save Kimi
# ---------------------------------------------------
# claude-code DOES auto-compact, but the compaction threshold is derived
# from the model's context window via the CLI's internal resolver (`B2`):
# it returns 1e6 for known long-context Anthropic models, a cached value
# for `claude-sonnet-4-6`, and otherwise falls through to a hard-coded
# `pi6 = 200000` default. A non-Anthropic model reached through the
# molecule LLM proxy (Kimi/MiniMax/GLM/DeepSeek) is NOT in that table, so
# the resolver returns the 200k fallback. Kimi's REAL window is 262144 —
# LARGER than the assumed 200k — so claude-code believes it has *more*
# headroom than it does only when the model is smaller, but the deeper
# failure is the inverse: the proxy advertises the model's true 262144
# window to claude-code's token accounting in some paths while the
# compaction trigger uses the 200k fallback, so the session is allowed to
# grow into a band (200k–262k) where claude-code thinks compaction already
# ran "enough" but the upstream still rejects. Net effect for the operator:
# auto-compact fired against the wrong number and the session wedged. The
# durable prevention is to tell claude-code the model's real window via
# `CLAUDE_CODE_MAX_CONTEXT_TOKENS` (see _maybe_set_context_window_env); the
# auto-heal below is the RECOVERY half that un-sticks an already-wedged
# agent.
#
# Matching mirrors claude-code's own overflow regex
#   \b(too long|too large|exceeds|token limit|prompt is too long)\b
# plus the proxy-shaped `token limit <N> requested <M>` body and the
# Anthropic-native phrasings, so we catch the error whether it surfaces as
# a raised ProcessError/Exception OR as an `is_error` ResultMessage.
# Case-insensitive substring match on the formatted error text.
#
# Adding a pattern here MUST come with a test in
# tests/test_context_overflow_autoheal.py — a false positive throws away a
# healthy session and forces a (recoverable but wasteful) re-summarization.
_CONTEXT_OVERFLOW_PATTERNS = (
    "prompt is too long",
    "token limit",            # proxy: "token limit 262144 requested 268132"
    "context window",
    "context_length_exceeded",
    "maximum context length",
    "exceeds the context",
    "input length and `max_tokens`",
    "too many tokens",
)


# Known context windows for proxy-routed (non-Anthropic) models that
# claude-code's own resolver doesn't know (it falls back to 200k for these).
# Keyed by a substring of the resolved model id (matched case-insensitively,
# longest key first so e.g. "kimi-k2.6" can override a generic "kimi"). Values
# are the model's REAL token window. Used ONLY as the last fallback in
# _maybe_set_context_window_env (env + config.yaml take precedence). Adding an
# entry requires confidence in the value — too high re-introduces the overflow
# wedge, too low wastes context. SSOT: centralize via config.yaml/provisioner
# (runtime#133). Confirmed: Kimi K2 = 262144 (the JRS incident model; reported
# `token limit 262144`).
_KNOWN_PROXY_MODEL_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("kimi", 262144),          # Moonshot Kimi K2 family — 256K
    ("moonshot", 262144),      # provider-prefixed form (moonshot/kimi-*)
)


def _known_model_context_window(model: str | None) -> int | None:
    """Best-effort context window for a proxy-routed model id, or None when
    unknown (leaving claude-code's default resolver in place — no regression
    for Anthropic models)."""
    low = (model or "").lower()
    if not low:
        return None
    # longest key first for specificity
    for key, window in sorted(_KNOWN_PROXY_MODEL_CONTEXT_WINDOWS, key=lambda kv: -len(kv[0])):
        if key in low:
            return window
    return None


def _is_context_overflow(text: str) -> bool:
    """True if `text` looks like a context-window overflow (vs. a generic
    rate-limit or subprocess crash). Case-insensitive substring match
    against `_CONTEXT_OVERFLOW_PATTERNS`.

    Deliberately NARROW: `_RETRYABLE_PATTERNS` already contains the broad
    word "limit", which would match almost any rate-limit string; this
    classifier exists to distinguish the *context* overflow (heal by
    resetting the session) from a *rate* limit (heal by backing off and
    retrying the SAME session). The two need opposite remedies — resetting
    the session on a rate-limit would needlessly discard good context.
    """
    low = (text or "").lower()
    return any(p in low for p in _CONTEXT_OVERFLOW_PATTERNS)


_SWALLOWED_STDERR_MARKER = "Check stderr output for details"


def _probe_claude_cli_error() -> str | None:
    """Run ``claude --print`` directly and capture its stderr + stdout.

    Used as a fallback when the claude-agent-sdk raises a bare ``Exception``
    with the swallowed "Check stderr output for details" placeholder — that
    happens when the SDK wraps a stream error from the CLI subprocess and
    loses both the ``.stderr`` attribute and the exit code. At that point
    the only way to see the real failure reason (rate limit, auth error,
    network outage, missing token) is to run the CLI ourselves.

    Bounded by a 30s timeout so a hung CLI can't stall the error path.
    Returns None if the probe itself failed (wrong invariant — don't
    corrupt the main error message with probe noise).
    """
    try:
        import subprocess
        # --print reads stdin, prints response, exits. Empty stdin gives the
        # CLI something to work with without triggering an actual model call
        # when it's going to fail anyway.
        proc = subprocess.run(
            ["claude", "--print"],
            input="probe",
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            # CLI succeeded — the original error was a transient state that
            # resolved between the SDK failure and our probe. Signal that.
            return "<cli probe succeeded — error was transient>"
        raw = (proc.stderr or "") + (proc.stdout or "")
        raw = raw.strip()
        if not raw:
            return f"<cli exited {proc.returncode} with empty output>"
        if len(raw) > _PROCESS_ERROR_STDERR_MAX_CHARS:
            raw = raw[:_PROCESS_ERROR_STDERR_MAX_CHARS] + "... [truncated]"
        return raw
    except Exception as probe_exc:  # pragma: no cover — best-effort diagnostic
        return f"<probe failed: {type(probe_exc).__name__}: {probe_exc}>"


def _format_process_error(exc: BaseException) -> str:
    """Render a Claude-SDK ProcessError (or any ClaudeSDKError) with its full
    captured context — exit code, stderr, exception type. Plain strings for
    non-SDK exceptions fall back to str(exc).

    Bounded at _PROCESS_ERROR_STDERR_MAX_CHARS so a runaway CLI can't spam
    the log. Used by the executor's error path (fixes #66 — the SDK's
    ProcessError carries `.stderr`/`.exit_code` attributes that the previous
    code silently discarded, leaving every CLI crash with an identical
    "Check stderr output for details" message in the workspace log).

    Fixes #160: when the SDK raises a bare ``Exception`` containing the
    "Check stderr output for details" placeholder (which happens when the
    CLI subprocess emits a stream error the SDK can't categorize — rate
    limit, auth, network), there's no ``.stderr``/``.exit_code`` to read.
    In that case we fall back to running the CLI ourselves via
    ``_probe_claude_cli_error`` so the operator sees the real failure
    reason (e.g. ``You've hit your limit · resets Apr 17``) instead of
    chasing ghosts in the workspace logs.
    """
    parts = [f"{type(exc).__name__}: {exc}"]
    exit_code = getattr(exc, "exit_code", None)
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    stderr = getattr(exc, "stderr", None)
    if stderr:
        trimmed = stderr[:_PROCESS_ERROR_STDERR_MAX_CHARS]
        if len(stderr) > _PROCESS_ERROR_STDERR_MAX_CHARS:
            trimmed += f"... [{len(stderr) - _PROCESS_ERROR_STDERR_MAX_CHARS} more chars truncated]"
        parts.append(f"stderr={trimmed!r}")
    elif exit_code is None and _SWALLOWED_STDERR_MARKER in str(exc):
        # #160: generic exception with the swallowed-stderr placeholder.
        # Probe the CLI directly — this is the only way to surface the real
        # error when the SDK lost it in translation.
        probed = _probe_claude_cli_error()
        if probed:
            parts.append(f"probed_cli_error={probed!r}")
    return " | ".join(parts)


# --- Stuck-MCP readiness gate (the concierge "lost its platform tools" bug) ---
#
# The org-level platform agent declares a second MCP server (`platform`, the
# molecule-mcp-server with ~88 org-admin tools like provision_workspace) in its
# config.yaml. That server is a `node` stdio subprocess that takes ~5-8s to
# finish its MCP handshake. The one-shot `claude_agent_sdk.query()` API spawns
# a COLD CLI subprocess per turn and ships its `init` system message — which
# carries the tool list the LLM will see — the instant the CLI boots, while the
# `node` server is still `status: pending`. Result: the 88 `mcp__platform__*`
# tools are absent from that turn's tool list, so the concierge "loses"
# provision_workspace etc. A fresh `query()` doesn't reliably fix it because each
# query is a cold subprocess that re-races the same handshake (confirmed live:
# 3/3 fresh `query()` sessions showed `platform: pending` / 0 platform tools at
# init, while a persistent client that POLLS `get_mcp_status()` settles to
# `connected` / 88 tools after ~5s).
#
# Fix: when the config declares extra (non-`a2a`) MCP servers, route the turn
# through a persistent `ClaudeSDKClient` and GATE on `get_mcp_status()` until
# every declared server reports `connected` AND exposes the SSOT-required tool
# (`_PLATFORM_MCP_REQUIRED_TOOL`) in its `tools` list BEFORE sending the prompt.
# Ordinary workspaces declare no extra servers and keep the fast `query()` path
# untouched. If the gate times out, or the server is connected but missing the
# required callable tool, we raise `_McpNotReadyError` so `_run_query_gated`
# can self-heal by reloading the MCP server via `client.reconnect_mcp_server()`
# (or fail-degraded if the reload doesn't help).
#
# Connection-status classification for the readiness gate.
#   `connected` + required tool present — the only success; the server's tools
#                 are actually live and callable.
#   `pending`   — still handshaking; keep polling within the budget.
#   `failed`    — the CLI's own MCP-connect timeout (MCP_TIMEOUT, default 30s)
#                 elapsed before the slow `node` server finished. Observed
#                 live to be INTERMITTENT under load (the same server connects
#                 fine on a fresh subprocess), so it is treated as RETRYABLE,
#                 not hard-terminal: the gate surfaces it as _McpNotReadyError
#                 which the executor heals by reloading the server with
#                 `reconnect_mcp_server()`.
#   `disabled` / `needs-auth` — genuinely terminal: a config/auth problem no
#                 reload can fix, so the gate raises immediately.
_MCP_READY_STATUS = "connected"
# Hard-terminal: retrying/reloading cannot help (misconfiguration / auth).
_MCP_TERMINAL_FAIL_STATUSES = ("disabled", "needs-auth")
# Bound on the readiness poll. The platform `node` MCP was observed to connect
# in ~6-13s; budget 50 × 0.5s = 25s gives ample headroom under load while
# staying under the SDK's 60s initialize timeout so the gate can't itself trip
# an init wedge.
_MCP_READY_MAX_POLLS = 50
_MCP_READY_POLL_INTERVAL_S = 0.5
# How many times the stuck-MCP heal may reload the server within a single
# `_run_query_gated` call. Each `reconnect_mcp_server()` gives the slow `node`
# MCP a fresh handshake attempt while keeping the same CLI subprocess, so a
# transient `failed`/timeout usually clears on the next load. Two reloads
# (three total attempts) makes an intermittent connect failure vanishingly
# unlikely to surface to the user, while staying bounded so a genuinely-broken
# server still terminates loudly / fails-degraded.
_MCP_HEAL_MAX_RETRIES = 2
# CLI MCP-connection timeout (ms) the executor pins for the platform agent so
# the slow `node` handshake isn't marked `failed` prematurely under load. The
# CLI default is 30000; 60000 doubles the headroom. Never clobbers an operator
# pin. (Preventive half of the stuck-MCP fix — the readiness gate is recovery.)
_MCP_CONNECT_TIMEOUT_MS = "60000"


class _McpNotReadyError(Exception):
    """Raised by the readiness-gated query path when a config-declared MCP
    server (e.g. `platform`) never reached `connected` within the bounded
    poll window, so its tools would be absent from the LLM's tool list.

    Routed through `_execute_locked`'s heal classifier (analogous to
    `_ResultError` for overflow): the first occurrence resets the session
    and retries ONCE; a second occurrence on the retry is a hard, loud
    error (the server is genuinely broken, not just slow). Carries the
    offending server name + last-seen status for the log + activity row.
    """

    def __init__(self, server: str, status: str) -> None:
        self.server = server
        self.status = status
        super().__init__(f"MCP server {server!r} not ready (status={status!r})")


class _ResultError(Exception):
    """Raised by `_run_query` when the SDK completes the stream but the
    terminal `ResultMessage` carries `is_error=True`.

    A context overflow does NOT always surface as a raised SDK exception:
    when the CLI subprocess reaches the model proxy and the upstream
    rejects the request body with a 400 (`token limit … requested …`), the
    CLI can emit a normal `result` message with `is_error=True` and the
    error text in `.result` instead of crashing. Without this, that path
    returned the error string as if it were a successful agent reply — the
    overflow looked like a (broken) answer and the heal never triggered.
    Re-raising as a typed exception routes the `is_error` result through
    the exact same retry/heal/wedge classification the raised-exception
    path uses, so detection lives in ONE place (`_execute_locked`).

    Carries the rendered error text so the classifier can match on it.
    """

    def __init__(self, text: str) -> None:
        self.text = text or ""
        super().__init__(self.text)


def _result_error_detail(
    result_text,
    subtype=None,
    api_error_status=None,
    errors=None,
):
    """Build the text carried by a `_ResultError`.

    When the SDK ResultMessage actually carried a `result` string (the
    common context-overflow case -- `token limit ... requested ...`), use
    it verbatim so the existing overflow classifier keeps matching.

    When `result` is EMPTY (the opaque agents-team-engine case: the SDK
    yields `is_error=True` with `result=None`), synthesize a concise,
    NON-SECRET detail from the diagnostic fields the ResultMessage DID
    carry -- `subtype` (e.g. "error_during_execution", "error_max_turns")
    and `api_error_status` (the upstream HTTP status, e.g. 401/429/404/500;
    the SDK documents this field as "Safe to log -- no message content").
    A short summary of `errors[]` is appended when present. This turns the
    previously opaque `_ResultError("")` -- which rendered as the useless
    "Agent error (_ResultError) -- see workspace logs for details" -- into
    a self-diagnosing string like
    `error_during_execution (api_error_status=401)`.

    Safety: subtype + status code are categorical/non-secret. Any
    `errors[]` text still flows through the runtime `sanitize_agent_error`,
    which truncates to ~1KB and scrubs token/key-shaped substrings before
    the string reaches the A2A response -- so this never leaks credentials
    or request bodies. We additionally cap the errors summary here so a
    pathological list can't dominate the message.
    """
    text = (result_text or "").strip()
    if text:
        return text
    parts = []
    if subtype:
        parts.append(str(subtype))
    if api_error_status is not None:
        parts.append("(api_error_status=%s)" % api_error_status)
    if errors:
        try:
            summary = "; ".join(str(e) for e in errors if e)
        except Exception:
            summary = ""
        summary = summary.strip()
        if summary:
            # Cap so a long errors list can't bloat the message; the
            # runtime sanitizer is the authoritative token/length scrubber.
            if len(summary) > 300:
                summary = summary[:300] + "…"
            parts.append("errors: " + summary)
    detail = " ".join(parts).strip()
    # Last-resort: never return "" (that is exactly the opaque case we are
    # fixing). A generic-but-honest marker still beats an empty string.
    return detail or "engine error with no detail (is_error result, empty result/subtype)"


@dataclass
class QueryResult:
    """Outcome of a single `query()` stream.

    `text` is the canonical final response; `session_id` is the id the SDK
    reports in its ResultMessage (used for resume on the next turn).
    `tool_uses` is the ordered list of tool names invoked during the turn
    — used as a UX-friendly fallback when `text` is empty (the agent did
    only tool calls and no final text block, common for autonomous-tick
    ticks that delegate or send_message_to_user without explanation).
    """
    text: str
    session_id: str | None
    tool_uses: list[str] = field(default_factory=list)


class ClaudeSDKExecutor(AgentExecutor):
    """Executes agent tasks via the claude-agent-sdk programmatic API."""

    def __init__(
        self,
        system_prompt: str | None,
        config_path: str,
        heartbeat: "HeartbeatLoop | None",
        model: str = "sonnet",
        prompt_files: list[str] | None = None,
        workspace_id: str = "",
        plugin_rules: str | None = None,
        plugin_prompts: list[str] | None = None,
    ):
        # system_prompt is the base-built prompt published onto
        # config.system_prompt (BASE-OWNED, honors prompt_files). It is the
        # authoritative SSOT value + the hot-reload fallback below.
        self.system_prompt = system_prompt
        self.config_path = config_path
        self.heartbeat = heartbeat
        self.model = model
        # prompt_files lets the per-turn hot-reload re-derive through the SAME
        # single builder (build_system_prompt) the base used — so a delivered
        # prompt takes effect without a restart AND prompt_files is honored
        # (no system-prompt.md-only re-read that ignores prompt_files).
        self.prompt_files = list(prompt_files or [])
        self.workspace_id = workspace_id
        # plugin_rules/plugin_prompts must also be threaded through the SAME
        # builder on hot-reload, otherwise a reloaded turn silently drops the
        # plugin fragments that setup() included (task #76 / #185).
        self.plugin_rules = plugin_rules
        self.plugin_prompts = list(plugin_prompts or [])
        self._session_id: str | None = None
        self._active_stream: AsyncIterator[Any] | None = None
        # Serializes concurrent execute() calls on the same executor so
        # session_id / _active_stream mutations stay race-free.
        self._run_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Prompt + options builders
    # ------------------------------------------------------------------

    def _resolve_cwd(self) -> str:
        """Run in /workspace if it has been populated, otherwise /configs."""
        if os.path.isdir(WORKSPACE_MOUNT) and os.listdir(WORKSPACE_MOUNT):
            return WORKSPACE_MOUNT
        return CONFIG_MOUNT

    def _build_system_prompt(self) -> str | None:
        """Compose the per-turn system prompt through the SINGLE base builder.

        SSOT (task #76): the prompt is built by the one canonical
        ``build_system_prompt`` (the same function the base ``_common_setup``
        uses), which honors ``config.prompt_files`` (with the legacy
        ``system-prompt.md`` fallback baked in) and already includes the base
        platform identity, A2A and HMA instructions. We call it PER TURN from
        ``self.config_path`` so a freshly-delivered/edited prompt file still
        hot-reloads without a restart (the load-bearing claude-code behavior),
        but the resolution now lives in ONE place — no more re-reading only
        ``system-prompt.md`` and silently ignoring ``prompt_files`` (the
        per-runtime drift that left the concierge identity-less).

        Only ``get_display_instructions()`` is appended here: it is the
        claude-code-specific native-tool-display guidance that is NOT part of
        the shared base build.

        If the builder is unavailable (e.g. a stubbed test runtime), fall back
        to the base-published ``self.system_prompt`` so the executor never
        boots prompt-less.
        """
        base: str | None = None
        try:
            from molecule_runtime.prompt import build_system_prompt

            base = build_system_prompt(
                self.config_path,
                self.workspace_id,
                [],   # skills: /configs/skills reaches claude-code via the
                #     ~/.claude/skills symlink (entrypoint.sh)
                [],   # peers: the CLI discovers peers live via the a2a MCP
                prompt_files=self.prompt_files,
                plugin_rules=self.plugin_rules,
                plugin_prompts=self.plugin_prompts,
            )
        except Exception:  # noqa: BLE001 — never let prompt-build crash a turn
            base = None

        if not base:
            # Builder unavailable / empty → the base-published prompt, which
            # itself was built by the same builder at setup() time.
            base = self.system_prompt

        display = get_display_instructions()
        parts = [p for p in (base, display) if p]
        return "\n\n".join(parts) if parts else None

    def _prepare_prompt(self, user_input: str) -> str:
        """Prepend delegation results that arrived while idle."""
        delegation_context = read_delegation_results()
        if delegation_context:
            return (
                "[Delegation results received while you were idle]\n"
                f"{delegation_context}\n\n[New message]\n{user_input}"
            )
        return user_input

    async def _inject_memories_if_first_turn(self, prompt: str) -> str:
        if self._session_id:
            return prompt
        memories = await recall_memories()
        if not memories:
            return prompt
        return f"[Prior context from memory]\n{memories}\n\n{prompt}"

    def _load_mcp_fragment(self) -> dict:
        """Read the standalone mcp_servers.yaml overlay fragment, if present.

        Same defensive posture as _load_config_dict: any I/O or parse error
        returns {} so a malformed fragment can never crash the executor.
        """
        try:
            fragment_file = os.path.join(self.config_path, "mcp_servers.yaml")
            with open(fragment_file) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}

    def _load_settings_mcp(self) -> dict:
        """Read ``/configs/.claude/settings.json`` for its ``mcpServers`` map.

        This is the file the plugin MCPServerAdaptor (RFC#3045) merges
        plugin-delivered MCP servers into. Same defensive posture as
        ``_load_config_dict``: any I/O or parse error returns ``{}`` so a
        malformed settings file can never crash the executor. (core#3079)
        """
        try:
            settings_file = os.path.join(self.config_path, ".claude", "settings.json")
            with open(settings_file) as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def _load_config_dict(self) -> dict:
        """Read config.yaml as a raw dict for field-level inspection.

        Returns an empty dict on any I/O or parse error so callers can
        always use ``.get()`` without guards.
        """
        try:
            config_file = os.path.join(self.config_path, "config.yaml")
            with open(config_file) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}

    def _declared_extra_mcp_names(self) -> list[str]:
        """Names of declared MCP servers other than the built-in ``a2a`` server.

        These are the servers the readiness gate must wait for before the
        turn's prompt is sent — ``a2a`` is an in-process stdio MCP that the
        executor controls and that connects immediately, but extra servers
        (the platform agent's ``platform`` / molecule-mcp-server, or a
        plugin-delivered ``molecule-platform`` npx server) are slow external
        subprocesses whose handshake races the CLI's init message. Empty for
        ordinary workspaces → the fast `query()` path stays in effect.

        Built by running the SAME merge helpers the options build uses across
        ALL three delivery channels (config.yaml `mcp_servers`, the
        /configs/mcp_servers.yaml fragment, and the plugin's
        /configs/.claude/settings.json `mcpServers` — core#3079), so the gate
        stays in lockstep with what is actually launched. Without the plugin
        channel here, a plugin-delivered server would load but the turn could
        start before its handshake completes.
        """
        merged: dict = {"a2a": {}}
        _apply_extra_mcp_servers(merged, self._load_config_dict())
        _apply_extra_mcp_servers(merged, self._load_mcp_fragment())
        _apply_settings_mcp_servers(merged, self._load_settings_mcp())
        return [name for name in merged if name != "a2a"]

    def _maybe_set_context_window_env(self) -> None:
        """Tell claude-code the model's REAL context window (deeper fix).

        Root cause of the Kimi wedge: claude-code's auto-compact threshold
        is derived from its internal context-window resolver, which only
        knows Anthropic models and falls back to a hard-coded 200000 for
        anything reached through the molecule LLM proxy (Kimi/MiniMax/GLM/
        DeepSeek). With the wrong window, compaction fires against the
        wrong number and the session is allowed to drift into a band the
        upstream still rejects.

        claude-code honors ``CLAUDE_CODE_MAX_CONTEXT_TOKENS`` as an
        explicit override of that resolver (see the bundled CLI's `B2`
        function). Setting it to the model's true window makes auto-compact
        trigger at the correct point — PREVENTING the overflow rather than
        only recovering from it. The auto-heal stays as the safety net for
        any window we don't have configured.

        Source of the window (first hit wins):
          1. ``MODEL_CONTEXT_WINDOW`` env (persona/operator override).
          2. ``context_window`` in config.yaml.
        Absent/invalid → leave the env untouched so claude-code keeps its
        own default behavior (no regression for Anthropic models, whose
        resolver is already correct).

        Idempotent + non-destructive: if the env is already set (operator
        pinned it, or a prior call set it) we don't overwrite it.
        """
        if os.environ.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS"):
            return
        raw = os.environ.get("MODEL_CONTEXT_WINDOW")
        if not raw:
            raw = self._load_config_dict().get("context_window")
        if raw in (None, ""):
            # 3rd source (the gap that wedged JRS): proxy-routed models
            # (Kimi/MiniMax/GLM/DeepSeek) are NOT in claude-code's window
            # resolver AND usually have no MODEL_CONTEXT_WINDOW/config set, so
            # without this the env stays unset → claude-code uses its 200k
            # fallback → auto-compact fires against the wrong number → the
            # session overflows the model's REAL window (Kimi hit 262191 >
            # 262144) → the wipe-reset auto-heal loses all task memory. Map
            # the known proxy models to their true window so claude-code's
            # NATIVE compaction (which summarizes, NOT wipes) triggers
            # correctly and the overflow never happens. Substring match on the
            # resolved model id. SSOT note: ideally the provisioner sets
            # `context_window` in config.yaml per model (the 2nd source above);
            # this map is the safety net until that lands (tracked: runtime#133).
            raw = _known_model_context_window(self.model)
        if raw in (None, ""):
            return
        try:
            window = int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "context_window=%r is not an integer — leaving claude-code's "
                "default window resolver in place", raw,
            )
            return
        if window <= 0:
            return
        os.environ["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(window)
        logger.info(
            "set CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d so auto-compact triggers "
            "against the model's real context window (model=%s) — prevents "
            "the proxy-routed-model context-overflow wedge",
            window, self.model,
        )

    def _maybe_set_mcp_connect_timeout_env(self) -> None:
        """Give slow stdio MCP servers more time to connect (preventive half
        of the stuck-MCP fix).

        The platform agent's `platform` MCP is a `node` subprocess whose
        handshake was observed to take ~6-13s and, under load, occasionally
        exceed the claude CLI's default 30s MCP-connect timeout — at which
        point the CLI marks the server `failed` and its tools never reach the
        LLM. The CLI honors ``MCP_TIMEOUT`` (ms) as an override of that
        connect timeout; pinning it higher makes premature `failed` far less
        likely, complementing the readiness gate (which RECOVERS) with
        PREVENTION.

        Only applied when the config declares extra (non-`a2a`) MCP servers —
        ordinary workspaces keep the CLI default. Idempotent + non-destructive:
        never clobbers an operator pin.
        """
        if os.environ.get("MCP_TIMEOUT"):
            return
        if not self._declared_extra_mcp_names():
            return
        os.environ["MCP_TIMEOUT"] = _MCP_CONNECT_TIMEOUT_MS
        logger.info(
            "set MCP_TIMEOUT=%s so the slow platform MCP isn't marked failed "
            "before its handshake completes (prevents the stuck-MCP tool-loss)",
            _MCP_CONNECT_TIMEOUT_MS,
        )

    def _build_options(self) -> Any:
        """Build ClaudeAgentOptions.

        No allowed_tools allowlist — bypassPermissions grants full access,
        matching the old CLI `--dangerously-skip-permissions` so Claude can
        use every built-in tool (Task, TodoWrite, NotebookEdit, BashOutput/
        KillShell, ExitPlanMode, etc.) plus all MCP tools.

        The MCP server launcher uses `sys.executable` so tests and alternate
        virtual-env layouts don't depend on a `python3` shim being on PATH.

        output_config wiring (issue #652)
        ----------------------------------
        Reads ``effort`` and ``task_budget`` from config.yaml and populates
        ``output_config`` on the SDK options before the API call:

        - ``effort`` (str): one of low|medium|high|xhigh|max.  xhigh is the
          Opus 4.7 recommended default for long agentic tasks.
        - ``task_budget`` (int): advisory total-token budget across the full
          agentic loop.  Must be >= 20000 (API minimum) or 0/absent (unset).
          When set, the ``task-budgets-2026-03-13`` beta header is added so
          the API accepts the field.
        """
        # Deeper fix for the context-overflow wedge: pin the model's real
        # context window so claude-code's auto-compact triggers against the
        # right number instead of its 200k fallback for proxy-routed
        # models. No-op when unconfigured (Anthropic models keep their
        # correct built-in resolver).
        self._maybe_set_context_window_env()
        # Preventive half of the stuck-MCP fix: give the slow platform MCP
        # more connect headroom so it isn't marked `failed` before its
        # handshake completes (no-op for ordinary workspaces).
        self._maybe_set_mcp_connect_timeout_env()

        mcp_servers = {
            "a2a": {
                "command": sys.executable,
                "args": [get_mcp_server_path()],
            }
        }
        # Merge any config-declared MCP servers (e.g. the platform-management
        # MCP for the org-level platform agent). No-op for ordinary workspaces.
        _apply_extra_mcp_servers(mcp_servers, self._load_config_dict())
        # Overlay fragment (core#2522): the provisioner ships the concierge's
        # platform-MCP declaration as a standalone /configs/mcp_servers.yaml,
        # because on the SaaS restart-provision path no base config.yaml is
        # resolvable to append onto (the pilot's TOOLS-FAIL RCA, 2026-06-10).
        # Applied AFTER config.yaml so the platform-authored fragment wins on
        # a same-name entry. Absent file -> {} -> no-op for every ordinary
        # workspace.
        _apply_extra_mcp_servers(mcp_servers, self._load_mcp_fragment())
        # Plugin channel (core#3079 / core#3082): when the platform-management MCP is
        # delivered as a PLUGIN (RFC#3045), the MCPServerAdaptor writes it into
        # /configs/.claude/settings.json `mcpServers`. The CLI runs with
        # --strict-mcp-config and ignores that on-disk file, so fold those
        # servers into the SDK options here or they never load. Applied LAST so
        # a plugin-authored server is authoritative on a same-name entry.
        #
        # Log the on-disk names so operators can see what --strict-mcp-config
        # would silently swallow if the executor did not fold it (core#3082).
        settings_mcp = self._load_settings_mcp()
        settings_mcp_servers = settings_mcp.get("mcpServers") or {}
        if settings_mcp_servers:
            settings_path = os.path.join(self.config_path, ".claude", "settings.json")
            logger.info(
                "on-disk mcpServers from %s would be ignored due to --strict-mcp-config; "
                "folding into SDK options: %s",
                settings_path,
                sorted(settings_mcp_servers.keys()),
            )
        _apply_settings_mcp_servers(mcp_servers, settings_mcp)

        create_kwargs: dict = dict(
            model=self.model,
            permission_mode="bypassPermissions",
            cwd=self._resolve_cwd(),
            mcp_servers=mcp_servers,
            system_prompt=self._build_system_prompt(),
            resume=self._session_id,
            # Forward --dangerously-load-development-channels to the spawned
            # claude CLI so the host registers our experimental.claude/channel
            # capability instead of dropping the notification on the allowlist
            # check. The wheel ships the gates (PR molecule-core#2463) and the
            # inbox bridge fires the notification, but without this flag the
            # CLI silently filters it during the channels research preview.
            #
            # The flag's signature in Claude Code 2.1.x takes an *allowlist*
            # of tagged entries — `server:<name>` for manually-configured
            # MCP servers, `plugin:<name>@<marketplace>` for plugin
            # channels. Passing `None` (the original PR #25 shape) renders
            # as a bare `--<flag>` with no value; the CLI rejects with
            # `argument missing` and the SDK times out at `initialize`,
            # surfacing as `Control request timeout: initialize` upstream
            # (caught live on workspace dd40faf8 on 2026-05-01 — every
            # A2A turn wedged 100% of the time). Verified live: with the
            # tagged value, A2A returns coherent replies AND the host
            # claude session renders inbound messages as `<channel>` tags
            # inline (no inbox poll needed). Drop once channels graduate
            # to the default allowlist.
            #
            # Task #214 — CLI 2.1.143 made the flag variadic (nargs='+').
            # The `{flag: value}` shape renders as TWO argv elements (see
            # claude_agent_sdk subprocess_cli.py:340) and the channels
            # parser then greedily absorbs the SDK's downstream `--print
            # <prompt>` argv pair, wedging the SDK at initialize. Fix:
            # pack `=value` into the key so the renderer's None-value
            # path emits a single argv element which the variadic parser
            # cannot reach across.
            extra_args={"dangerously-load-development-channels=server:molecule": None},
        )

        # --- output_config: effort + task_budget (issue #652) ---
        config = self._load_config_dict()
        output_config: dict = {}
        effort = config.get("effort", "")
        task_budget = config.get("task_budget", 0)

        if effort:
            output_config["effort"] = effort  # "low"|"medium"|"high"|"xhigh"|"max"

        if task_budget and int(task_budget) >= 20000:
            output_config["task_budget"] = {
                "type": "tokens",
                "total": int(task_budget),
            }
            betas = list(create_kwargs.get("betas", []))
            if "task-budgets-2026-03-13" not in betas:
                betas.append("task-budgets-2026-03-13")
            create_kwargs["betas"] = betas
        elif task_budget and int(task_budget) > 0:
            # Below minimum — reject clearly before any API call is made.
            raise ValueError(
                f"task_budget must be >= 20000 tokens (got {task_budget})"
            )

        if output_config:
            create_kwargs["output_config"] = output_config

        return sdk.ClaudeAgentOptions(**create_kwargs)

    # ------------------------------------------------------------------
    # Query streaming
    # ------------------------------------------------------------------

    class _StreamAccumulator:
        """Mutable scratch state shared by the plain and gated query paths.

        Both `_run_query` (one-shot `sdk.query()`) and `_run_query_gated`
        (persistent `ClaudeSDKClient`) consume the SAME message stream shape
        (AssistantMessage / ResultMessage). This holds the per-turn
        accumulators so the message-handling logic lives in exactly one place
        (`_accumulate_message`) and can't drift between the two paths.
        """

        def __init__(self) -> None:
            self.assistant_chunks: list[str] = []
            self.tool_uses: list[str] = []
            self.result_text: str | None = None
            self.session_id: str | None = None
            self.result_is_error: bool = False
            # Diagnostic fields off the terminal ResultMessage, kept so an
            # opaque is_error result (result=None) still yields a useful,
            # non-secret _ResultError detail (subtype + upstream HTTP status).
            self.result_subtype: str | None = None
            self.result_api_error_status: int | None = None
            self.result_errors: list | None = None

    async def _accumulate_message(self, message: Any, acc: "ClaudeSDKExecutor._StreamAccumulator") -> None:
        """Fold one SDK stream message into `acc`.

        Extracted verbatim from the original `_run_query` loop so the gated
        path (`_run_query_gated`) reuses identical text / tool-use / session-id
        / is_error handling. Keep this the ONLY place that reads message
        blocks.
        """
        if isinstance(message, sdk.AssistantMessage):
            for block in message.content:
                if isinstance(block, sdk.TextBlock):
                    acc.assistant_chunks.append(block.text)
                else:
                    # Handle thinking/reasoning blocks from Anthropic-
                    # compatible upstreams (MiniMax M2/M2.7, Moonshot
                    # K2.6) so reasoning-only output doesn't surface as
                    # empty content. Duck-typing: real SDK objects have
                    # a `.thinking` attr; dict-shaped blocks have
                    # `type: "thinking"`.
                    thinking_text = None
                    if hasattr(block, "thinking"):
                        thinking_text = getattr(block, "thinking", None)
                    elif isinstance(block, dict) and block.get("type") == "thinking":
                        thinking_text = block.get("thinking")
                    if thinking_text:
                        acc.assistant_chunks.append(thinking_text)
                        return
                    # ToolUseBlock / ServerToolUseBlock are present
                    # on the real SDK but not on the conftest stub —
                    # check by class name to avoid an isinstance()
                    # against a class the stub doesn't define.
                    cls = type(block).__name__
                    if cls in ("ToolUseBlock", "ServerToolUseBlock"):
                        await _report_tool_use(block)
                        name = getattr(block, "name", "") or ""
                        if name:
                            acc.tool_uses.append(name)
        elif isinstance(message, sdk.ResultMessage):
            sid = getattr(message, "session_id", None)
            if sid:
                acc.session_id = sid
            acc.result_text = getattr(message, "result", None)
            # The SDK reports an upstream-rejected request (e.g. a 400
            # context overflow from the model proxy) as a terminal result
            # with is_error=True rather than a raised exception. Capture it
            # so the error path can re-raise it into the unified
            # classification.
            acc.result_is_error = bool(getattr(message, "is_error", False))
            # Diagnostic fields the SDK ResultMessage carries on a terminal
            # error (verified against claude-agent-sdk ResultMessage: .subtype,
            # .api_error_status:int|None, .errors:list[str]|None). Captured so
            # an opaque is_error result (result=None) still self-diagnoses.
            acc.result_subtype = getattr(message, "subtype", None)
            acc.result_api_error_status = getattr(message, "api_error_status", None)
            acc.result_errors = getattr(message, "errors", None)
        elif type(message).__name__ == "SystemMessage":
            # The CLI's `init` system message carries the tool list the model
            # will see this turn (.data["tools"]). Record the loaded mcp__* tool
            # ids so the heartbeat reports `loaded_mcp_tools` (core#3082) —
            # proving the management MCP's tools are actually LIVE in the model's
            # tool list, not merely declared. This is the producer the platform
            # online/degraded gate needs; without it the gate fail-closes on the
            # absent tool list and the concierge reads as degraded.
            #
            # Class-name check (not isinstance) so the conftest SDK stub, which
            # need not define SystemMessage, doesn't break. Bookkeeping only —
            # never let it raise into the turn.
            if getattr(message, "subtype", None) == "init":
                try:
                    data = getattr(message, "data", None)
                    tools = data.get("tools") if isinstance(data, dict) else None
                    if isinstance(tools, list):
                        set_loaded_mcp_tools(
                            [
                                t
                                for t in tools
                                if isinstance(t, str) and t.startswith("mcp__")
                            ]
                        )
                except Exception:
                    logger.debug("loaded_mcp_tools capture skipped", exc_info=True)

    @staticmethod
    def _mcp_servers_from_status(status: Any) -> list[dict]:
        """Normalize a `get_mcp_status()` response to a list of server dicts.

        The SDK returns ``{"mcpServers": [...]}`` (wire shape); be tolerant of
        a bare list too so a future SDK shape-change degrades gracefully.
        """
        if isinstance(status, dict):
            servers = status.get("mcpServers")
        else:
            servers = status
        return [s for s in (servers or []) if isinstance(s, dict)]

    async def _await_mcp_ready(self, client: Any, declared: list[str]) -> None:
        """Block (bounded) until every declared MCP server is `connected` AND
        exposes the SSOT-required management tool in its callable tool list.

        Polls `client.get_mcp_status()` up to `_MCP_READY_MAX_POLLS` times.
        Returns as soon as all `declared` servers report `connected` and their
        `tools` list includes `_PLATFORM_MCP_REQUIRED_TOOL`. Raises
        `_McpNotReadyError` if any declared server hits a TERMINAL failure
        status, if the poll budget is exhausted, or if a server is connected
        but missing the required callable tool.

        This is THE fix for the stuck-`platform`-MCP bug: it holds the CLI
        subprocess open through the `node` server's slow handshake so the
        prompt is only sent once the required `mcp__platform__*` tool is
        actually live and will appear in the model's tool list.
        """
        declared_set = set(declared)
        last_status: dict[str, Any] = {}
        for _poll in range(_MCP_READY_MAX_POLLS):
            try:
                status = await client.get_mcp_status()
            except Exception:
                # A transient status-probe failure shouldn't abort the gate;
                # keep polling. If it's genuinely broken we'll exhaust the
                # budget and raise below with the last-seen status.
                await asyncio.sleep(_MCP_READY_POLL_INTERVAL_S)
                continue
            servers = self._mcp_servers_from_status(status)
            last_status = {s.get("name", ""): s for s in servers}
            # Terminal failure on any declared server → stop early, heal/retry.
            for name in declared_set:
                server = last_status.get(name) or {}
                st = server.get("status", "pending")
                if st in _MCP_TERMINAL_FAIL_STATUSES:
                    raise _McpNotReadyError(name, st)
            # Success only when every declared server is connected AND the
            # SSOT-required tool is present in its callable tool list. A server
            # can be "connected" but still not expose the management tool (e.g.
            # the mgmt-MCP is status=failed internally); that must NOT read as
            # ready.
            all_ready = True
            for name in declared_set:
                server = last_status.get(name) or {}
                if server.get("status") != _MCP_READY_STATUS:
                    all_ready = False
                    break
                tool_names = _tool_names_from_mcp_server_status(server)
                if _PLATFORM_MCP_REQUIRED_TOOL not in tool_names:
                    raise _McpNotReadyError(
                        name, f"connected-missing-{_PLATFORM_MCP_REQUIRED_TOOL}"
                    )
            if all_ready:
                logger.debug(
                    "MCP readiness gate: all declared servers connected with "
                    "%s callable (%s)",
                    _PLATFORM_MCP_REQUIRED_TOOL,
                    declared,
                )
                return
            await asyncio.sleep(_MCP_READY_POLL_INTERVAL_S)
        # Budget exhausted — surface the first still-unconnected server so the
        # heal path can reload it.
        for name in declared:
            server = last_status.get(name) or {}
            if server.get("status") != _MCP_READY_STATUS:
                raise _McpNotReadyError(name, server.get("status", "pending"))
            tool_names = _tool_names_from_mcp_server_status(server)
            if _PLATFORM_MCP_REQUIRED_TOOL not in tool_names:
                raise _McpNotReadyError(
                    name, f"connected-missing-{_PLATFORM_MCP_REQUIRED_TOOL}"
                )
        # Defensive: loop exited without all-ready yet no name flagged
        # (e.g. empty status). Treat as not-ready on the first declared name.
        raise _McpNotReadyError(declared[0], "unknown")

    async def _run_query_gated(self, prompt: str, options: Any, declared: list[str]) -> QueryResult:
        """Readiness-gated variant of `_run_query` for the platform agent.

        Uses a persistent `ClaudeSDKClient` so we can poll `get_mcp_status()`
        and WAIT for the slow `platform` MCP to connect BEFORE the prompt is
        sent — otherwise the one-shot `query()` ships its init (and the tool
        list the LLM sees) while the server is still `pending`, hiding the 88
        org-admin tools. Only used when `declared` is non-empty (extra MCP
        servers configured); ordinary workspaces never reach here.

        Same message handling + wedge-clear + is_error re-raise semantics as
        `_run_query` (via the shared `_accumulate_message`), and the same
        `self._active_stream` contract so `cancel()` can reach in.

        Auto-heal: if a declared server is not ready (including the case where
        it is connected but the SSOT-required tool is missing), the executor
        reloads that MCP server via `client.reconnect_mcp_server()` and re-runs
        the readiness gate. This is bounded by `_MCP_HEAL_MAX_RETRIES`; after
        that the workspace is marked wedged so the platform reports degraded.
        """
        acc = self._StreamAccumulator()
        client = sdk.ClaudeSDKClient(options=options)
        await client.connect()
        mcp_reloads = 0
        try:
            # Gate: hold the subprocess open until the platform MCP's required
            # tool is actually callable. If not ready, reload the server and
            # re-gate (bounded).
            while True:
                try:
                    await self._await_mcp_ready(client, declared)
                    break
                except _McpNotReadyError as exc:
                    if mcp_reloads >= _MCP_HEAL_MAX_RETRIES:
                        logger.error(
                            "stuck-MCP auto-heal exhausted [claude-code]: MCP "
                            "server %r still not ready (status=%s) after %d "
                            "reconnect attempts — failing degraded",
                            exc.server,
                            exc.status,
                            mcp_reloads,
                        )
                        _mark_sdk_wedged(
                            f"MCP server {exc.server!r} not ready after reconnect "
                            f"({exc.status}) — restart workspace to recover"
                        )
                        raise
                    mcp_reloads += 1
                    logger.error(
                        "auto-heal: reloading MCP server %r (status=%s) "
                        "[claude-code] (reload %d/%d)",
                        exc.server,
                        exc.status,
                        mcp_reloads,
                        _MCP_HEAL_MAX_RETRIES,
                    )
                    try:
                        await client.reconnect_mcp_server(exc.server)
                    except Exception as recon_exc:
                        logger.warning(
                            "reconnect_mcp_server(%r) raised: %s — will re-gate",
                            exc.server,
                            _format_process_error(recon_exc),
                        )
                    continue
            await client.query(prompt)
            self._active_stream = client.receive_response()
            try:
                async for message in _aiter_with_idle_cap(self._active_stream):
                    await self._accumulate_message(message, acc)
            except Exception:
                # Mirror _run_query: prefer the captured is_error text over a
                # trailing generic CLI exception so overflow detection works.
                if acc.result_is_error:
                    raise _ResultError(_result_error_detail(
                        acc.result_text,
                        acc.result_subtype,
                        acc.result_api_error_status,
                        acc.result_errors,
                    ))
                raise
            finally:
                self._active_stream = None
        finally:
            # Always tear down the persistent subprocess — one client per turn
            # (matches the one-shot query() lifecycle; no cross-turn warm cache
            # exists for stdio MCP subprocesses anyway).
            try:
                await client.disconnect()
            except Exception:
                logger.debug("MCP-gated client disconnect raised (ignored)")
        if acc.result_is_error:
            raise _ResultError(_result_error_detail(
                acc.result_text,
                acc.result_subtype,
                acc.result_api_error_status,
                acc.result_errors,
            ))
        text = acc.result_text if acc.result_text is not None else "".join(acc.assistant_chunks)
        if acc.result_text is not None or acc.assistant_chunks:
            _clear_sdk_wedge_on_success()
        return QueryResult(text=text, session_id=acc.session_id, tool_uses=acc.tool_uses)

    async def _run_query(self, prompt: str, options: Any) -> QueryResult:
        """Drive the SDK query stream and return a QueryResult.

        Prefers ResultMessage.result (the canonical final text — same field
        the CLI's --output-format json used) and only falls back to the
        concatenation of AssistantMessage TextBlocks when result is absent.
        Otherwise pre-tool reasoning and post-tool summary get double-emitted.

        Pure: does not mutate executor state other than setting / clearing
        `self._active_stream` so cancel() can reach in. The caller decides
        whether to persist the returned session_id.

        Dispatch: when the config declares extra (non-`a2a`) MCP servers (the
        platform agent), delegate to the readiness-gated path so the slow
        `platform` MCP is `connected` BEFORE the prompt is sent — otherwise its
        88 org-admin tools are hidden from the turn's tool list. Ordinary
        workspaces declare none and keep this fast one-shot `query()` path.
        """
        declared_extra = self._declared_extra_mcp_names()
        if declared_extra:
            return await self._run_query_gated(prompt, options, declared_extra)

        assistant_chunks: list[str] = []
        tool_uses: list[str] = []
        result_text: str | None = None
        session_id: str | None = None
        result_is_error: bool = False
        result_subtype: str | None = None
        result_api_error_status: int | None = None
        result_errors: list | None = None
        self._active_stream = sdk.query(prompt=prompt, options=options)
        try:
            try:
                async for message in _aiter_with_idle_cap(self._active_stream):
                    if isinstance(message, sdk.AssistantMessage):
                        for block in message.content:
                            if isinstance(block, sdk.TextBlock):
                                assistant_chunks.append(block.text)
                            else:
                                # Handle thinking/reasoning blocks from Anthropic-
                                # compatible upstreams (MiniMax M2/M2.7, Moonshot
                                # K2.6) so reasoning-only output doesn't surface as
                                # empty content. Duck-typing: real SDK objects have
                                # a `.thinking` attr; dict-shaped blocks have
                                # `type: "thinking"`.
                                thinking_text = None
                                if hasattr(block, "thinking"):
                                    thinking_text = getattr(block, "thinking", None)
                                elif isinstance(block, dict) and block.get("type") == "thinking":
                                    thinking_text = block.get("thinking")
                                if thinking_text:
                                    assistant_chunks.append(thinking_text)
                                    continue
                                # ToolUseBlock / ServerToolUseBlock are present
                                # on the real SDK but not on the conftest stub —
                                # check by class name to avoid an isinstance()
                                # against a class the stub doesn't define.
                                cls = type(block).__name__
                                if cls in ("ToolUseBlock", "ServerToolUseBlock"):
                                    await _report_tool_use(block)
                                    name = getattr(block, "name", "") or ""
                                    if name:
                                        tool_uses.append(name)
                    elif isinstance(message, sdk.ResultMessage):
                        sid = getattr(message, "session_id", None)
                        if sid:
                            session_id = sid
                        result_text = getattr(message, "result", None)
                        # The SDK reports an upstream-rejected request (e.g. a
                        # 400 context overflow from the model proxy) as a
                        # terminal result with is_error=True rather than a
                        # raised exception. Capture it so the error path below
                        # can re-raise it into the unified classification.
                        result_is_error = bool(getattr(message, "is_error", False))
                        # Same diagnostic capture as the gated path so an
                        # opaque is_error result (result=None) self-diagnoses.
                        result_subtype = getattr(message, "subtype", None)
                        result_api_error_status = getattr(message, "api_error_status", None)
                        result_errors = getattr(message, "errors", None)
            except Exception:
                # REAL-SDK SHAPE (claude CLI 2.1.163 / SDK 0.1.72): on a
                # terminal error result the SDK yields the is_error=True
                # ResultMessage AND THEN raises a generic
                # `Exception("Command failed with exit code 1 / Check stderr
                # output for details")` because the CLI process exits
                # non-zero. That trailing raise pre-empts the post-loop
                # `_ResultError` re-raise below, so the REAL overflow text
                # ("token limit … requested …" / "Prompt is too long") would
                # be discarded and `_is_context_overflow` would only ever see
                # the generic "Command failed" string → the heal never fires.
                #
                # If an is_error result was already seen, that captured text
                # is the authoritative failure reason — re-raise it as a
                # typed `_ResultError` IN PREFERENCE to the generic CLI
                # exception so the overflow text reaches `_is_context_overflow`
                # in `_execute_locked`. Any other stream exception (no
                # is_error result captured) propagates unchanged.
                if result_is_error:
                    raise _ResultError(_result_error_detail(
                        result_text,
                        result_subtype,
                        result_api_error_status,
                        result_errors,
                    ))
                raise
        finally:
            self._active_stream = None
        # An is_error result is an upstream rejection (e.g. a 400 context
        # overflow from the model proxy) that the SDK surfaced as a normal
        # terminal message instead of a raised exception. Re-raise it so it
        # flows through the same retry/heal/wedge classification in
        # _execute_locked as a raised SDK error — detection lives in one
        # place. The session_id (if any) was NOT persisted to self by this
        # method; the caller's heal path clears it regardless.
        #
        # This handles the variant where the SDK yields the is_error result
        # and then completes the iterator cleanly (no trailing raise); the
        # except-branch above handles the real-SDK variant where it raises.
        if result_is_error:
            raise _ResultError(_result_error_detail(
                result_text,
                result_subtype,
                result_api_error_status,
                result_errors,
            ))
        text = result_text if result_text is not None else "".join(assistant_chunks)
        # Auto-recover the wedge flag — if a previous query() left this
        # process in `_sdk_wedged` and THIS query just completed
        # cleanly, the SDK clearly works again. Clear so the next
        # heartbeat reports runtime_state empty and the platform flips
        # status degraded → online without a manual restart.
        #
        # Gate on actual content from the stream so a degenerate
        # "iterator returned without raising but emitted nothing"
        # case (possible from a partial stream or a stub SDK) doesn't
        # falsely advertise recovery. A real successful query yields
        # at least a ResultMessage (sets result_text) or one
        # AssistantMessage TextBlock (populates assistant_chunks).
        if result_text is not None or assistant_chunks:
            _clear_sdk_wedge_on_success()
        return QueryResult(text=text, session_id=session_id, tool_uses=tool_uses)

    # ------------------------------------------------------------------
    # AgentExecutor interface
    # ------------------------------------------------------------------

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        """Run a turn through the Claude Agent SDK and emit the response.

        Serialized via `self._run_lock` — concurrent A2A messages to the same
        workspace queue rather than racing on `_session_id` / `_active_stream`.
        """
        user_input = extract_message_text(context.message)
        # Surface attached files to claude-code via a manifest in the prompt.
        # Claude Code reads files through its own Read/Glob tools by path —
        # as long as the prompt names the path, the CLI will open them on
        # demand. Same contract every platform runtime uses so the UX is
        # identical across official runtimes.
        attached = extract_attached_files(context.message)
        if attached:
            manifest = "\n\nAttached files:\n" + "\n".join(
                f"- {f['name']} ({f['mime_type'] or 'unknown type'}) at {f['path']}"
                for f in attached
            )
            user_input = (user_input + manifest) if user_input else manifest.lstrip()
        if not user_input:
            await event_queue.enqueue_event(new_text_message(_NO_TEXT_MSG))
            return

        async with self._run_lock:
            response_text = await self._execute_locked(user_input)

        # Enqueue outside the lock so the next queued turn can start
        # preparing its prompt while this turn's response ships. Event
        # ordering is preserved per-queue by the A2A server, so no races.
        # If the response mentions /workspace/... files, stage each and
        # emit file parts alongside the text so the canvas can download.
        #
        # a2a-sdk v1 uses protobuf, NOT the v0 Pydantic discriminated-union
        # types. There is no FilePart / TextPart / FileWithUri class — Part
        # is one struct with optional `text`, `url`, `raw`, `data`,
        # `filename`, `media_type` fields (plus `metadata`). Set the field
        # that matches the part's nature; leave the rest unset.
        outbound = collect_outbound_files(response_text)
        if outbound:
            from a2a.types import Message, Part, Role
            import uuid as _uuid
            parts: list = [Part(text=response_text)] if response_text else []
            for f in outbound:
                parts.append(Part(
                    url="workspace:" + f["path"],
                    filename=f["name"],
                    media_type=f["mime_type"],
                ))
            await event_queue.enqueue_event(Message(
                message_id=_uuid.uuid4().hex,
                role=Role.ROLE_AGENT,
                parts=parts,
            ))
        else:
            await event_queue.enqueue_event(new_text_message(response_text))

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        """Check if an SDK exception looks like a transient rate-limit or
        capacity error that's worth retrying with backoff."""
        msg = str(exc).lower()
        return any(p in msg for p in _RETRYABLE_PATTERNS)

    def _reset_session_after_error(self, exc: BaseException) -> None:
        """Clear `_session_id` if the exception looks like a subprocess
        crash (#75). On the next `_build_options()` call `resume=None` is
        passed to the SDK, so the CLI boots a brand-new session instead of
        trying to resume one the previous subprocess left in an
        unrecoverable state.

        Kept in its own method so the policy can evolve (e.g. also clear
        on MessageParseError) without touching the retry loop. Logs at
        INFO when a session was actually cleared; silent when there was
        nothing to reset.
        """
        exc_name = type(exc).__name__
        # Conservative: reset only on subprocess-level failures. Pure
        # rate-limit / capacity errors don't leave the session in a bad
        # state — keep the session_id so the resumed turn preserves
        # conversational continuity.
        is_subprocess_error = (
            exc_name in ("ProcessError", "CLIConnectionError")
            or getattr(exc, "exit_code", None) is not None
            or "exit code" in str(exc).lower()
        )
        if not is_subprocess_error:
            return
        if self._session_id is None:
            return
        logger.info(
            "SDK session reset after %s: clearing session_id so the next "
            "attempt starts fresh (fixes #75 session contamination)",
            exc_name,
        )
        self._session_id = None

    def _reset_session_for_context_overflow(self) -> None:
        """Hard session reset for a context-window overflow auto-heal.

        Stronger than `_reset_session_after_error`: the bloated transcript
        on disk is the *cause* of the overflow, so we both (a) clear
        `self._session_id` (next `_build_options()` passes `resume=None`,
        so the SDK boots a brand-new, empty session) AND (b) best-effort
        purge the stale on-disk session transcripts so the oversized
        history can never be accidentally resumed by a later boot.

        The SDK stores per-session transcripts as
        ``~/.claude/projects/<project_key>/<session>.jsonl`` (honoring
        ``CLAUDE_CONFIG_DIR`` when set). We don't have a cheap
        project_key→path mapping here, so we purge ALL ``*.jsonl`` session
        files under the projects tree — this workspace runs exactly one
        agent, so there is no other agent's session to protect, and a
        fresh boot simply re-creates the dir. Bounded + best-effort: any
        filesystem error is swallowed (the in-memory `resume=None` reset
        alone is sufficient to recover; the disk purge is belt-and-braces
        so a future explicit-resume path can't reach the bloated file).
        """
        self._session_id = None
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
            os.path.expanduser("~"), ".claude"
        )
        projects_root = os.path.join(config_dir, "projects")
        if not os.path.isdir(projects_root):
            return
        purged = 0
        try:
            for jsonl in glob.glob(
                os.path.join(projects_root, "**", "*.jsonl"), recursive=True
            ):
                try:
                    os.remove(jsonl)
                    purged += 1
                except OSError:
                    # A single un-removable file (perm drift, race) must
                    # not abort the heal — the resume=None reset already
                    # guarantees a fresh session next turn.
                    continue
        except Exception:
            logger.exception(
                "context-overflow heal: session-transcript purge raised "
                "(resume=None reset still in effect — recovery proceeds)"
            )
            return
        if purged:
            logger.info(
                "context-overflow heal: purged %d stale session transcript(s) "
                "under %s",
                purged,
                projects_root,
            )

    async def _notify_context_overflow_heal(self, detail: str) -> None:
        """Best-effort operator-visible signal that an auto-heal fired.

        The ERROR log is the durable record; this posts a one-line
        agent_log activity row so the canvas's live feed shows the heal in
        real time instead of only surfacing it in container logs. Mirrors
        `_report_tool_use`: lazy imports, short timeout, every failure
        swallowed — telemetry must never break (or block) the heal+retry.

        Deliberately a chat-feed signal, NOT a runtime wedge: the workspace
        self-recovers on the very next (retried) turn, so flipping it to
        `degraded` would be a false alarm the operator can't action.
        """
        try:
            import httpx
            from molecule_runtime.a2a_client import PLATFORM_URL, WORKSPACE_ID
            from molecule_runtime.platform_auth import auth_headers
        except Exception:
            return
        try:
            summary = (
                "♻️ Auto-heal: context window overflowed — reset session and "
                "retried on a fresh session. "
                f"({detail[:120]})"
            )
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{PLATFORM_URL}/workspaces/{WORKSPACE_ID}/activity",
                    json={
                        "activity_type": "agent_log",
                        "source_id": WORKSPACE_ID,
                        "target_id": WORKSPACE_ID,
                        "summary": summary[:300],
                        "status": "warning",
                        "method": "context_overflow_autoheal",
                    },
                    headers=auth_headers(),
                )
        except Exception:
            return

    async def _execute_locked(self, user_input: str) -> str:
        """Body of execute() that runs under the run lock.

        Retries transient errors (rate limits, capacity, exit-code-1) up to
        _MAX_RETRIES times with exponential backoff (5s, 10s, 20s).
        """
        # Keep a clean copy of the user's actual message for the memory record,
        # BEFORE any delegation or memory injection.
        original_input = user_input
        logger.debug("SDK execute [claude-code]: %s", user_input[:200])

        prompt = self._prepare_prompt(user_input)

        response_text: str = ""
        tool_uses_for_turn: list[str] = []
        try:
            # set_current_task INSIDE the try so active_tasks is always
            # decremented by the finally block even if CancelledError hits
            # during the heartbeat HTTP push. Moving it outside the try
            # created a narrow window where cancellation left active_tasks
            # stuck at 1 forever, permanently blocking queue drain. (#2026)
            await set_current_task(self.heartbeat, brief_summary(user_input))
            prompt = await self._inject_memories_if_first_turn(prompt)
            # Bound the context-overflow auto-heal to ONE reset per
            # dispatch. The first overflow resets the session and retries
            # on a fresh (empty) session; if THAT immediately overflows
            # again, the prompt itself — not accumulated history — exceeds
            # the window, which a reset cannot fix. Surface a hard error
            # instead of looping the reset forever.
            overflow_healed = False
            for attempt in range(_MAX_RETRIES):
                options = self._build_options()
                try:
                    result = await self._run_query(prompt=prompt, options=options)
                    if result.session_id:
                        self._session_id = result.session_id
                    response_text = result.text
                    tool_uses_for_turn = result.tool_uses
                    break  # success
                except Exception as exc:
                    formatted = _format_process_error(exc)
                    # #75: CLI subprocess crashes leave our _session_id
                    # referencing a session the next subprocess can't
                    # resume. Without this reset the next attempt would
                    # crash identically even when the underlying cause
                    # was transient, cascading into "crashed once →
                    # crashes forever until container restart." Clear
                    # the session_id so the next attempt (retry or
                    # next user turn) starts fresh.
                    self._reset_session_after_error(exc)

                    # --- Context-overflow auto-heal (the Kimi wedge) ---
                    # If the error is a context-window overflow, the
                    # accumulated session transcript has grown past the
                    # model's window: resuming it makes EVERY future
                    # dispatch overflow identically (agent stuck forever
                    # until a manual restart). Heal it in-band: reset the
                    # session (resume=None + purge the bloated transcript)
                    # and retry ONCE on a fresh, empty session.
                    #
                    # Bounded by `overflow_healed`: at most one reset per
                    # dispatch. A second overflow after a fresh-session
                    # reset means the single prompt is itself too large
                    # (not history) — a reset can't fix that, so we fall
                    # through to the terminal error path below.
                    #
                    # Loud + observable per the fail-loud SOP: ERROR-level
                    # structured log on detect, plus a best-effort
                    # operator notification. NOT a runtime wedge — a wedge
                    # means "only a restart recovers"; this self-recovers,
                    # so flipping the workspace to degraded would be a
                    # false alarm.
                    if not overflow_healed and _is_context_overflow(formatted):
                        overflow_healed = True
                        logger.error(
                            "auto-heal: session reset on context-overflow "
                            "[claude-code] (attempt %d/%d) — model=%s, "
                            "resetting session + retrying once on a fresh "
                            "session: %s",
                            attempt + 1, _MAX_RETRIES, self.model,
                            formatted[:200],
                        )
                        self._reset_session_for_context_overflow()
                        await self._notify_context_overflow_heal(formatted)
                        # RE-INJECT durable memory into the fresh session.
                        # `prompt` was built at the top of this dispatch when a
                        # session existed, so `_inject_memories_if_first_turn`
                        # returned it WITHOUT the recalled memory snapshot. The
                        # heal just reset `_session_id` to None, so the retry
                        # runs on a brand-new session — without this re-inject it
                        # starts amnesiac (the "agent forgets everything, even
                        # things I asked it to remember, after an overflow reset"
                        # bug: the durable commit_memory store is never re-fed
                        # into the post-reset session). Rebuild the prompt from
                        # the clean input so the fresh session gets the recalled
                        # memory back. (System-prompt memory snapshot files —
                        # MEMORY.md/CLAUDE.md — already ride along via `options`,
                        # but the dynamic recall prefix does not; this restores
                        # it.)
                        prompt = await self._inject_memories_if_first_turn(
                            self._prepare_prompt(original_input)
                        )
                        # No backoff: the fresh session is independent of
                        # any upstream rate state; retry immediately.
                        continue

                    # --- Stuck-MCP handling ---
                    # The readiness gate and server-reload auto-heal now live
                    # inside `_run_query_gated`: it retries up to
                    # `_MCP_HEAL_MAX_RETRIES` reconnects via
                    # `client.reconnect_mcp_server()` before raising a terminal
                    # `_McpNotReadyError`. If that reaches here, the server is
                    # genuinely broken; fall through to the terminal error path.

                    # A context overflow that survives a heal (overflow_healed
                    # already True) must go straight to the terminal error
                    # path — NOT the transient-retry branch below. The
                    # overflow text ("token limit …") also matches the broad
                    # `_RETRYABLE_PATTERNS` ("limit"), so without this guard
                    # the loop would back-off-and-retry a third time, which
                    # (a) re-overflows identically and (b) defeats the
                    # one-reset-per-dispatch cap. The single prompt is too
                    # big; backoff cannot shrink it.
                    is_unhealable_overflow = _is_context_overflow(formatted)

                    if (
                        not is_unhealable_overflow
                        and attempt < _MAX_RETRIES - 1
                        and self._is_retryable(exc)
                    ):
                        delay = _BASE_RETRY_DELAY_S * (2 ** attempt)
                        logger.warning(
                            "SDK agent [claude-code] transient error (attempt %d/%d), "
                            "retrying in %ds: %s",
                            attempt + 1, _MAX_RETRIES, delay, formatted,
                        )
                        await asyncio.sleep(delay)
                        continue
                    # Non-retryable or exhausted retries. Log exit_code +
                    # stderr explicitly (fixes #66) so operators don't have
                    # to reproduce the crash manually to find out why the
                    # subprocess died.
                    if overflow_healed and _is_context_overflow(formatted):
                        # A context overflow that survived a fresh-session
                        # reset: the single prompt (this one message + its
                        # injected memory/delegation context) is itself
                        # larger than the model's window. A session reset
                        # cannot shrink one oversized request — surface a
                        # hard, operator-actionable error rather than
                        # looping the reset.
                        logger.error(
                            "context-overflow auto-heal exhausted [claude-code]: "
                            "the request still overflows on a FRESH session, so "
                            "the single prompt exceeds the model window "
                            "(model=%s) — not a stale-history problem; shrink the "
                            "input or raise the model's context window: %s",
                            self.model, formatted[:200],
                        )
                    logger.error("SDK agent error [claude-code]: %s", formatted)
                    logger.exception("SDK agent error [claude-code] — full traceback follows")
                    # Detect the specific claude_agent_sdk init-wedge case
                    # so the heartbeat task can flip the workspace to
                    # `degraded`. Match on the lowercased formatted error;
                    # `formatted` is whatever _format_process_error built,
                    # which already includes both the message and the
                    # exception class name.
                    formatted_lc = formatted.lower()
                    # Completion-stall wedge (the platform-agent wedge fix):
                    # `_aiter_with_idle_cap` aborted the turn because the LLM
                    # completion stream went silent for > _COMPLETION_IDLE_CAP_S.
                    # By the time we get here the per-turn run-lock has been held
                    # for the full idle cap, so inbound A2A messages have been
                    # queuing/524-ing the whole time while the workspace still
                    # showed "online" — an invisible wedge. Flag it so the next
                    # heartbeat reports runtime_state="wedged" → the platform
                    # flips the workspace to degraded and surfaces a Restart
                    # hint. A later good turn clears it via
                    # _clear_sdk_wedge_on_success.
                    if "completion stalled" in formatted_lc:
                        _mark_sdk_wedged(
                            f"completion stalled >{_COMPLETION_IDLE_CAP_S}s - restart workspace"
                        )
                    for pat in _WEDGE_ERROR_PATTERNS:
                        if pat in formatted_lc:
                            _mark_sdk_wedged(
                                f"claude_agent_sdk wedge: {formatted[:200]} — restart workspace to recover"
                            )
                            break
                    response_text = sanitize_agent_error(
                        exc=exc, stderr=error_detail_for_external(exc)
                    )
                    break
        finally:
            await set_current_task(self.heartbeat, "")
            await commit_memory(
                f"Conversation: {original_input[:MEMORY_CONTENT_MAX_CHARS]}"
            )
            # Auto-push unpushed commits and open PR (non-blocking, best-effort).
            await auto_push_hook()

        # If the agent produced no text but did call tools, surface a brief
        # summary of which tools were used instead of the bare
        # "(no response generated)" sentinel. Common case: autonomous-tick
        # ticks that only do delegate_task_async / send_message_to_user with
        # no final text block. Canvas users seeing "(no response generated)"
        # under a fired schedule have no signal that work actually happened;
        # the tool list makes that visible.
        if not response_text and tool_uses_for_turn:
            # Order-preserving de-dupe so e.g. 4× TaskCreate collapses to 1.
            seen: set[str] = set()
            unique: list[str] = []
            for name in tool_uses_for_turn:
                if name not in seen:
                    seen.add(name)
                    unique.append(name)
            counts: dict[str, int] = {}
            for name in tool_uses_for_turn:
                counts[name] = counts.get(name, 0) + 1
            tool_summary = ", ".join(
                f"{name}×{counts[name]}" if counts[name] > 1 else name
                for name in unique
            )
            return f"(no text reply — used tools: {tool_summary})"
        return response_text or _NO_RESPONSE_MSG

    async def cancel(self, context: RequestContext, event_queue: EventQueue):
        """Cooperatively cancel the currently running turn.

        cancel() targets whatever turn is in flight *right now*, not the
        specific turn the caller may have been looking at when they sent
        the cancel request. If turn A has finished and turn B is already
        running under the run lock by the time cancel arrives, turn B is
        the one that gets aborted. This matches how a "stop" button in a
        chat UI typically behaves (stop whatever is running) and is a
        conscious trade-off against per-turn bookkeeping.

        Implementation: the SDK's query() is an async generator; calling
        aclose() raises GeneratorExit inside the running turn and unwinds
        cleanly. We read `self._active_stream` into a local BEFORE calling
        aclose so the reference can't be reassigned by another turn
        mid-cancel. Best-effort — if no stream is active (cancel arrived
        between turns, or the stream has no aclose), this is a no-op.
        """
        stream = self._active_stream
        if stream is None:
            return
        aclose = getattr(stream, "aclose", None)
        if aclose is None:
            return
        try:
            await aclose()
        except Exception:
            logger.exception("SDK cancel: aclose() raised")
