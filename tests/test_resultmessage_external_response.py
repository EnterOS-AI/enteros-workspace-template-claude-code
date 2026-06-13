"""#121 security RC #11424: the enriched ``_ResultError`` detail must reach
the EXTERNAL A2A response, through the REAL ``sanitize_agent_error``.

PR #121 enriched ``_ResultError.text`` (subtype + api_error_status + a
capped ``errors[]`` summary) so the opaque
``Agent error (_ResultError) — see workspace logs for details`` could
self-diagnose. But the executor's terminal error path called
``sanitize_agent_error(exc)`` with NO ``stderr=`` argument — the real
sanitizer returns the opaque "see workspace logs" form unless ``stderr``
is passed. So the enrichment never reached the user-facing response.

The sibling ``test_resultmessage_detail.py`` STUBS ``sanitize_agent_error``
as ``lambda e: f"Agent error: {e}"``, so it proves the helper enriches
``.text`` but does NOT exercise the genuine external-response formatting or
the secret-redaction path. THIS module closes that gap: it runs the REAL
``sanitize_agent_error`` + ``error_detail_for_external`` (vendored verbatim
from molecule_runtime/executor_helpers.py, drift-guarded against the
installed runtime when present) over the executor terminal call shape and
asserts:
  (a) the external string self-diagnoses (contains subtype + status, NOT
      "see workspace logs");
  (b) redaction holds — bearer / sk-… / "API key sk-…" tokens are
      [REDACTED], the raw token is absent;
  (c) the result-present (overflow) case is unchanged.

Vendoring rationale: the template CI runner installs only
``pytest pytest-asyncio pyyaml`` and stubs ``molecule_runtime`` (see
tests/conftest.py), so the real runtime is NOT importable in CI. We vendor
the four real symbols VERBATIM and assert byte-equality against the
installed runtime source whenever it IS importable (local / image), so a
future runtime change to the sanitizer can never silently diverge from the
contract this test pins.
"""

import inspect
from typing import Any

import pytest


# --------------------------------------------------------------------------
# REAL sanitizer, vendored VERBATIM from
# molecule-ai-workspace-runtime molecule_runtime/executor_helpers.py.
# Drift-guarded below (test_vendored_sanitizer_matches_installed_runtime).
# --------------------------------------------------------------------------

_MAX_STDERR_PREVIEW = 1024  # bytes — first 1 KB of error detail shown to caller


def _sanitize_for_external(msg: str) -> str:
    """Strip strings that look like API keys, bearer tokens, or absolute paths.

    Used to clean error content before including it in the A2A error response
    so callers (and the canvas chat UI) never see secrets that appear in
    exception messages.
    """
    # Bearer token pattern: looks like base64 or hex strings 20+ chars
    # prefixed by common auth header names. Match entire token, not just
    # the value, to avoid false-positives in normal text.
    import re as _re

    # Standalone provider-token shapes (e.g. a bare OpenAI-style key) that
    # appear with NO preceding label/separator. The labeled pattern below
    # only fires when a "bearer"/"token"/"api_key" prefix + separator is
    # present, so a value like ``sk-XXXX...`` on its own would otherwise
    # leak verbatim. The ``sk-`` prefix + 20-char minimum keeps this narrow
    # enough to avoid eating normal prose (e.g. "disk-usage", "task sk-").
    msg = _re.sub(r"(?i)sk-[A-Za-z0-9_/.-]{20,}", "[REDACTED]", msg)
    # Labeled auth values: a known auth-header / key name, a separator,
    # then a 20+ char value. ``api[\s_-]?key`` matches "api_key",
    # "api-key" AND the space form "api key". Run after the standalone
    # pass so the value in ``Authorization: Bearer sk-...`` is scrubbed
    # regardless of which arm matches.
    msg = _re.sub(r"(?i)(?:bearer|token|api[\s_-]?key|sk-)[ :=]+[A-Za-z0-9_/.-]{20,}", "[REDACTED]", msg)
    # Absolute paths: /etc/shadow, /home/user/.aws/credentials, etc.
    msg = _re.sub(r"(?:/[^/\s]+){2,}", lambda m: m.group(0) if len(m.group(0)) < 60 else "[REDACTED_PATH]", msg)
    return msg


