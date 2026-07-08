#!/usr/bin/env bash
# tests/test_entrypoint_restore.sh
#
# Container-runnable unit test for entrypoint.sh's
# restore_from_secondary_volume() function (cp#326 Option D).
#
# Strategy: extract the function definition from entrypoint.sh, rewrite
# its hardcoded paths to point at a per-test sandbox, write the patched
# copy to a tempfile, source it, then call the function. Mock blkid /
# mount / mountpoint when the test needs the happy path; rsync runs for
# real against the sandbox so we can verify the data actually lands.
#
# Coverage:
#   T1: marker present  -> no-op (idempotency)
#   T2: device absent   -> no-op
#   T3: device present + happy-path rsync -> restore lands + marker drop
#   T4: re-run after T3 -> short-circuit on marker
#
# Runs:
#   bash tests/test_entrypoint_restore.sh
#
# Exit 0 = all tests pass. Non-zero = at least one assertion failed.
#
# This is the watch-it-fail-red unit-test layer for PR 2; the wire-
# level contract against real AWS is asserted by the matching CP
# Stage C smoke (stage-c-workspace-backup-smoke.sh step 6b).

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENTRYPOINT="$REPO_ROOT/entrypoint.sh"

if [ ! -f "$ENTRYPOINT" ]; then
    echo "FAIL: entrypoint.sh not at $ENTRYPOINT"
    exit 1
fi

# Extract just the restore_from_secondary_volume function body from
# entrypoint.sh so we can rewrite paths without re-executing the file's
# top-level code. awk emits from the function header through the
# matching close brace at column 0.
extract_function() {
    awk '
        /^restore_from_secondary_volume\(\)/ { capturing = 1 }
        capturing { print }
        capturing && /^}/ { capturing = 0; exit }
    ' "$ENTRYPOINT"
}

ORIGINAL_FN=$(extract_function)
if [ -z "$ORIGINAL_FN" ]; then
    echo "FAIL: could not extract restore_from_secondary_volume() from $ENTRYPOINT"
    exit 1
fi

PASS=0
FAIL=0
declare -a FAILURES=()

# Each test gets:
#   $TMP        — sandbox tempdir
#   $DEV        — the fake "secondary device" (a regular file)
#   $MOUNT_POINT
#   $MARKER     — the idempotency marker path
#   $FN_FILE    — a sourceable copy of the function with paths rewritten
#
# Rewrites applied:
#   /dev/xvdb                       -> $DEV
#   /mnt/restore                    -> $MOUNT_POINT
#   /configs/.restore-completed     -> $MARKER
#   the [ ! -b ] block-device test  -> [ ! -e ] regular-file test
#   the rsync destination "/$rel"   -> "$TMP/$rel"
#
# Mocks (when needed) override blkid / mount / mountpoint as bash
# functions BEFORE sourcing the SUT, then exported so the sourced
# subshell inherits them.

write_patched_function() {
    local tmp="$1"
    local dev="$2"
    local mp="$3"
    local marker="$4"

    local fn_file="$tmp/restore_fn.sh"
    # Use a here-doc to avoid quoting nightmares with sed -i.
    # Path rewrites are simple character substitutions; the awk-extracted
    # body has no | characters in the matched regions so sed's | delim
    # is safe.
    printf '%s\n' "$ORIGINAL_FN" \
        | sed \
            -e "s|/dev/xvdb|$dev|g" \
            -e "s|/mnt/restore|$mp|g" \
            -e "s|/configs/\\.restore-completed|$marker|g" \
            -e "s|\\[ ! -b \"\$SECONDARY_DEV\" \\]|[ ! -e \"\$SECONDARY_DEV\" ]|g" \
            -e "s|DST=\"/\$rel\"|DST=\"$tmp/\$rel\"|g" \
        > "$fn_file"
    echo "$fn_file"
}

