"""Pin _mark_sdk_wedged + _clear_sdk_wedge_on_success mirror into
molecule_runtime.runtime_wedge.

The local _sdk_wedged_reason flag (module-level in claude_sdk_executor)
must be mirrored into the universal runtime_wedge module so two
consumers can observe the wedge:

  1. Heartbeat (workspace/heartbeat.py:_runtime_state_payload) — flips
     workspace status to `degraded` on the canvas. WITHOUT the mirror,
     a wedged workspace stays green-dot while every chat hangs.

  2. Boot smoke (workspace/smoke_mode.py:run_executor_smoke) — task
     #131. Catches PR-25-class regressions (malformed CLI argv → SDK
     init wedge) BEFORE the broken image ships to GHCR. WITHOUT the
     mirror, the smoke sees the outer wait_for time out and reports
     PASS even though the runtime self-reported wedged.

Stubs molecule_runtime.runtime_wedge as a recorder, then asserts the
mirror calls land. Regression-injection-checked: deleting either of
the new try/except blocks in _mark_sdk_wedged / _clear_sdk_wedge_on_success
makes these tests fail with a clear message naming the missing call.
"""

import os
import sys
import types
from unittest.mock import MagicMock


# ---- Stubs ----
#
# claude_sdk_executor.py imports a tall stack at module load. We
# replace each with the minimum surface needed so the test file runs
# in CI without the real packages installed. Patterns mirror
# test_dev_channels_flag.py — same _ensure_module/_ensure_attr
# helpers so a real-package install on workstation still wins over
# the stubs.


def _ensure_module(dotted: str) -> types.ModuleType:
    if dotted not in sys.modules:
        sys.modules[dotted] = types.ModuleType(dotted)
    return sys.modules[dotted]


def _ensure_attr(mod: types.ModuleType, name: str, value: object) -> None:
    if not hasattr(mod, name):
        setattr(mod, name, value)


def _install_executor_stubs():
    """Mirror of test_dev_channels_flag._install_stubs — same surface."""
    sdk = _ensure_module("claude_agent_sdk")
    _ensure_attr(sdk, "ClaudeAgentOptions", MagicMock(name="ClaudeAgentOptions"))
    _ensure_attr(sdk, "AssistantMessage", type("AssistantMessage", (), {}))
    _ensure_attr(sdk, "TextBlock", type("TextBlock", (), {}))
    _ensure_attr(sdk, "ResultMessage", type("ResultMessage", (), {}))
    _ensure_attr(sdk, "query", MagicMock(name="query"))

    _ensure_module("a2a")
    _ensure_module("a2a.server")
    a2a_exec = _ensure_module("a2a.server.agent_execution")
    _ensure_attr(a2a_exec, "AgentExecutor", type("AgentExecutor", (), {}))
    _ensure_attr(a2a_exec, "RequestContext", type("RequestContext", (), {}))
    a2a_events = _ensure_module("a2a.server.events")
    _ensure_attr(a2a_events, "EventQueue", type("EventQueue", (), {}))
    a2a_helpers = _ensure_module("a2a.helpers")
    _ensure_attr(a2a_helpers, "new_text_message", lambda *_a, **_kw: None)

    _ensure_module("molecule_runtime")
    helpers = _ensure_module("molecule_runtime.executor_helpers")
    _ensure_attr(helpers, "CONFIG_MOUNT", "/configs")
    _ensure_attr(helpers, "WORKSPACE_MOUNT", "/workspace")
    _ensure_attr(helpers, "MEMORY_CONTENT_MAX_CHARS", 10000)
    _ensure_attr(helpers, "auto_push_hook", lambda *a, **kw: None)
    _ensure_attr(helpers, "brief_summary", lambda *a, **kw: "")
    _ensure_attr(helpers, "collect_outbound_files", lambda *a, **kw: [])
    _ensure_attr(helpers, "commit_memory", lambda *a, **kw: None)
    _ensure_attr(helpers, "extract_attached_files", lambda *a, **kw: [])
    _ensure_attr(helpers, "extract_message_text", lambda *a, **kw: "")
    _ensure_attr(helpers, "get_a2a_instructions", lambda **kw: "")
    _ensure_attr(helpers, "get_display_instructions", lambda *a, **kw: "")
    _ensure_attr(helpers, "get_hma_instructions", lambda *a, **kw: "")
    _ensure_attr(helpers, "get_mcp_server_path", lambda *a, **kw: "/dev/null")
    _ensure_attr(helpers, "get_system_prompt", lambda *a, **kw: "")
    _ensure_attr(helpers, "read_delegation_results", lambda *a, **kw: "")
    _ensure_attr(helpers, "recall_memories", lambda *a, **kw: "")
    _ensure_attr(helpers, "sanitize_agent_error", lambda e: str(e))
    _ensure_attr(helpers, "set_current_task", lambda *a, **kw: None)