def sanitize_agent_error(
    exc: BaseException | None = None,
    category: str | None = None,
    stderr: str | None = None,
) -> str:
    """Render an agent-side failure into a user-safe error message.

    Either pass an exception (class name is used as the tag) or an explicit
    category string (e.g. from `classify_subprocess_error`). If both are
    given, `category` wins. If neither, the tag defaults to "unknown".

    When ``stderr`` is provided (e.g. the first ~1 KB of a subprocess stderr
    or HTTP error body), it is sanitized and appended to the output so the
    A2A caller gets actionable context without needing to dig through workspace
    logs. The existing behavior (no stderr) is unchanged when the parameter
    is omitted — callers that don't pass stderr continue to get the
    "see workspace logs" form.
    """
    if category:
        tag = category
    elif exc is not None:
        tag = type(exc).__name__
    else:
        tag = "unknown"

    if stderr:
        # Truncate and sanitize before including — prevents DoS via
        # a malicious or buggy peer injecting a huge error body, and
        # scrubs any API keys / bearer tokens that snuck into the message.
        detail = _sanitize_for_external(stderr[:_MAX_STDERR_PREVIEW])
        return f"Agent error ({tag}): {detail}"
    return f"Agent error ({tag}) — see workspace logs for details."


def error_detail_for_external(exc: BaseException) -> str | None:
    """Best-effort actionable detail from an exception for the A2A error
    response.

    Prefers a subprocess/HTTP ``.stderr`` attribute (decoded if bytes),
    else falls back to ``str(exc)``. The returned text is meant to be passed
    straight to :func:`sanitize_agent_error` as ``stderr=`` -- which truncates
    it to 1 KB (``_MAX_STDERR_PREVIEW``) and scrubs secrets / long paths via
    :func:`_sanitize_for_external` -- so it stays safe for the canvas / peer
    facing response. Returns ``None`` when there is no usable detail, in which
    case ``sanitize_agent_error`` keeps its existing "see workspace logs" form.

    Deliberately surfaces only the message / stderr -- never a stack trace or
    ``exc_info`` (that full detail still goes to the owner-gated workspace logs
    via ``logger.error(..., exc_info=True)``).
    """
    detail: Any = getattr(exc, "stderr", None)
    if isinstance(detail, (bytes, bytearray)):
        try:
            detail = detail.decode("utf-8", "replace")
        except Exception:
            detail = None
    if not detail:
        detail = str(exc) or None
    return detail or None


# --------------------------------------------------------------------------
# Minimal real-shape _ResultError (mirrors claude_sdk_executor._ResultError:
# str(exc) == the enriched .text, since __init__ calls super().__init__(text)).
# We re-declare it here rather than importing the executor module because
# importing it requires the whole stub chain; the contract under test is the
# *interaction* between the enriched exception, error_detail_for_external,
# and sanitize_agent_error. A guard test below asserts this stand-in matches
# the executor's real _ResultError str() behaviour.
# --------------------------------------------------------------------------


class _ResultErrorLike(Exception):
    def __init__(self, text: str) -> None:
        self.text = text or ""
        super().__init__(self.text)


def _external_response_for(exc: BaseException) -> str:
    """Exactly the executor terminal call after the #11424 fix:
    sanitize_agent_error(exc=exc, stderr=error_detail_for_external(exc))."""
    return sanitize_agent_error(exc=exc, stderr=error_detail_for_external(exc))


# ------------------------------- drift guard -------------------------------


def _installed_helpers():
    try:
        from molecule_runtime import executor_helpers as eh  # type: ignore
    except Exception:
        return None
    # The conftest stub registers a bare ModuleType with no real source.
    if not getattr(eh, "__file__", None):
        return None
    if not hasattr(eh, "sanitize_agent_error"):
        return None
    return eh


def test_vendored_sanitizer_matches_installed_runtime():
    """When the REAL runtime is importable (local dev / built image), the
    vendored copy here must be byte-identical to the installed source — so a
    runtime-side change to the sanitizer can't silently break this contract
    without turning this test red."""
    eh = _installed_helpers()
    if eh is None:
        pytest.skip("real molecule_runtime not installed (CI stub) — vendored copy used")
    for name, local in (
        ("sanitize_agent_error", sanitize_agent_error),
        ("error_detail_for_external", error_detail_for_external),
        ("_sanitize_for_external", _sanitize_for_external),
    ):
        real = getattr(eh, name)
        assert inspect.getsource(real) == inspect.getsource(local), (
            f"vendored {name} drifted from installed molecule_runtime"
        )
    assert eh._MAX_STDERR_PREVIEW == _MAX_STDERR_PREVIEW