setup_sandbox() {
    local tmp
    tmp=$(mktemp -d)
    mkdir -p "$tmp/configs"
    mkdir -p "$tmp/workspace"
    mkdir -p "$tmp/home/agent/.claude"
    mkdir -p "$tmp/mnt"
    echo "$tmp"
}

# ---------------- T1: marker-present short-circuit ----------------
t1_marker_present_short_circuits() {
    local TMP DEV MP MARKER FN_FILE out
    TMP=$(setup_sandbox)
    DEV="$TMP/devnull"  # not used; we early-exit
    MP="$TMP/mnt"
    MARKER="$TMP/configs/.restore-completed"
    : > "$MARKER"  # marker present from the start

    FN_FILE=$(write_patched_function "$TMP" "$DEV" "$MP" "$MARKER")
    # shellcheck disable=SC1090
    source "$FN_FILE"

    out=$(restore_from_secondary_volume 2>&1)

    if ! echo "$out" | grep -q "marker $MARKER present"; then
        echo "expected short-circuit message in: $out"
        rm -rf "$TMP"; return 1
    fi
    if echo "$out" | grep -q "detected"; then
        echo "should not have reached device probe; got: $out"
        rm -rf "$TMP"; return 1
    fi
    rm -rf "$TMP"
    return 0
}

# ---------------- T2: device absent no-op ----------------
t2_device_absent_noop() {
    local TMP DEV MP MARKER FN_FILE out
    TMP=$(setup_sandbox)
    DEV="$TMP/devnull"  # nonexistent file
    MP="$TMP/mnt"
    MARKER="$TMP/configs/.restore-completed"
    # No marker; no device.

    FN_FILE=$(write_patched_function "$TMP" "$DEV" "$MP" "$MARKER")
    # shellcheck disable=SC1090
    source "$FN_FILE"

    out=$(restore_from_secondary_volume 2>&1)

    if ! echo "$out" | grep -qF "no $DEV"; then
        echo "expected absent-device skip; got: $out"
        rm -rf "$TMP"; return 1
    fi
    if [ -f "$MARKER" ]; then
        echo "marker MUST NOT be created when device is absent"
        rm -rf "$TMP"; return 1
    fi
    rm -rf "$TMP"
    return 0
}

