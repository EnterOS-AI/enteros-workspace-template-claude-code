#!/usr/bin/env bash
# tests/test_platform_agent_entrypoint.sh
#
# Container-runnable unit test for scripts/platform-agent-entrypoint.sh's
# per-file /opt→/configs reconcile (core#2919 risk-1+2).
#
# Strategy: extract the per-file-copy body from the wrapper, rewrite
# SRC + DST to point at a per-test sandbox, source the patched copy,
# and assert on the resulting /configs state. The trailing
# `exec /entrypoint.sh "$@"` is replaced with a no-op echo so the
# test doesn't try to start molecule-runtime.
#
# Coverage:
#   T1: /opt absent (image built without template content) → no-op
#   T2: /configs empty + /opt fully populated → all files copied
#   T3: /configs partial (only mcp_servers.yaml) → only config.yaml +
#       prompts/ filled; mcp_servers.yaml NOT overwritten (augment,
#       not strip — the linchpin)
#   T4: /configs fully populated + /opt fully populated → no-op
#       (delivered files are never touched)
#   T5: prompts/ subdirectory — a file in /opt/prompts NOT in
#       /configs/prompts gets filled; a file in /configs/prompts
#       NOT in /opt/prompts is untouched (delivered wins)
#
# Runs:
#   bash tests/test_platform_agent_entrypoint.sh
#
# Exit 0 = all tests pass. Non-zero = at least one assertion failed.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WRAPPER="$SCRIPT_DIR/../scripts/platform-agent-entrypoint.sh"

if [ ! -f "$WRAPPER" ]; then
    echo "FAIL: wrapper not found at $WRAPPER" >&2
    exit 1
fi

# extract_per_file_body rewrites the wrapper's SRC + DST constants to
# the test sandbox and replaces the final `exec /entrypoint.sh "$@"`
# with a no-op echo, so sourcing the result runs the reconcile but
# doesn't try to start molecule-runtime.
extract_per_file_body() {
    local src="$1" dst="$2"
    sed -e "s|^SRC=\"/opt/molecule-platform-agent-template\"|SRC=\"${src}\"|" \
        -e "s|^DST=\"/configs\"|DST=\"${dst}\"|" \
        -e 's|^exec /entrypoint.sh "\$@"|echo "[test-stub] would exec /entrypoint.sh" >\&2|' \
        "$WRAPPER"
}

# make_sandbox sets up a fresh /opt + /configs in a temp dir, with
# /opt populated by default (or empty if opt_empty=true), and
# /configs populated by passed-in files (key=value list).
# Returns the sandbox path on stdout (the ONLY stdout — every
# diagnostic goes to stderr so the caller's `$(...)` capture is
# clean).
make_sandbox() {
    local opt_empty="$1"; shift
    local sb
    sb="$(mktemp -d)"
    mkdir -p "$sb/opt/molecule-platform-agent-template/prompts" "$sb/configs/prompts"

    if [ "$opt_empty" != "true" ]; then
        cat > "$sb/opt/molecule-platform-agent-template/config.yaml" <<'EOF'
model: moonshot/kimi-k2.6
runtime: claude-code
EOF
        cat > "$sb/opt/molecule-platform-agent-template/mcp_servers.yaml" <<'EOF'
mcp_servers:
  - name: platform
    command: molecule-platform-mcp
EOF
        cat > "$sb/opt/molecule-platform-agent-template/prompts/concierge.md" <<'EOF'
You are the Org Concierge. Be helpful.
EOF
        cat > "$sb/opt/molecule-platform-agent-template/prompts/extra.md" <<'EOF'
Extra prompt from /opt
EOF
    fi

    # Any caller-supplied /configs files (key=value, key=path)
    for kv in "$@"; do
        local key="${kv%%=*}"
        local val="${kv#*=}"
        mkdir -p "$(dirname "$sb/configs/$key")"
        echo "$val" > "$sb/configs/$key"
    done

    printf '%s' "$sb"
}

assert_file_content() {
    local path="$1" want="$2" label="$3"
    if [ ! -f "$path" ]; then
        echo "FAIL [$label]: $path does not exist" >&2
        return 1
    fi
    local got
    got="$(cat "$path")"
    if [ "$got" != "$want" ]; then
        echo "FAIL [$label]: $path" >&2
        echo "  want: $want" >&2
        echo "  got:  $got" >&2
        return 1
    fi
}

assert_file_absent() {
    local path="$1" label="$2"
    if [ -e "$path" ]; then
        echo "FAIL [$label]: $path should be absent (it was created)" >&2
        return 1
    fi
}

# T1: /opt absent — no-op, no copies, no errors.
t1() {
    local sb
    sb="$(make_sandbox true)"
    local patched
    patched="$(mktemp)"
    extract_per_file_body "$sb/opt/molecule-platform-agent-template" "$sb/configs" > "$patched"

    # shellcheck disable=SC1090
    sh "$patched"
    local rc=$?
    rm -f "$patched"

    assert_file_absent "$sb/configs/config.yaml" "T1: /opt absent → no config.yaml copy"
    assert_file_absent "$sb/configs/mcp_servers.yaml" "T1: /opt absent → no mcp_servers.yaml copy"
    if [ "$rc" -ne 0 ]; then
        echo "FAIL [T1]: wrapper exited non-zero on /opt absent" >&2
        rm -rf "$sb"
        return 1
    fi
    rm -rf "$sb"
    echo "PASS T1: /opt absent → no-op"
}

