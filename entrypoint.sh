#!/bin/sh
# Drop privileges to the agent user before exec'ing molecule-runtime.
# claude-code refuses --dangerously-skip-permissions when running as
# root/sudo for safety. Without this entrypoint, every cron tick fails
# with `ProcessError: Command failed with exit code 1` and the agent
# logs `--dangerously-skip-permissions cannot be used with root/sudo
# privileges for security reasons`.
#
# Pattern matches the legacy monorepo workspace-template/entrypoint.sh:
# fix volume ownership as root, then re-exec via gosu as agent (uid 1000).

# Boot-context snapshot — emitted on EVERY container start, including
# every restart of a crash-loop. Lets `docker logs` answer "what env
# was actually present?" without having to docker exec into a dying
# container. Logs NAMES of auth-relevant env vars, never VALUES. Fires
# twice (once as root pre-gosu, once as agent post-gosu) so an operator
# can see whether a value was lost across the privilege drop.
# Keep the env-name list in sync with adapter.py's _AUTH_ENV_AUDIT —
# the same set of vendors should be audited from both sides.
log_boot_context() {
    echo "----- entrypoint boot $(date -u +%Y-%m-%dT%H:%M:%SZ) -----"
    echo "uid=$(id -u) gid=$(id -g) user=$(id -un 2>/dev/null || echo unknown)"
    echo "hostname=$(hostname) workspace_id=${WORKSPACE_ID:-<unset>}"
    echo "platform_url=${PLATFORM_URL:-<unset>}"
    echo "configs_dir: $(ls -ld /configs 2>/dev/null || echo MISSING)"
    echo "configs_contents: $(ls /configs 2>/dev/null | tr '\n' ' ' || echo MISSING)"
    echo "workspace_dir: $(ls -ld /workspace 2>/dev/null || echo MISSING)"
    # Auth env presence (NAMES + set/unset only — never the values).
    # Mirror of _AUTH_ENV_AUDIT in adapter.py — keep in sync if you add a vendor.
    for var in CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL MINIMAX_API_KEY GLM_API_KEY KIMI_API_KEY DEEPSEEK_API_KEY; do
        eval "val=\$$var"
        if [ -n "$val" ]; then
            echo "env $var=set"
        else
            echo "env $var=unset"
        fi
    done
    echo "------------------------------------------------"
}
log_boot_context