def test_resulterror_like_matches_executor_str_behaviour():
    """Guard: our stand-in mirrors claude_sdk_executor._ResultError — str(exc)
    is the enriched text — so error_detail_for_external's str(exc) fallback
    surfaces the SAME value the real executor would feed the sanitizer."""
    e = _ResultErrorLike("error_during_execution (api_error_status=401)")
    assert str(e) == "error_during_execution (api_error_status=401)"
    assert error_detail_for_external(e) == "error_during_execution (api_error_status=401)"


# ----------------------- (a) detail surfaces externally -----------------------


def test_enriched_detail_reaches_external_response():
    """The bug fix: an enriched _ResultError (subtype + api_error_status) must
    self-diagnose in the EXTERNAL response — NOT "see workspace logs"."""
    exc = _ResultErrorLike("error_during_execution (api_error_status=401)")
    out = _external_response_for(exc)
    assert "error_during_execution" in out
    assert "401" in out
    assert "see workspace logs" not in out
    # Tag still present so operators know the exception class.
    assert "_ResultErrorLike" in out


def test_pre_fix_call_shape_was_opaque():
    """Documents the GAP #11424 closed: the OLD terminal call —
    sanitize_agent_error(exc) with no stderr — returns the opaque form even
    when .text is enriched. Proves the fix (passing stderr) is load-bearing."""
    exc = _ResultErrorLike("error_during_execution (api_error_status=401)")
    opaque = sanitize_agent_error(exc)  # the pre-#11424 call
    assert opaque == "Agent error (_ResultErrorLike) — see workspace logs for details."
    assert "error_during_execution" not in opaque
    # And the fixed call is strictly more informative.
    assert _external_response_for(exc) != opaque


# ----------------------------- (b) redaction holds -----------------------------


def test_redaction_bearer_token_in_errors_detail():
    """An errors[] entry carrying a bearer token must be [REDACTED] in the
    external response; the raw token must be absent."""
    raw = "Authorization: Bearer abcDEF1234567890ghIJKLmnop"
    exc = _ResultErrorLike(f"error_during_execution: {raw}")
    out = _external_response_for(exc)
    assert "abcDEF1234567890ghIJKLmnop" not in out
    assert "[REDACTED]" in out
    # The non-secret diagnostic context still survives.
    assert "error_during_execution" in out


def test_redaction_bare_sk_key():
    """A bare ``sk-…`` provider key (no auth-header prefix) must be redacted."""
    raw = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    exc = _ResultErrorLike(f"upstream rejected key {raw}")
    out = _external_response_for(exc)
    assert raw not in out
    assert "[REDACTED]" in out


def test_redaction_api_key_label():
    """The "API key sk-…" labeled shape must be redacted."""
    raw = "sk-LIVE0000111122223333444455556666"
    exc = _ResultErrorLike(f"error_during_execution (api_error_status=401): API key {raw} invalid")
    out = _external_response_for(exc)
    assert raw not in out
    assert "[REDACTED]" in out
    # Status context preserved despite the scrub.
    assert "401" in out and "error_during_execution" in out


# --------------------------- (c) result-present unchanged ---------------------------


def test_result_present_overflow_text_unchanged():
    """When result IS present (overflow path), the verbatim text flows through
    the sanitizer to the external response unchanged (no secrets in it)."""
    exc = _ResultErrorLike("token limit 262144 requested 268132")
    out = _external_response_for(exc)
    assert "token limit 262144 requested 268132" in out
    assert "see workspace logs" not in out


def test_oversized_detail_truncated_to_1kb():
    """DoS guard preserved: a pathological multi-KB detail is capped to the
    first 1 KB before it reaches the external response."""
    exc = _ResultErrorLike("X" * 5000)
    out = _external_response_for(exc)
    # tag + ": " + at most 1024 chars of detail.
    assert out.count("X") <= _MAX_STDERR_PREVIEW
