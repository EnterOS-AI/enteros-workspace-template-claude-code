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

if [ "$(id -u)" = "0" ]; then
    # Configs volume is created by Docker as root; agent needs write access
    # for plugin installs, memory writes, .auth_token rotation, etc.
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
    fi
    # Claude Code session directory — mounted at /root/.claude/sessions by
    # the platform provisioner. Symlink it into agent's home so the SDK
    # finds it when running as agent. The provisioner's mount point is
    # hardcoded to /root/.claude/sessions; we don't want to change the
    # platform contract just for this template.
    mkdir -p /home/agent/.claude
    if [ -d /root/.claude/sessions ]; then
        chown -R agent:agent /root/.claude /home/agent/.claude 2>/dev/null
        ln -sfn /root/.claude/sessions /home/agent/.claude/sessions
    fi

    # GitHub credential helper setup (fix #1933 / #1866 / #547).
    # Runs as root so the global gitconfig is written before we drop to agent.
    # The helper fetches fresh GitHub App installation tokens from the
    # platform API on every git push/clone, with caching + env-var fallback.
    if [ -x /app/scripts/molecule-git-token-helper.sh ]; then
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

    exec gosu agent "$0" "$@"
fi

# Now running as agent (uid 1000)

# Background token refresh daemon — keeps `gh` CLI auth + credential helper
# cache warm across the ~60 min GitHub App installation token TTL. Wrapped
# in a respawn loop so a daemon crash doesn't silently leave the workspace
# stuck on an expired token (which is exactly how #1933 was discovered).
if [ -x /app/scripts/molecule-gh-token-refresh.sh ]; then
    nohup bash -c '
        while true; do
            /app/scripts/molecule-gh-token-refresh.sh
            rc=$?
            echo "[molecule-gh-token-refresh] daemon exited rc=$rc — respawning in 30s" >&2
            sleep 30
        done
    ' > /home/agent/.gh-token-refresh.log 2>&1 &
fi

# Initial gh auth — primes the CLI with whatever GH_TOKEN/GITHUB_TOKEN was
# injected at provision time, so commands work in the ~60s window before the
# background daemon's first refresh fires.
if [ -n "${GITHUB_TOKEN:-}" ]; then
    echo "${GITHUB_TOKEN}" | gh auth login --hostname github.com --with-token 2>/dev/null || true
elif [ -n "${GH_TOKEN:-}" ]; then
    echo "${GH_TOKEN}" | gh auth login --hostname github.com --with-token 2>/dev/null || true
fi

exec molecule-runtime "$@"