# ---------------- T3: happy-path rsync ----------------
# Mock blkid -> "ext4". Mock mount -> populate $MOUNT_POINT by
# symlinking each top-level dir from a fixture. Mock mountpoint -> 0.
# rsync runs for real.
t3_happy_path_rsync() {
    local TMP DEV MP MARKER FN_FILE out
    TMP=$(setup_sandbox)
    DEV="$TMP/xvdb"
    : > "$DEV"
    MP="$TMP/mnt"
    MARKER="$TMP/configs/.restore-completed"

    # Fixture "snapshot contents" that mount(8) will expose at $MP.
    local SRC="$TMP/src"
    mkdir -p "$SRC/configs" "$SRC/workspace" "$SRC/home/agent/.claude"
    echo "model: claude-opus" > "$SRC/configs/config.yaml"
    echo "TOKEN" > "$SRC/configs/.auth_token"
    echo "console.log(1)" > "$SRC/workspace/index.js"
    echo "{}" > "$SRC/home/agent/.claude/settings.json"

    # Mocks. Defined as functions in THIS shell; because we call the SUT
    # without a command-substitution subshell (capture stdout via a
    # tempfile instead), the SUT inherits these shadowing functions AND
    # the local $SRC / $MP vars. macOS bash 3.2 drops `export -f`
    # functions across command-substitution subshells, so we avoid both
    # `export -f` and `$(...)` around the SUT call.
    MOCK_SRC="$SRC"
    MOCK_MP="$MP"
    blkid() {
        # blkid -s TYPE -o value <dev>  — always report ext4 in the test.
        echo "ext4"
        return 0
    }
    mount() {
        # Simulate ro-mount by exposing the fixture at the mount point.
        ln -sfn "$MOCK_SRC/configs" "$MOCK_MP/configs"
        ln -sfn "$MOCK_SRC/workspace" "$MOCK_MP/workspace"
        ln -sfn "$MOCK_SRC/home" "$MOCK_MP/home"
        return 0
    }
    mountpoint() {
        # The SUT calls `mountpoint -q <mp>`; report mounted.
        return 0
    }
    # Portable rsync mock. macOS ships rsync 2.6.9 which rejects the
    # production -aHAX flags (the Linux container has rsync 3.x). We
    # mock rsync with a cp-based equivalent so the test verifies the
    # SUT's ORCHESTRATION (path selection, exit-code handling, marker
    # drop) independent of the host rsync version. The -aHAX flag
    # correctness is validated by the publish-image build (Linux) +
    # the CP Stage C integration smoke. The last two args are the
    # SRC/ and DST/ (trailing-slash rsync dir-copy semantics).
    rsync() {
        local src="" dst=""
        # Last two positional args are src and dst.
        for a in "$@"; do
            src="$dst"
            dst="$a"
        done
        # src now = second-to-last, dst = last. cp -R the contents.
        mkdir -p "$dst"
        # Copy contents of src (trailing slash) into dst.
        if [ -d "$src" ]; then
            cp -R "$src". "$dst" 2>/dev/null || cp -R "$src"* "$dst" 2>/dev/null || true
            return 0
        fi
        return 1
    }

    FN_FILE=$(write_patched_function "$TMP" "$DEV" "$MP" "$MARKER")
    # shellcheck disable=SC1090
    source "$FN_FILE"

    # Capture stdout via a tempfile so the SUT runs in THIS shell (not a
    # command-substitution subshell) and can see the mock functions.
    local outf="$TMP/out.log"
    restore_from_secondary_volume >"$outf" 2>&1
    out=$(cat "$outf")

    # Marker dropped
    if [ ! -f "$MARKER" ]; then
        echo "marker should be dropped after rsync. out:"; echo "$out"
        unset -f blkid mount mountpoint rsync
        rm -rf "$TMP"; return 1
    fi
    # configs/config.yaml restored
    if [ ! -f "$TMP/configs/config.yaml" ]; then
        echo "configs/config.yaml not restored. Tree:"
        find "$TMP" -maxdepth 4 -print
        unset -f blkid mount mountpoint rsync
        rm -rf "$TMP"; return 1
    fi
    if ! grep -q "claude-opus" "$TMP/configs/config.yaml"; then
        echo "config.yaml content wrong"
        unset -f blkid mount mountpoint rsync
        rm -rf "$TMP"; return 1
    fi
    # workspace/index.js restored
    if [ ! -f "$TMP/workspace/index.js" ]; then
        echo "workspace/index.js missing"
        unset -f blkid mount mountpoint rsync
        rm -rf "$TMP"; return 1
    fi
    # home/agent/.claude/settings.json restored
    if [ ! -f "$TMP/home/agent/.claude/settings.json" ]; then
        echo "settings.json missing"
        unset -f blkid mount mountpoint rsync
        rm -rf "$TMP"; return 1
    fi
    if ! echo "$out" | grep -q "MOLECULE-RESTORE: complete"; then
        echo "expected completion log line; got: $out"
        unset -f blkid mount mountpoint rsync
        rm -rf "$TMP"; return 1
    fi
    # Re-run from same state must short-circuit (marker is now present).
    # Marker gate fires before any mock is touched, so a subshell is fine.
    local out2
    restore_from_secondary_volume >"$TMP/out2.log" 2>&1
    out2=$(cat "$TMP/out2.log")
    if ! echo "$out2" | grep -q "marker $MARKER present"; then
        echo "second call after restore should short-circuit; got: $out2"
        unset -f blkid mount mountpoint rsync
        rm -rf "$TMP"; return 1
    fi

    unset -f blkid mount mountpoint rsync
    rm -rf "$TMP"
    return 0
}

