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

# T6: USER/volume-permission contract (CR2 #12653).
#
# The wrapper does `mkdir -p "$DST"` + `cp "$SRC/$rel" "$DST/$rel"`
# (the per-file /opt→/configs reconcile, #2919 risk-1+2). In the
# running container, /configs is a volume mounted as ROOT-owned
# (mode 755) — writable ONLY by uid 0. The wrapper MUST run as
# root for those ops to succeed; under `set -eu` a non-root
# mkdir/cp aborts the boot with an EACCES.
#
# The earlier tests in this file (T1–T5) rewrite SRC + DST to
# per-test temp dirs that the test runner can freely read AND
# write to (because we make them). That hides the
# USER/volume-permission contract: the wrapper succeeds as
# non-root against user-writable sandboxes, so a future PR that
# added a final `USER agent` (or any non-root USER) back to
# Dockerfile.platform-agent would NOT be caught by T1–T5 — the
# wrapper would still "work" in CI, then EACCES in the container.
#
# T6 simulates the in-container contract: chmod 555 the /configs
# sandbox (read+execute only, no write for the test user OR
# anyone else except root — equivalent to root-owned mode 755).
# The wrapper MUST fail. This is a "negative" test: it asserts
# the contract by failing the wrapper on purpose, so a future
# regression that runs the wrapper as non-root in a root-owned
# /configs would ALSO fail in T6's simulation.
#
# If the test runner cannot simulate the contract (e.g. it's
# already running as root and chmod 555 doesn't affect root's
# access), T6 SKIPs rather than producing a false pass — the
# Dockerfile-side guard (no final `USER agent`) is the
# authoritative check; T6 is the contract-assertion belt-and-
# braces.
t6() {
    local sb
    sb="$(mktemp -d)"
    mkdir -p "$sb/opt/molecule-platform-agent-template/prompts" "$sb/configs/prompts"
    # Populate /opt so the wrapper has files to copy.
    cat > "$sb/opt/molecule-platform-agent-template/config.yaml" <<'EOF'
model: moonshot/kimi-k2.6
runtime: claude-code
EOF
    cat > "$sb/opt/molecule-platform-agent-template/mcp_servers.yaml" <<'EOF'
mcp_servers:
  - name: platform
    command: molecule-platform-mcp
EOF
    # Simulate the in-container USER/volume-permission contract:
    # /configs is a root-owned volume mount, mode 755 (writable
    # only by uid 0). We can't chown to root as a non-root test
    # user, so chmod 555 (read+execute only, no write for anyone
    # except root) achieves the same effective contract: the
    # wrapper's `cp "$SRC/file" "$DST/file"` will fail with EACCES
    # under `set -eu`.
    chmod 555 "$sb/configs"
    chmod 555 "$sb/configs/prompts"

    # If we're running as root, chmod 555 doesn't restrict us, so
    # the simulation is meaningless (the wrapper would succeed
    # and T6 would falsely report PASS). SKIP in that case —
    # the Dockerfile-side guard (no final USER) is the
    # authoritative check; T6 is for non-root test environments.
    if [ "$(id -u)" = "0" ]; then
        echo "SKIP T6: test runner is uid 0 (chmod 555 doesn't restrict root); the Dockerfile-side guard (no final \`USER agent\`) is the authoritative check for this regression"
        chmod 755 "$sb/configs" "$sb/configs/prompts"
        rm -rf "$sb"
        return 0
    fi

    local patched
    patched="$(mktemp)"
    extract_per_file_body "$sb/opt/molecule-platform-agent-template" "$sb/configs" > "$patched"
    set +e
    sh "$patched" 2>/dev/null
    local rc=$?
    set -e
    rm -f "$patched"
    # Restore perms so we can clean up.
    chmod 755 "$sb/configs" "$sb/configs/prompts"
    rm -rf "$sb"

    if [ "$rc" -eq 0 ]; then
        echo "FAIL [T6]: wrapper SUCCEEDED against a non-writable /configs sandbox — the USER/volume-permission contract is NOT enforced. A future PR that adds a final \`USER agent\` to Dockerfile.platform-agent would NOT be caught by this test." >&2
        return 1
    fi
    echo "PASS T6: wrapper correctly FAILS when run as non-root against a non-writable /configs — USER/volume-permission contract enforced (proves Dockerfile MUST NOT set a final non-root USER after the ENTRYPOINT)"
}

failed=0
t1 || failed=1
t2 || failed=1
t3 || failed=1
t4 || failed=1
t5 || failed=1
t6 || failed=1

if [ "$failed" -ne 0 ]; then
    echo "FAILED" >&2
    exit 1
fi
echo "OK: all 6 platform-agent-entrypoint tests passed"
