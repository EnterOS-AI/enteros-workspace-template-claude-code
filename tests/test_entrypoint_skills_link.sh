#!/usr/bin/env bash
# tests/test_entrypoint_skills_link.sh
#
# Container-runnable unit test for entrypoint.sh's
# link_plugin_skills_into_claude_home().
#
# WHY THIS FUNCTION EXISTS (the bug this test pins down):
# plugin skills are materialized by the runtime's AgentskillsAdaptor
# into /configs/skills/<skill>/SKILL.md, but Claude Code discovers
# personal skills ONLY under ~/.claude/skills. With no bridge, a plugin
# could install cleanly and its skill still be invisible to the agent —
# observed live 2026-07-05 on the agents-team platform agent: the
# lark-connect skill sat in /configs/skills for a day while the agent
# flailed on "connect Lark" asks; hand-creating the symlink made the
# very next turn list and invoke the skill.
#
# Strategy (same pattern as test_entrypoint_restore.sh): awk-extract the
# function from entrypoint.sh, sed-rewrite its hardcoded paths into a
# per-test sandbox, mock chown (uid may not be root / agent user may not
# exist in the test env), source, call, assert on the filesystem.
#
# Coverage:
#   T1: fresh boot                  -> /configs/skills created + symlink lands
#   T2: re-run                      -> idempotent, link still correct
#   T3: stale/dangling symlink      -> re-pointed at /configs/skills
#   T4: REAL dir at ~/.claude/skills-> left untouched, no nested link inside
#   T5: skill content readable through the link (the actual UX contract)
#
# Runs:
#   bash tests/test_entrypoint_skills_link.sh
#
# Exit 0 = all tests pass. Non-zero = at least one assertion failed.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENTRYPOINT="$REPO_ROOT/entrypoint.sh"

if [ ! -f "$ENTRYPOINT" ]; then
    echo "FAIL: entrypoint.sh not at $ENTRYPOINT"
    exit 1
fi

extract_function() {
    awk '
        /^link_plugin_skills_into_claude_home\(\)/ { capturing = 1 }
        capturing { print }
        capturing && /^}/ { capturing = 0; exit }
    ' "$ENTRYPOINT"
}

ORIGINAL_FN=$(extract_function)
if [ -z "$ORIGINAL_FN" ]; then
    echo "FAIL: could not extract link_plugin_skills_into_claude_home() from $ENTRYPOINT"
    exit 1
fi

PASS=0
FAIL=0
declare -a FAILURES=()

# chown to agent:agent fails outside the container (no agent user / not
# root). The function is fail-soft around it, but mock it anyway so test
# output stays clean and we never depend on the host's user table.
chown() { :; }
export -f chown

write_patched_function() {
    local tmp="$1"
    local fn_file="$tmp/skills_fn.sh"
    printf '%s\n' "$ORIGINAL_FN" \
        | sed \
            -e "s|/configs/skills|$tmp/configs/skills|g" \
            -e "s|/home/agent/.claude/skills|$tmp/home/agent/.claude/skills|g" \
        > "$fn_file"
    echo "$fn_file"
}

setup_sandbox() {
    local tmp
    tmp=$(mktemp -d)
    mkdir -p "$tmp/configs"
    mkdir -p "$tmp/home/agent/.claude"
    echo "$tmp"
}

# ---------------- T1: fresh boot lands the link ----------------
t1_fresh_boot_links() {
    local TMP FN_FILE out target
    TMP=$(setup_sandbox)
    FN_FILE=$(write_patched_function "$TMP")
    # shellcheck disable=SC1090
    source "$FN_FILE"

    out=$(link_plugin_skills_into_claude_home 2>&1)

    if [ ! -d "$TMP/configs/skills" ]; then
        echo "expected $TMP/configs/skills to be created; out: $out"
        rm -rf "$TMP"; return 1
    fi
    if [ ! -L "$TMP/home/agent/.claude/skills" ]; then
        echo "expected symlink at ~/.claude/skills; out: $out"
        rm -rf "$TMP"; return 1
    fi
    target=$(readlink "$TMP/home/agent/.claude/skills")
    if [ "$target" != "$TMP/configs/skills" ]; then
        echo "symlink points at '$target', expected '$TMP/configs/skills'"
        rm -rf "$TMP"; return 1
    fi
    if ! echo "$out" | grep -q "MOLECULE-SKILLS: linked"; then
        echo "expected linked log line; out: $out"
        rm -rf "$TMP"; return 1
    fi
    rm -rf "$TMP"
    return 0
}

