"""
MOLECULE-HOTFIX: claude-code 2.1.150 emits a Result message with is_error=True
AND subtype="success" on benign completions. The claude-agent-sdk's
client.py:304-307 then sets _last_error_result_text="success", which the SDK
later synthesizes into a {"type":"error", "error":"Claude Code returned an
error result: success"} message that query.py:852 raises as an Exception.

Net effect on workspace agents (PM, engineer-A/B, anything claude-code runtime):
every other dispatch fails with the misleading message "Exception: Claude Code
returned an error result: success" despite the underlying turn being fine.

This script edits the installed SDK in place to skip the raise when the error
string ends with ": success". Runs at Docker build time so every new image
ships the workaround. Idempotent: safe to re-run.

If the upstream SDK is updated to a version that no longer contains the
original block, this script fails the build with a clear error so we know to
remove the workaround.
"""
import pathlib
import sys

TARGET = pathlib.Path(
    "/usr/local/lib/python3.11/site-packages/claude_agent_sdk/_internal/query.py"
)

ORIG = (
    '            elif message.get("type") == "error":\n'
    '                raise Exception(message.get("error", "Unknown error"))'
)
PATCHED = (
    '            elif message.get("type") == "error":\n'
    '                _err = message.get("error", "Unknown error")\n'
    '                # MOLECULE-HOTFIX: claude-code 2.1.150 emits is_error=True with\n'
    '                # subtype=success on benign results; SDK synthesizes a type=error\n'
    '                # message with body "...: success". Treat exactly that as end-of-stream.\n'
    '                if isinstance(_err, str) and _err.endswith(": success"):\n'
    '                    break\n'
    '                raise Exception(_err)'
)


def main() -> int:
    if not TARGET.exists():
        print(f"FAIL: {TARGET} does not exist", file=sys.stderr)
        return 2
    src = TARGET.read_text()
    if PATCHED in src:
        print("already_patched=true")
        return 0
    if ORIG in src:
        TARGET.write_text(src.replace(ORIG, PATCHED))
        print("patched=true")
        return 0
    print(
        "FAIL: original SDK block not found. The upstream claude-agent-sdk may\n"
        "have been updated. Re-evaluate the workaround and either remove this\n"
        "script (if the upstream bug is fixed) or update the ORIG/PATCHED\n"
        "strings to match the new block.",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    sys.exit(main())