# T2: /configs empty + /opt fully populated → all files copied.
t2() {
    local sb
    sb="$(make_sandbox false)"
    local patched
    patched="$(mktemp)"
    extract_per_file_body "$sb/opt/molecule-platform-agent-template" "$sb/configs" > "$patched"

    sh "$patched"
    local rc=$?
    rm -f "$patched"

    if [ "$rc" -ne 0 ]; then
        echo "FAIL [T2]: wrapper exited non-zero on empty /configs" >&2
        rm -rf "$sb"
        return 1
    fi
    assert_file_content "$sb/configs/config.yaml" "model: moonshot/kimi-k2.6
runtime: claude-code" "T2: config.yaml filled"
    assert_file_content "$sb/configs/mcp_servers.yaml" "mcp_servers:
  - name: platform
    command: molecule-platform-mcp" "T2: mcp_servers.yaml filled"
    assert_file_content "$sb/configs/prompts/concierge.md" "You are the Org Concierge. Be helpful." "T2: prompts/concierge.md filled"
    rm -rf "$sb"
    echo "PASS T2: empty /configs → all files filled from /opt"
}

# T3: /configs partial — only mcp_servers.yaml present. mcp_servers.yaml
# MUST NOT be overwritten (the linchpin: a partial template delivery
# can't strip the delivered content). config.yaml + prompts/ are
# filled from /opt.
t3() {
    local sb
    sb="$(make_sandbox false "mcp_servers.yaml=delivered-mcp-content")"
    local patched
    patched="$(mktemp)"
    extract_per_file_body "$sb/opt/molecule-platform-agent-template" "$sb/configs" > "$patched"

    sh "$patched"
    local rc=$?
    rm -f "$patched"

    if [ "$rc" -ne 0 ]; then
        echo "FAIL [T3]: wrapper exited non-zero on partial /configs" >&2
        rm -rf "$sb"
        return 1
    fi
    # mcp_servers.yaml: delivered content wins — NOT overwritten
    assert_file_content "$sb/configs/mcp_servers.yaml" "delivered-mcp-content" "T3: mcp_servers.yaml NOT overwritten - augment not strip"
    # config.yaml: absent, so filled from /opt
    assert_file_content "$sb/configs/config.yaml" "model: moonshot/kimi-k2.6
runtime: claude-code" "T3: config.yaml filled from /opt"
    # prompts/concierge.md: absent, so filled from /opt
    assert_file_content "$sb/configs/prompts/concierge.md" "You are the Org Concierge. Be helpful." "T3: prompts/concierge.md filled from /opt"
    rm -rf "$sb"
    echo "PASS T3: partial /configs → delivered preserved, missing filled"
}

# T4: /configs fully populated + /opt fully populated → no-op. Delivered
# files are NEVER touched.
t4() {
    local sb
    sb="$(make_sandbox false "config.yaml=delivered-config" "mcp_servers.yaml=delivered-mcp" "prompts/concierge.md=delivered-concierge-prompt")"
    local patched
    patched="$(mktemp)"
    extract_per_file_body "$sb/opt/molecule-platform-agent-template" "$sb/configs" > "$patched"

    sh "$patched"
    local rc=$?
    rm -f "$patched"

    if [ "$rc" -ne 0 ]; then
        echo "FAIL [T4]: wrapper exited non-zero on fully-populated /configs" >&2
        rm -rf "$sb"
        return 1
    fi
    assert_file_content "$sb/configs/config.yaml" "delivered-config" "T4: config.yaml NOT touched"
    assert_file_content "$sb/configs/mcp_servers.yaml" "delivered-mcp" "T4: mcp_servers.yaml NOT touched"
    assert_file_content "$sb/configs/prompts/concierge.md" "delivered-concierge-prompt" "T4: prompts/concierge.md NOT touched"
    rm -rf "$sb"
    echo "PASS T4: fully-populated /configs - no-op - delivered wins"
}

# T5: prompts/ subdir — a file in /configs/prompts NOT in /opt/prompts
# is left alone (delivered wins). A file in /opt/prompts NOT in
# /configs/prompts is filled. (Confirms the prompts/ loop has the
# same fill-absent-only semantic as the root-level loop.)
t5() {
    local sb
    sb="$(make_sandbox false "prompts/local-only.md=delivered-local-prompt" "prompts/concierge.md=delivered-concierge")"
    local patched
    patched="$(mktemp)"
    extract_per_file_body "$sb/opt/molecule-platform-agent-template" "$sb/configs" > "$patched"

    sh "$patched"
    local rc=$?
    rm -f "$patched"

    if [ "$rc" -ne 0 ]; then
        echo "FAIL [T5]: wrapper exited non-zero on partial prompts/" >&2
        rm -rf "$sb"
        return 1
    fi
    # /configs/prompts/local-only.md: delivered, NOT in /opt, left alone
    assert_file_content "$sb/configs/prompts/local-only.md" "delivered-local-prompt" "T5: delivered prompts/local-only.md preserved - not in /opt"
    # /configs/prompts/concierge.md: delivered, /opt has it, NOT overwritten
    assert_file_content "$sb/configs/prompts/concierge.md" "delivered-concierge" "T5: delivered prompts/concierge.md NOT overwritten"
    # /configs/prompts/extra.md: NOT delivered, /opt has it, filled
    assert_file_content "$sb/configs/prompts/extra.md" "Extra prompt from /opt" "T5: /opt-only prompts/extra.md filled"
    rm -rf "$sb"
    echo "PASS T5: prompts/ subdir - fill-absent-only at every level"
}

failed=0
t1 || failed=1
t2 || failed=1
t3 || failed=1
t4 || failed=1
t5 || failed=1

if [ "$failed" -ne 0 ]; then
    echo "FAILED" >&2
    exit 1
fi
echo "OK: all 5 platform-agent-entrypoint tests passed"
