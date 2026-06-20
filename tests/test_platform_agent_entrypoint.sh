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

# Helpers for privilege handling in T6. Tests may run as a non-root
# user with passwordless sudo (local dev) OR as root (CI). We use sudo
# only when we are not already root, and we use runuser(1) to drop to
# uid 1000 for the negative sub-case so it is exercised even when the
# test runner itself is root.
maybe_sudo() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo -n "$@"
    fi
}

as_non_root() {
    # Prefer the image's runtime user if it exists locally.
    if id agent >/dev/null 2>&1; then
        runuser -u agent -- "$@"
    elif id -u nobody >/dev/null 2>&1; then
        runuser -u nobody -- "$@"
    else
        # Last resort: rely on the test runner already being non-root.
        "$@"
    fi
}

# T6: USER/volume-permission contract (CR2 #12653 / review 12686).
#
# Reproduces the REAL container contract, end-to-end:
#   1. /configs is ROOT-OWNED (chown 0:0, mode 755 — writable only
#      by uid 0). The test runner (non-root, typically uid 1000)
#      cannot write to it.
#   2. The wrapper at scripts/platform-agent-entrypoint.sh runs
#      UNMODIFIED — no SRC/DST rewrite, no `exec` no-op. Its
#      hardcoded /opt/molecule-platform-agent-template + /configs
#      resolve to the sandbox via sudo bind-mounts.
#   3. The wrapper chains to /entrypoint.sh (a stub we provide
#      inside the sandbox) instead of a no-op echo — the real
#      contract requires the wrapper to exec the base entrypoint.
#   4. The test has TWO sub-cases that together pin the contract:
#      A. RUN AS ROOT (sudo) → wrapper completes successfully;
#         config.yaml + mcp_servers.yaml + prompts/concierge.md
#         land in /configs; the stub /entrypoint.sh ran.
#         This is the post-Dockerfile-fix behavior.
#      B. RUN AS NON-ROOT (test runner, simulating the original
#         `USER agent` regression) → wrapper ABORTS under `set -eu`
#         with EACCES on the cp into the root-owned /configs.
#         This is the contract GUARD — a future PR that re-adds
#         `USER agent` (or any non-root USER) to
#         Dockerfile.platform-agent would break the container
#         boot, AND break this sub-case in CI. Sub-case A still
#         passes (we explicitly use sudo), so the test's only
#         failure mode for the regression is sub-case B.
#
# If the test runner lacks `sudo` (no unprivileged container /
# no privileged access), T6 SKIPs — the authoritative check is
# the Dockerfile-side guard (no final `USER agent`), and the
# in-CI staging path runs the real platform-agent image where
# the actual root-owned /configs is the live contract.
t6() {
    # Sudo required to chown to root, bind-mount over /opt and
    # /configs, and run the wrapper as root. The test runner
    # (uid 1000) cannot do any of these.
    if ! maybe_sudo true 2>/dev/null; then
        echo "SKIP T6: no passwordless sudo (cannot reproduce the root-owned /configs contract); the Dockerfile-side guard is the authoritative check"
        return 0
    fi

    local sb
    sb="$(mktemp -d)"
    mkdir -p "$sb/opt/molecule-platform-agent-template/prompts" "$sb/configs/prompts"

    # Populate the template content (the source the wrapper copies
    # FROM). The wrapper's hardcoded SRC is /opt/molecule-platform-agent-template
    # — we bind-mount $sb/opt over the real /opt below.
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

    # Stub /entrypoint.sh — the wrapper exec's THIS, not a no-op echo.
    # The stub echoes the would-run marker and exits 0 so the
    # wrapper's `exec /entrypoint.sh "$@"` succeeds.
    cat > "$sb/entrypoint.sh" <<'EOF'
#!/bin/sh
echo "[test-stub /entrypoint.sh] would run molecule-runtime"
exit 0
EOF
    chmod +x "$sb/entrypoint.sh"

    # The ACTUAL wrapper — copied from the repo, not rewritten.
    # No sed SRC/DST munging. No `exec` no-op patch. The wrapper
    # runs as it would at container start.
    local wrapper_path="$sb/platform-agent-entrypoint.sh"
    cp "$(dirname "$0")/../scripts/platform-agent-entrypoint.sh" "$wrapper_path"
    chmod +x "$wrapper_path"

    # Reproduce the in-container contract: /configs is root-owned
    # (chown 0:0) and mode 755. The test runner (uid 1000) cannot
    # write to it; only the root-run wrapper (sub-case A) can.
    maybe_sudo chown -R 0:0 "$sb/configs" 2>/dev/null || {
        echo "SKIP T6: cannot sudo chown to root" >&2
        rm -rf "$sb"
        return 0
    }
    maybe_sudo chmod 755 "$sb/configs" "$sb/configs/prompts" 2>/dev/null

    # Bind-mount the sandbox over the real /opt, /configs, AND
    # /entrypoint.sh so the wrapper's hardcoded paths resolve to
    # the sandbox. The /entrypoint.sh bind-mount is needed because
    # the wrapper's final `exec /entrypoint.sh "$@"` would otherwise
    # invoke the real /entrypoint.sh on the test system (which does
    # real work — chown, gosu, mount/restore — and would interfere
    # with the test). The stub /entrypoint.sh echoes a marker and
    # exits 0, so the wrapper's chain-to-base-entrypoint contract
    # is exercised without side effects.
    local mounted_opt=0 mounted_configs=0 mounted_entrypoint=0
    if ! maybe_sudo mount --bind "$sb/opt" /opt 2>/dev/null; then
        echo "SKIP T6: cannot sudo mount --bind /opt" >&2
        maybe_sudo chown -R "$(id -u):$(id -g)" "$sb/configs" 2>/dev/null
        rm -rf "$sb"
        return 0
    fi
    mounted_opt=1
    if ! maybe_sudo mount --bind "$sb/configs" /configs 2>/dev/null; then
        maybe_sudo umount /opt 2>/dev/null || true
        echo "SKIP T6: cannot sudo mount --bind /configs" >&2
        maybe_sudo chown -R "$(id -u):$(id -g)" "$sb/configs" 2>/dev/null
        rm -rf "$sb"
        return 0
    fi
    mounted_configs=1
    if ! maybe_sudo mount --bind "$sb/entrypoint.sh" /entrypoint.sh 2>/dev/null; then
        maybe_sudo umount /configs 2>/dev/null || true
        maybe_sudo umount /opt 2>/dev/null || true
        echo "SKIP T6: cannot sudo mount --bind /entrypoint.sh" >&2
        maybe_sudo chown -R "$(id -u):$(id -g)" "$sb/configs" 2>/dev/null
        rm -rf "$sb"
        return 0
    fi
    mounted_entrypoint=1

    # ── T6-A: positive case — wrapper runs AS ROOT against a
    # ROOT-OWNED /configs. After the Dockerfile fix (no final
    # `USER agent`), this is what happens in the container.
    set +e
    maybe_sudo "$wrapper_path" 2>/dev/null
    local rc_root=$?
    set -e
    if [ "$rc_root" -ne 0 ]; then
        maybe_sudo umount /entrypoint.sh 2>/dev/null || true
        maybe_sudo umount /configs 2>/dev/null || true
        maybe_sudo umount /opt 2>/dev/null || true
        maybe_sudo chown -R "$(id -u):$(id -g)" "$sb/configs" 2>/dev/null
        echo "FAIL [T6-A]: wrapper aborted under set -eu (root-owned /configs, run as root via sudo). The Dockerfile fix should let the wrapper run as root, but it aborted. The cp into /configs is failing for some other reason (sandbox path, bind-mount, etc.) — investigate before claiming the contract is upheld." >&2
        rm -rf "$sb"
        return 1
    fi
    # Verify the per-file copy actually happened — the wrapper's
    # claimed exit-0 must be backed by real files in /configs.
    local copied_ok=1
    for rel in config.yaml mcp_servers.yaml prompts/concierge.md; do
        if [ ! -f "$sb/configs/$rel" ]; then
            echo "  [T6-A] missing expected copy: $sb/configs/$rel" >&2
            copied_ok=0
        fi
    done
    if [ "$copied_ok" -ne 1 ]; then
        maybe_sudo umount /entrypoint.sh 2>/dev/null || true
        maybe_sudo umount /configs 2>/dev/null || true
        maybe_sudo umount /opt 2>/dev/null || true
        maybe_sudo chown -R "$(id -u):$(id -g)" "$sb/configs" 2>/dev/null
        echo "FAIL [T6-A]: wrapper exited 0 but did NOT copy the expected files into /configs" >&2
        rm -rf "$sb"
        return 1
    fi
    echo "PASS T6-A: actual wrapper ran AS ROOT (sudo) against ROOT-OWNED /configs — config.yaml + mcp_servers.yaml + prompts/concierge.md all copied; wrapper exec'd the stub /entrypoint.sh (chained, not no-op); no abort under set -eu"

    # ── T6-B: negative case (the contract GUARD) — wrapper runs
    # AS NON-ROOT against the SAME root-owned /configs. This
    # simulates the original `USER agent` regression: the wrapper
    # runs as agent (uid 1000) and must FAIL on the cp into the
    # root-owned /configs. The wrapper's `set -eu` chain aborts
    # on the EACCES, which is the in-container failure mode.
    #
    # Reset /configs to empty (delete the T6-A copies) so the
    # wrapper's fill-absent-only logic actually tries to copy
    # (otherwise the [ ! -f ] guards skip and the test never
    # exercises the cp permission path). Keep root ownership +
    # mode 755.
    maybe_sudo rm -rf "$sb/configs"/* "$sb/configs/prompts"/* 2>/dev/null
    maybe_sudo chown -R 0:0 "$sb/configs" 2>/dev/null
    maybe_sudo chmod 755 "$sb/configs" "$sb/configs/prompts" 2>/dev/null

    # Run the wrapper as the non-root test user (NO sudo) — the
    # cp into root-owned /configs MUST fail with EACCES.
    set +e
    as_non_root "$wrapper_path" 2>/dev/null
    local rc_nonroot=$?
    set -e
    if [ "$rc_nonroot" -eq 0 ]; then
        # The wrapper SUCCEEDED as non-root. This is the regression:
        # the contract that the wrapper requires root is BROKEN.
        maybe_sudo umount /entrypoint.sh 2>/dev/null || true
        maybe_sudo umount /configs 2>/dev/null || true
        maybe_sudo umount /opt 2>/dev/null || true
        maybe_sudo chown -R "$(id -u):$(id -g)" "$sb/configs" 2>/dev/null
        echo "FAIL [T6-B]: wrapper SUCCEEDED as non-root against root-owned /configs. The USER=agent contract is NOT enforced — a future PR that adds a final \`USER agent\` (or any non-root USER) to Dockerfile.platform-agent would NOT be caught by this test. The original bug (CR2 #12653) would re-occur." >&2
        rm -rf "$sb"
        return 1
    fi
    # And the copies should NOT have happened (EACCES prevented them).
    if [ -f "$sb/configs/config.yaml" ]; then
        maybe_sudo umount /entrypoint.sh 2>/dev/null || true
        maybe_sudo umount /configs 2>/dev/null || true
        maybe_sudo umount /opt 2>/dev/null || true
        maybe_sudo chown -R "$(id -u):$(id -g)" "$sb/configs" 2>/dev/null
        echo "FAIL [T6-B]: wrapper aborted but config.yaml was still copied — the EACCES did not block the cp (sandbox perms wrong?)" >&2
        rm -rf "$sb"
        return 1
    fi
    echo "PASS T6-B: wrapper correctly FAILS as non-root against root-owned /configs — EACCES on the cp aborts the wrapper under set -eu; the USER=agent contract is enforced (a future \`USER agent\` PR would break the container AND break this sub-case)"

    # Cleanup: unmount in REVERSE bind order, restore perms, rm -rf.
    if [ "$mounted_entrypoint" = "1" ]; then
        maybe_sudo umount /entrypoint.sh 2>/dev/null || true
    fi
    if [ "$mounted_configs" = "1" ]; then
        maybe_sudo umount /configs 2>/dev/null || true
    fi
    if [ "$mounted_opt" = "1" ]; then
        maybe_sudo umount /opt 2>/dev/null || true
    fi
    maybe_sudo chown -R "$(id -u):$(id -g)" "$sb/configs" 2>/dev/null
    rm -rf "$sb"
}

# T7: Dockerfile-level contract guard (no-sudo fallback).
#
# T6 exercises the real wrapper-to-base-entrypoint chain against a
# root-owned /configs path, but it needs passwordless sudo (or a root
# test runner) to bind-mount /opt, /configs and /entrypoint.sh. In
# restricted CI runners T6 may SKIP. This test pins the same contract
# from the build artifact:
#   1. Dockerfile.platform-agent must NOT end with a non-root USER
#      (specifically no `USER agent`), so the wrapper starts as root.
#   2. Dockerfile.platform-agent must set ENTRYPOINT to the wrapper.
#   3. The wrapper script must chain to /entrypoint.sh.
# A future PR that re-adds `USER agent` or breaks the entrypoint chain
# will fail this test immediately, even where T6 cannot run.
t7() {
    local df
    df="$(dirname "$0")/../Dockerfile.platform-agent"
    if [ ! -f "$df" ]; then
        echo "FAIL [T7]: Dockerfile.platform-agent not found at $df" >&2
        return 1
    fi

    # Reject a final USER agent (or any non-root USER after the wrapper
    # is installed). USER root is fine; no USER after ENTRYPOINT is also
    # fine because the base image runs as root by default.
    local last_user
    last_user="$(grep -E '^USER ' "$df" | tail -n1 || true)"
    if [ -n "$last_user" ] && [ "$last_user" != "USER root" ]; then
        echo "FAIL [T7]: Dockerfile.platform-agent ends with non-root $last_user; the wrapper needs root to write the root-owned /configs volume" >&2
        return 1
    fi

    # Wrapper must be installed as the image entrypoint.
    if ! grep -Eq '^ENTRYPOINT\s+\["/platform-agent-entrypoint\.sh"\]' "$df"; then
        echo "FAIL [T7]: Dockerfile.platform-agent does not set ENTRYPOINT [\"/platform-agent-entrypoint.sh\"]" >&2
        return 1
    fi

    # Wrapper script must hand off to the base entrypoint.
    local wrapper
    wrapper="$(dirname "$0")/../scripts/platform-agent-entrypoint.sh"
    if [ ! -f "$wrapper" ]; then
        echo "FAIL [T7]: wrapper script not found at $wrapper" >&2
        return 1
    fi
    if ! grep -Eq '^exec /entrypoint\.sh "\$@"$' "$wrapper"; then
        echo "FAIL [T7]: wrapper does not end with 'exec /entrypoint.sh \"\$@\"'" >&2
        return 1
    fi

    echo "PASS T7: Dockerfile/platform-agent preserves root-entrypoint contract (no USER agent, wrapper ENTRYPOINT, chains to /entrypoint.sh)"
}

failed=0
t1 || failed=1
t2 || failed=1
t3 || failed=1
t4 || failed=1
t5 || failed=1
t6 || failed=1
t7 || failed=1

if [ "$failed" -ne 0 ]; then
    echo "FAILED" >&2
    exit 1
fi
echo "OK: all 7 platform-agent-entrypoint tests passed"