# ---------------------------------------------------------------------
# Restore-on-recreate from secondary EBS volume (cp#326 Option D).
#
# Contract with CP: when ProvisionWorkspace finds a non-expired backup
# snapshot for this WorkspaceID, it attaches the snapshot as a SECONDARY
# EBS volume at /dev/xvdb at launch (DeleteOnTermination=true). This
# function mounts that volume on first container boot and rsyncs the
# restore set (/configs, /workspace, /home/agent/.claude) from it back
# into the root filesystem, then drops a marker so subsequent container
# restarts (within the same EC2's lifetime) skip the restore.
#
# Why cp#326 needs this: AWS rejects ANY SnapshotId on the ROOT device
# at RunInstances time with "InvalidBlockDeviceMapping: snapshotId
# cannot be modified on root device". The cp#301 architecture (override
# the AMI's root snapshot) is impossible per AWS spec. Option D works
# WITH AWS's model — secondary volumes accept SnapshotId — and rsync
# bridges the data-plane gap.
#
# Operational contract:
#   - Idempotent: a marker at /configs/.restore-completed gates re-runs.
#     If the container restarts in the same EC2 (DOT=true so the volume
#     persists across container restarts but NOT EC2 terminate), the
#     restore skips. If the EC2 is terminated + replaced, /configs is
#     fresh and the marker is gone — restore runs on the new EC2.
#   - Best-effort: any failure (volume absent, fs unreadable, rsync
#     error) is LOGGED with MOLECULE-RESTORE: prefix but does NOT abort
#     the boot. The workspace comes up with empty state — the explicit
#     no-restore branch the user already accepts on first-time provision.
#   - Read-only mount on the secondary at /mnt/restore so a defective
#     filesystem can't corrupt our root.
#   - All log lines prefixed `MOLECULE-RESTORE:` so `docker logs <id>
#     2>&1 | grep MOLECULE-RESTORE` is the operator's one-liner debug.
#
# Path allowlist (NOT a blanket /mnt/restore -> / rsync — that would
# also restore /etc/passwd, /var/lib/docker, etc. which are container-
# managed):
#   - /configs/         (config.yaml, .auth_token, skills/, memory)
#   - /workspace/       (the shared codebase + agent's working files)
#   - /home/agent/.claude/  (Claude SDK session state, settings.json)
#
# If a future template adds another persistent path (e.g. /home/agent/.cache),
# add it to RESTORE_PATHS below AND ensure the corresponding source path
# exists in the snapshot. Keep the list narrow on purpose — the alternative
# (full / rsync with exclusions) trades blast-radius safety for convenience.
restore_from_secondary_volume() {
    local SECONDARY_DEV="/dev/xvdb"
    local MOUNT_POINT="/mnt/restore"
    local MARKER="/configs/.restore-completed"

    # Marker present = restore already done for this EC2's lifetime.
    # Cheapest possible idempotency check; runs before any blockdev probe.
    if [ -f "$MARKER" ]; then
        echo "MOLECULE-RESTORE: marker $MARKER present — skipping (already restored on this EC2)"
        return 0
    fi

    # No secondary device = nothing to restore (first-time provision or
    # no backup snapshot existed). NOT an error.
    if [ ! -b "$SECONDARY_DEV" ]; then
        echo "MOLECULE-RESTORE: no $SECONDARY_DEV — first-time provision or no backup snapshot, skipping"
        return 0
    fi

    echo "MOLECULE-RESTORE: $SECONDARY_DEV detected — attempting restore"

    # Probe filesystem type. If blkid fails (raw/unformatted volume), we
    # skip; if the fs type is something we can't mount safely, we skip.
    local FSTYPE
    FSTYPE=$(blkid -s TYPE -o value "$SECONDARY_DEV" 2>/dev/null || echo "")
    if [ -z "$FSTYPE" ]; then
        echo "MOLECULE-RESTORE: WARN no fs detected on $SECONDARY_DEV (raw/unformatted) — skipping"
        return 0
    fi
    echo "MOLECULE-RESTORE: $SECONDARY_DEV fstype=$FSTYPE"

    # Mount read-only. ro prevents a corrupt fs from being modified by
    # mount-time journal replay AND blocks any rsync mistake from
    # writing to the source.
    mkdir -p "$MOUNT_POINT"
    if ! mount -o ro "$SECONDARY_DEV" "$MOUNT_POINT" 2>&1 | sed 's/^/MOLECULE-RESTORE: mount: /'; then
        # mount(8) writes to stderr on success too via -v; we don't pass -v
        # so a non-zero from the pipeline means the mount itself failed.
        :
    fi
    if ! mountpoint -q "$MOUNT_POINT"; then
        echo "MOLECULE-RESTORE: WARN mount of $SECONDARY_DEV failed — skipping restore"
        return 0
    fi
    echo "MOLECULE-RESTORE: mounted $SECONDARY_DEV at $MOUNT_POINT (ro)"

    # rsync the allowlist. -a preserves perms/owner/times/symlinks;
    # --delete makes restore authoritative (a file removed from the
    # prior workspace is also removed from the new one); -x stays on
    # one filesystem (defensive against bind-mounts on the source).
    #
    # Source paths on the snapshot must match prod root layout. The
    # workspace EC2's root filesystem mirrors a normal Linux root, so
    # /configs lives at $MOUNT_POINT/configs and so on.
    local RESTORE_PATHS="configs workspace home/agent/.claude"
    local rsync_failed=0
    for rel in $RESTORE_PATHS; do
        local SRC="$MOUNT_POINT/$rel"
        local DST="/$rel"
        if [ ! -d "$SRC" ]; then
            echo "MOLECULE-RESTORE: source $SRC absent — skipping (likely the prior workspace never wrote it)"
            continue
        fi
        # Ensure dest parent exists. For /home/agent/.claude the parent
        # is /home/agent which is created by useradd; for /configs and
        # /workspace they're volume mount points the platform creates.
        mkdir -p "$(dirname "$DST")"

        echo "MOLECULE-RESTORE: rsync $SRC/ -> $DST/"
        # Capture rsync's REAL exit code. A naive `rsync ... | sed`
        # pipeline returns sed's exit code (0), masking rsync failures
        # — under #!/bin/sh there's no PIPESTATUS, so we route rsync's
        # output through a tempfile and read $? directly. The
        # entrypoint-restore unit test caught this: without it,
        # "MOLECULE-RESTORE: ok" prints even when rsync errors.
        rsync_log="/tmp/molecule-restore-rsync.$$.log"
        rsync -aHAX --delete --numeric-ids "$SRC/" "$DST/" >"$rsync_log" 2>&1
        rsync_rc=$?
        sed 's/^/MOLECULE-RESTORE:   /' "$rsync_log" 2>/dev/null
        rm -f "$rsync_log"
        if [ "$rsync_rc" -eq 0 ]; then
            echo "MOLECULE-RESTORE: ok $DST"
        else
            echo "MOLECULE-RESTORE: WARN rsync to $DST exited $rsync_rc — workspace may be partially restored"
            rsync_failed=1
        fi
    done

    # Leave the mount in place — operator audit evidence, and the
    # secondary volume costs us nothing more (DOT=true at next
    # terminate). Unmount would re-introduce an "is the volume
    # actually attached?" failure mode for no operational gain.

    # Drop marker so subsequent container restarts skip. Even if rsync
    # had partial failures we drop the marker — re-running rsync would
    # NOT recover (the source is the same) and would just spend time on
    # every restart. Operator sees the WARN in docker logs and decides
    # whether to manually rm the marker + restart for a retry.
    : > "$MARKER"
    if [ "$rsync_failed" -eq 0 ]; then
        echo "MOLECULE-RESTORE: complete — marker $MARKER dropped"
    else
        echo "MOLECULE-RESTORE: complete with WARNINGS — marker $MARKER dropped; rm marker + restart for retry"
    fi
}

