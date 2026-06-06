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
    extract_attached_files,
    extract_message_text,
    get_a2a_instructions,
    get_display_instructions,
    get_hma_instructions,
    get_mcp_server_path,
    get_system_prompt,
    read_delegation_results,
    recall_memories,
    sanitize_agent_error,
    set_current_task,
)

if TYPE_CHECKING:
    from molecule_runtime.heartbeat import HeartbeatLoop

logger = logging.getLogger(__name__)

_NO_TEXT_MSG = "Error: message contained no text content."
_NO_RESPONSE_MSG = "(no response generated)"
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
#      argv → SDK init wedge) BEFORE the broken image ships to GHCR.
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
    ):
        self.system_prompt = system_prompt
        self.config_path = config_path
        self.heartbeat = heartbeat
        self.model = model
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
        """Compose system prompt from file + A2A + HMA memory instructions."""
        base = get_system_prompt(self.config_path, fallback=self.system_prompt)
        a2a = get_a2a_instructions(mcp=True)
        display = get_display_instructions()
        hma = get_hma_instructions()
        parts = [p for p in (base, a2a, display, hma) if p]
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

        mcp_servers = {
            "a2a": {
                "command": sys.executable,
                "args": [get_mcp_server_path()],
            }
        }

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

    async def _run_query(self, prompt: str, options: Any) -> QueryResult:
        """Drive the SDK query stream and return a QueryResult.

        Prefers ResultMessage.result (the canonical final text — same field
        the CLI's --output-format json used) and only falls back to the
        concatenation of AssistantMessage TextBlocks when result is absent.
        Otherwise pre-tool reasoning and post-tool summary get double-emitted.

        Pure: does not mutate executor state other than setting / clearing
        `self._active_stream` so cancel() can reach in. The caller decides
        whether to persist the returned session_id.
        """
        assistant_chunks: list[str] = []
        tool_uses: list[str] = []
        result_text: str | None = None
        session_id: str | None = None
        result_is_error: bool = False
        self._active_stream = sdk.query(prompt=prompt, options=options)
        try:
            async for message in self._active_stream:
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
        finally:
            self._active_stream = None
        # An is_error result is an upstream rejection (e.g. a 400 context
        # overflow from the model proxy) that the SDK surfaced as a normal
        # terminal message instead of a raised exception. Re-raise it so it
        # flows through the same retry/heal/wedge classification in
        # _execute_locked as a raised SDK error — detection lives in one
        # place. The session_id (if any) was NOT persisted to self by this
        # method; the caller's heal path clears it regardless.
        if result_is_error:
            raise _ResultError(result_text or "")
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
        # identical across hermes / langgraph / claude-code.
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
                        # No backoff: the fresh session is independent of
                        # any upstream rate state; retry immediately.
                        continue

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
                    for pat in _WEDGE_ERROR_PATTERNS:
                        if pat in formatted_lc:
                            _mark_sdk_wedged(
                                f"claude_agent_sdk wedge: {formatted[:200]} — restart workspace to recover"
                            )
                            break
                    response_text = sanitize_agent_error(exc)
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