# ---------------- T2: idempotent re-run ----------------
t2_rerun_idempotent() {
    local TMP FN_FILE target
    TMP=$(setup_sandbox)
    FN_FILE=$(write_patched_function "$TMP")
    # shellcheck disable=SC1090
    source "$FN_FILE"

    link_plugin_skills_into_claude_home >/dev/null 2>&1
    link_plugin_skills_into_claude_home >/dev/null 2>&1

    if [ ! -L "$TMP/home/agent/.claude/skills" ]; then
        echo "second run destroyed the symlink"
        rm -rf "$TMP"; return 1
    fi
    target=$(readlink "$TMP/home/agent/.claude/skills")
    if [ "$target" != "$TMP/configs/skills" ]; then
        echo "second run re-pointed the symlink to '$target'"
        rm -rf "$TMP"; return 1
    fi
    # ln -sfn against an existing SYMLINK must replace it, never nest a
    # link inside the target dir.
    if [ -e "$TMP/configs/skills/skills" ]; then
        echo "nested link created inside /configs/skills on re-run"
        rm -rf "$TMP"; return 1
    fi
    rm -rf "$TMP"
    return 0
}

# ---------------- T3: stale symlink gets re-pointed ----------------
t3_stale_symlink_repointed() {
    local TMP FN_FILE target
    TMP=$(setup_sandbox)
    FN_FILE=$(write_patched_function "$TMP")
    # A stale link — e.g. restored by the backup rsync from an image
    # generation whose configs path differed, or plain dangling.
    ln -s "$TMP/nonexistent-old-path" "$TMP/home/agent/.claude/skills"
    # shellcheck disable=SC1090
    source "$FN_FILE"

    link_plugin_skills_into_claude_home >/dev/null 2>&1

    target=$(readlink "$TMP/home/agent/.claude/skills")
    if [ "$target" != "$TMP/configs/skills" ]; then
        echo "stale symlink not re-pointed; still '$target'"
        rm -rf "$TMP"; return 1
    fi
    rm -rf "$TMP"
    return 0
}

# ---------------- T4: real directory is never clobbered ----------------
t4_real_dir_untouched() {
    local TMP FN_FILE out
    TMP=$(setup_sandbox)
    FN_FILE=$(write_patched_function "$TMP")
    mkdir -p "$TMP/home/agent/.claude/skills/my-hand-authored-skill"
    echo "user data" > "$TMP/home/agent/.claude/skills/my-hand-authored-skill/SKILL.md"
    # shellcheck disable=SC1090
    source "$FN_FILE"

    out=$(link_plugin_skills_into_claude_home 2>&1)

    if [ -L "$TMP/home/agent/.claude/skills" ]; then
        echo "real dir was replaced by a symlink (user data clobbered)"
        rm -rf "$TMP"; return 1
    fi
    if [ ! -f "$TMP/home/agent/.claude/skills/my-hand-authored-skill/SKILL.md" ]; then
        echo "user skill file lost"
        rm -rf "$TMP"; return 1
    fi
    # Must not have nested a link INSIDE the real dir either.
    if [ -e "$TMP/home/agent/.claude/skills/skills" ]; then
        echo "nested link created inside the real dir"
        rm -rf "$TMP"; return 1
    fi
    if ! echo "$out" | grep -q "not a symlink"; then
        echo "expected leave-alone log line; out: $out"
        rm -rf "$TMP"; return 1
    fi
    rm -rf "$TMP"
    return 0
}

# ---------------- T5: skill content visible through the link ----------------
t5_skill_visible_through_link() {
    local TMP FN_FILE
    TMP=$(setup_sandbox)
    FN_FILE=$(write_patched_function "$TMP")
    # shellcheck disable=SC1090
    source "$FN_FILE"

    link_plugin_skills_into_claude_home >/dev/null 2>&1

    # Simulate the AgentskillsAdaptor writing a plugin skill POST-link
    # (post-boot plugin install) — it must be reachable through the
    # personal-skills path with zero further action.
    mkdir -p "$TMP/configs/skills/lark-connect"
    echo "---" > "$TMP/configs/skills/lark-connect/SKILL.md"

    if [ ! -f "$TMP/home/agent/.claude/skills/lark-connect/SKILL.md" ]; then
        echo "skill written to /configs/skills not visible via ~/.claude/skills"
        rm -rf "$TMP"; return 1
    fi
    rm -rf "$TMP"
    return 0
}

run_test() {
    local name="$1"
    local out
    if out=$("$name" 2>&1); then
        PASS=$((PASS + 1))
        echo "PASS: $name"
    else
        FAIL=$((FAIL + 1))
        FAILURES+=("$name: $out")
        echo "FAIL: $name"
        echo "      $out"
    fi
}

run_test t1_fresh_boot_links
run_test t2_rerun_idempotent
run_test t3_stale_symlink_repointed
run_test t4_real_dir_untouched
run_test t5_skill_visible_through_link

echo ""
echo "passed=$PASS failed=$FAIL"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