def _install_runtime_wedge_recorder() -> dict:
    """Replace molecule_runtime.runtime_wedge with a recorder that
    captures every (mark_wedged|clear_wedge) call. Returns the recorder
    dict so tests can assert on it. Forces a fresh module each time so
    state from a previous test doesn't bleed in."""
    rec = {"mark_calls": [], "clear_calls": 0}
    mod = types.ModuleType("molecule_runtime.runtime_wedge")

    def _mark(reason: str) -> None:
        rec["mark_calls"].append(reason)

    def _clear() -> None:
        rec["clear_calls"] += 1

    mod.mark_wedged = _mark
    mod.clear_wedge = _clear
    sys.modules["molecule_runtime.runtime_wedge"] = mod
    return rec


def _load_executor():
    """Re-import claude_sdk_executor with fresh stubs."""
    _install_executor_stubs()
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    sys.modules.pop("claude_sdk_executor", None)
    import claude_sdk_executor  # noqa: WPS433
    return claude_sdk_executor


# ─── Mirror tests ─────────────────────────────────────────────────────


def test_mark_sdk_wedged_mirrors_into_runtime_wedge():
    """_mark_sdk_wedged must call runtime_wedge.mark_wedged with the
    same reason. Heartbeat reads runtime_wedge — without this mirror
    the canvas keeps showing green-dot while every chat hangs."""
    rec = _install_runtime_wedge_recorder()
    mod = _load_executor()
    mod._reset_sdk_wedge_for_test()

    mod._mark_sdk_wedged("claude SDK init timeout — restart workspace")

    assert rec["mark_calls"] == [
        "claude SDK init timeout — restart workspace",
    ], (
        "_mark_sdk_wedged did not mirror into runtime_wedge.mark_wedged. "
        "Heartbeat + smoke_mode (#131) both observe the universal flag — "
        "without the mirror, a wedged workspace looks healthy to both."
    )
    # Local flag should still be set — mirror is additive, not a replacement.
    assert mod.is_wedged() is True
    assert mod.wedge_reason() == "claude SDK init timeout — restart workspace"


def test_mark_sdk_wedged_first_call_wins_for_mirror_too():
    """The local flag has first-wins semantics so a transient secondary
    wedge can't overwrite a more specific initial reason. The mirror
    must follow the same rule — otherwise heartbeat banner text could
    flip mid-incident."""
    rec = _install_runtime_wedge_recorder()
    mod = _load_executor()
    mod._reset_sdk_wedge_for_test()

    mod._mark_sdk_wedged("specific initial reason — restart workspace")
    mod._mark_sdk_wedged("generic later reason")

    assert rec["mark_calls"] == ["specific initial reason — restart workspace"], (
        "Mirror fired more than once across repeated _mark_sdk_wedged calls. "
        "Local flag has first-wins; mirror must too, or the canvas banner "
        "and smoke gate will see the wrong reason."
    )


def test_clear_sdk_wedge_on_success_mirrors_into_runtime_wedge():
    """Clear must propagate too — otherwise a transient wedge that the
    next successful turn would clear locally would leave the universal
    flag latched, and the workspace would stay degraded forever
    (heartbeat would never report runtime_state empty)."""
    rec = _install_runtime_wedge_recorder()
    mod = _load_executor()
    mod._reset_sdk_wedge_for_test()
    mod._mark_sdk_wedged("transient blip")

    mod._clear_sdk_wedge_on_success()

    assert rec["clear_calls"] == 1, (
        "_clear_sdk_wedge_on_success did not mirror into runtime_wedge.clear_wedge. "
        "Local clear without mirror = workspace stays degraded forever after "
        "an observed-success recovery."
    )
    assert mod.is_wedged() is False


def test_clear_when_not_wedged_does_not_call_runtime_wedge():
    """No-op symmetry: if local flag wasn't set, the mirror must not
    fire either. Avoids clearing a wedge that some OTHER adapter set
    in the same process (forward-cover for the future per-org
    multi-executor design hinted at in the module docstring)."""
    rec = _install_runtime_wedge_recorder()
    mod = _load_executor()
    mod._reset_sdk_wedge_for_test()

    mod._clear_sdk_wedge_on_success()

    assert rec["clear_calls"] == 0, (
        "_clear_sdk_wedge_on_success fired the mirror even though the "
        "local flag wasn't set — would stomp on a peer adapter's wedge "
        "in a multi-executor setup."
    )