if [ "$(id -u)" = "0" ]; then
    # Restore-on-recreate runs FIRST in the root branch — before any
    # chown — so rsync's preserved ownership doesn't immediately get
    # re-chowned by the agent-ownership step. (The chown is still
    # needed for the no-restore case + for any subdir rsync didn't
    # touch.) See restore_from_secondary_volume() above for the
    # contract.
    restore_from_secondary_volume

    # Configs volume is created by Docker as root; agent needs write access
    # for plugin installs, memory writes, .auth_token rotation, etc.
    #
    # T4 atomic-co-sequencing contract (RFC internal#456 §10): the T4
    # escalation leg (sudo NOPASSWD + docker group, baked in the
    # Dockerfile) is ADDITIVE. The agent still runs uid-1000 and
    # /configs/.auth_token MUST remain agent-owned — escalation must
    # NOT regress the Hermes list_peers-401 token-ownership class.
    # This chown -R is the agent-ownership half of that contract; the
    # Layer-3 conformance gate asserts owner_uid==1000 on the running
    # container alongside the host-root-reach assertion.
    chown -R agent:agent /configs 2>/dev/null
    # /workspace handling — only chown when the contents are root-owned
    # (typical on Docker Desktop on Windows where host uid maps to 0).
    # On Linux Docker with matching uids the recursive chown is skipped
    # to keep startup fast.
    chown agent:agent /workspace 2>/dev/null || true
    if [ -d /workspace ]; then
        first_entry=$(find /workspace -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)
        if [ -n "$first_entry" ] && [ "$(stat -c '%u' "$first_entry" 2>/dev/null)" = "0" ]; then
            chown -R agent:agent /workspace 2>/dev/null
        fi
        # Pre-create /workspace/.molecule/chat-uploads so the upload
        # handler in workspace/internal_chat_uploads.py never has to
        # mkdir as agent inside a root-owned tree. Without this the
        # first upload after a fresh provision fails with "failed to
        # prepare uploads dir" because the volume mount comes up with
        # root-owned `.molecule` whenever a sibling subsystem (e.g. an
        # adapter writing telemetry, or a workspace runtime that ran
        # before the chown landed) raced ahead. Idempotent: a re-run
        # finds the dir already there, mode 0755 / agent:agent.
        mkdir -p /workspace/.molecule/chat-uploads 2>/dev/null || true
        chown -R agent:agent /workspace/.molecule 2>/dev/null || true
    fi
    # Claude Code session directory — mounted at /root/.claude/sessions by
    # the platform provisioner. Symlink it into agent's home so the SDK
    # finds it when running as agent. The provisioner's mount point is
    # hardcoded to /root/.claude/sessions; we don't want to change the
    # platform contract just for this template.
    #
    # NOTE (T4 perms regression): on FIRST boot the host volume mount for
    # /home/agent/.claude doesn't exist yet — entrypoint creates it and
    # the chown lands inside the `if -d /root/.claude/sessions` guard.
    # On SECOND boot with a populated /home/agent/.claude (sessions/,
    # session-env/, settings.json — any of which the SDK or agent has
    # written between boots) the dir may already be root-owned because
    # the SDK's working files inherited root's uid when written under
    # the prior root segment of an earlier entrypoint, OR because a
    # newer claude-code release writes new subdirs we don't create here.
    # That leaves uid-1000 agent EPERMing on every settings/session write
    # ("permission restrictions" surfaced to the canvas as a generic
    # Bash failure). Fix: create the well-known subdirs idempotently
    # and run the chown unconditionally (no-op when ownership is already
    # correct, fast on small trees). Stub ~/.claude/settings.json too so
    # the agent's introspection (cat ~/.claude/settings.json) succeeds
    # and shows operating mode — bypassPermissions is the canonical
    # mode set programmatically by claude_sdk_executor.py.
    mkdir -p /home/agent/.claude/sessions /home/agent/.claude/session-env
    if [ ! -f /home/agent/.claude/settings.json ]; then
        cat > /home/agent/.claude/settings.json <<'EOF'
{
  "permissions": {"defaultMode": "bypassPermissions"},
  "_note": "Mode is also set programmatically by claude_sdk_executor.py (permission_mode='bypassPermissions'); this file is informational and lets `cat ~/.claude/settings.json` succeed."
}
EOF
    fi
    chown -R agent:agent /home/agent/.claude 2>/dev/null
    if [ -d /root/.claude/sessions ]; then
        chown -R agent:agent /root/.claude 2>/dev/null
        ln -sfn /root/.claude/sessions /home/agent/.claude/sessions
    fi

    # Optional GitHub mirror credential helper setup.
    # GitHub is mirror-only for Molecule; keep this disabled unless an
    # operator explicitly opts a workspace into mirror credentials.
    if [ "${ENABLE_GITHUB_MIRROR_CREDENTIALS:-false}" = "true" ] && [ -x /app/scripts/molecule-git-token-helper.sh ]; then
        git config --global "credential.https://github.com.helper" \
            "!/app/scripts/molecule-git-token-helper.sh"
        git config --global "credential.https://github.com.useHttpPath" true
        if [ -f /root/.gitconfig ]; then
            cp /root/.gitconfig /home/agent/.gitconfig
            chown agent:agent /home/agent/.gitconfig
        fi
    fi
    mkdir -p /home/agent/.molecule-token-cache
    chown agent:agent /home/agent/.molecule-token-cache
    chmod 700 /home/agent/.molecule-token-cache
    mkdir -p /home/agent/Downloads
    chown agent:agent /home/agent/Downloads

    exec gosu agent "$0" "$@"
fi

# Now running as agent (uid 1000)

# Optional background token refresh daemon for GitHub mirror credentials.
if [ "${ENABLE_GITHUB_MIRROR_CREDENTIALS:-false}" = "true" ] && [ -x /app/scripts/molecule-gh-token-refresh.sh ]; then
    nohup bash -c '
        while true; do
            /app/scripts/molecule-gh-token-refresh.sh
            rc=$?
            echo "[molecule-gh-token-refresh] daemon exited rc=$rc — respawning in 30s" >&2
            sleep 30
        done
    ' > /home/agent/.gh-token-refresh.log 2>&1 &
fi

# Third-party provider routing is now handled by adapter.py at boot —
# it reads the `providers:` registry from /configs/config.yaml and sets
# ANTHROPIC_BASE_URL based on the picked MODEL. Adding a new provider
# is a one-line YAML edit (see config.yaml's `providers:` section).
# Operator-set ANTHROPIC_BASE_URL still wins as the escape hatch for
# regional endpoints (e.g. Xiaomi's token-plan-sgp.*, MiniMax's
# api.minimaxi.com China endpoint).

exec molecule-runtime "$@"