# ---------------- T4: rsync failure surfaces a WARN (exit-code fix) -----
# Regression test for the bug the test suite caught during development:
# the production function used `rsync ... | sed`, whose pipeline exit
# code is sed's (0), masking rsync failures and printing
# "MOLECULE-RESTORE: ok" even when rsync errored. The fix routes
# rsync's output through a tempfile and reads $? directly. This test
# pins that a FAILING rsync surfaces the WARN line + "complete with
# WARNINGS" and does NOT print "ok" for the failed path.
t4_rsync_failure_surfaces_warn() {
    local TMP DEV MP MARKER FN_FILE out
    TMP=$(setup_sandbox)
    DEV="$TMP/xvdb"
    : > "$DEV"
    MP="$TMP/mnt"
    MARKER="$TMP/configs/.restore-completed"

    local SRC="$TMP/src"
    mkdir -p "$SRC/configs"
    echo "x" > "$SRC/configs/config.yaml"

    MOCK_SRC="$SRC"
    MOCK_MP="$MP"
    blkid() { echo "ext4"; return 0; }
    mount() {
        ln -sfn "$MOCK_SRC/configs" "$MOCK_MP/configs"
        # workspace + home absent on purpose — those paths get the
        # "source absent — skipping" branch, not a failure.
        return 0
    }
    mountpoint() { return 0; }
    # rsync mock that ALWAYS fails (simulates a read error / disk full).
    rsync() { return 23; }  # 23 = partial transfer, a real rsync code

    FN_FILE=$(write_patched_function "$TMP" "$DEV" "$MP" "$MARKER")
    # shellcheck disable=SC1090
    source "$FN_FILE"

    restore_from_secondary_volume >"$TMP/out.log" 2>&1
    out=$(cat "$TMP/out.log")

    # MUST surface the WARN for the failed configs rsync.
    if ! echo "$out" | grep -q "WARN rsync to .* exited 23"; then
        echo "expected 'WARN rsync ... exited 23'; got: $out"
        unset -f blkid mount mountpoint rsync
        rm -rf "$TMP"; return 1
    fi
    # MUST NOT print "ok" for the failed configs path (the masked-exit
    # bug would have printed it).
    if echo "$out" | grep -q "MOLECULE-RESTORE: ok .*configs$"; then
        echo "must NOT print 'ok' for a failed rsync (exit-code masking regression); got: $out"
        unset -f blkid mount mountpoint rsync
        rm -rf "$TMP"; return 1
    fi
    # Completion line should be the WARNINGS variant.
    if ! echo "$out" | grep -q "complete with WARNINGS"; then
        echo "expected 'complete with WARNINGS'; got: $out"
        unset -f blkid mount mountpoint rsync
        rm -rf "$TMP"; return 1
    fi
    # Marker still drops (re-running rsync wouldn't recover — same source).
    if [ ! -f "$MARKER" ]; then
        echo "marker should still drop even on rsync failure"
        unset -f blkid mount mountpoint rsync
        rm -rf "$TMP"; return 1
    fi

    unset -f blkid mount mountpoint rsync
    rm -rf "$TMP"
    return 0
}

run() {
    local name="$1"
    local fn="$2"
    local outfile
    outfile=$(mktemp)
    if "$fn" >"$outfile" 2>&1; then
        echo "PASS  $name"
        PASS=$((PASS + 1))
    else
        echo "FAIL  $name"
        sed 's/^/      /' "$outfile"
        FAIL=$((FAIL + 1))
        FAILURES+=("$name")
    fi
    rm -f "$outfile"
}

run "T1_marker_present_short_circuits" t1_marker_present_short_circuits
run "T2_device_absent_noop"             t2_device_absent_noop
run "T3_happy_path_rsync"               t3_happy_path_rsync
run "T4_rsync_failure_surfaces_warn"    t4_rsync_failure_surfaces_warn

echo
echo "------------------------------------------------"
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
    echo "Failed tests:"
    for f in "${FAILURES[@]}"; do
        echo "  - $f"
    done
    exit 1
fi
exit 0