def test_mirror_swallows_runtime_wedge_import_error():
    """Older runtime versions (pre-task-#131 wheel) don't ship
    runtime_wedge. The mirror call must swallow ImportError so a
    template pinned to an older runtime keeps booting — the local
    sticky flag still gates is_wedged() inside this module so the
    retry loop / cancel handler keep working."""
    # Install all the executor stubs then explicitly REMOVE the
    # runtime_wedge submodule so the import inside _mark_sdk_wedged
    # raises ImportError.
    _install_executor_stubs()
    sys.modules.pop("molecule_runtime.runtime_wedge", None)

    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    sys.modules.pop("claude_sdk_executor", None)
    import claude_sdk_executor as mod  # noqa: WPS433
    mod._reset_sdk_wedge_for_test()

    # Should not raise even though runtime_wedge import will fail.
    mod._mark_sdk_wedged("init timeout")
    assert mod.is_wedged() is True
    assert mod.wedge_reason() == "init timeout"

    # Clear path also swallows.
    mod._clear_sdk_wedge_on_success()
    assert mod.is_wedged() is False


# ─── Mirror-call-failure injection (review follow-up) ──────────────────
#
# The recorder above never raises, so the inner `try` arm around
# `_mark_runtime_wedged(reason)` (and the symmetric clear) wasn't
# pinned by the original mirror tests. Inject a recorder whose
# call-side raises so the catch arm is exercised: the mirror failure
# must be logged but must NOT suppress the local sticky flag.


def _install_runtime_wedge_raising_recorder() -> dict:
    """Replace molecule_runtime.runtime_wedge with a recorder whose
    mark_wedged + clear_wedge implementations RAISE on call (not on
    import). Captures the call-attempt count so the test can verify
    the catch arm fired without leaking the exception. Returns the
    recorder dict (mark_attempts, clear_attempts)."""
    rec = {"mark_attempts": 0, "clear_attempts": 0}
    mod = types.ModuleType("molecule_runtime.runtime_wedge")

    def _mark(_reason: str) -> None:
        rec["mark_attempts"] += 1
        raise RuntimeError("simulated runtime_wedge.mark_wedged internal raise")

    def _clear() -> None:
        rec["clear_attempts"] += 1
        raise RuntimeError("simulated runtime_wedge.clear_wedge internal raise")

    mod.mark_wedged = _mark
    mod.clear_wedge = _clear
    sys.modules["molecule_runtime.runtime_wedge"] = mod
    return rec


def test_mark_sdk_wedged_swallows_mirror_call_exception(caplog):
    """If runtime_wedge.mark_wedged itself raises (signature is fine,
    body has a bug), the caller in claude_sdk_executor must log AND
    keep the local sticky flag set. Otherwise an internal regression
    in runtime_wedge would silently make this workspace appear healthy
    while every chat actually hangs.
    """
    import logging
    rec = _install_runtime_wedge_raising_recorder()
    mod = _load_executor()
    mod._reset_sdk_wedge_for_test()

    with caplog.at_level(logging.ERROR, logger="claude_sdk_executor"):
        mod._mark_sdk_wedged("local-and-mirror reason")

    assert rec["mark_attempts"] == 1, (
        "executor never called runtime_wedge.mark_wedged — the inner "
        "try block was skipped or short-circuited"
    )
    assert mod.is_wedged() is True, (
        "mirror-call exception suppressed the local sticky flag — "
        "violates the 'mirror is best-effort, local is source of truth' "
        "contract"
    )
    assert mod.wedge_reason() == "local-and-mirror reason"
    # Loud log line is the only operator-visible signal that the mirror
    # silently failed — pin its presence so a future logger.exception →
    # logger.debug downgrade can't sneak through.
    assert any(
        "runtime_wedge.mark_wedged mirror failed" in r.message
        for r in caplog.records
    ), "mirror-call failure was not logged at ERROR — operator can't see the regression"


def test_clear_sdk_wedge_on_success_swallows_mirror_call_exception(caplog):
    """Symmetric to the mark test: a runtime_wedge.clear_wedge bug
    must not leave the local flag stuck-on (which would make
    auto-recovery silently broken even though the SDK started working
    again)."""
    import logging
    rec = _install_runtime_wedge_raising_recorder()
    mod = _load_executor()
    mod._reset_sdk_wedge_for_test()
    mod._mark_sdk_wedged("transient")
    # Mark also raised but local flag is set — that's the precondition.
    assert mod.is_wedged() is True
    rec["mark_attempts"] = 0  # only count the clear attempt below

    with caplog.at_level(logging.ERROR, logger="claude_sdk_executor"):
        mod._clear_sdk_wedge_on_success()

    assert rec["clear_attempts"] == 1, (
        "executor never called runtime_wedge.clear_wedge — inner try "
        "block was skipped"
    )
    assert mod.is_wedged() is False, (
        "mirror clear-call exception left the local sticky flag set — "
        "auto-recovery is silently broken"
    )
    assert any(
        "runtime_wedge.clear_wedge mirror failed" in r.message
        for r in caplog.records
    ), "clear mirror-call failure was not logged at ERROR"
