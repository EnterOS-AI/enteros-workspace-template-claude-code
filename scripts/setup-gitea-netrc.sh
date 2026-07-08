#!/usr/bin/env bash
# setup-gitea-netrc.sh — safe Gitea auth setup for agent runtimes.
#
# Problem: `curl -u "<user>:<token>"` leaks the token into process argv and
# platform activity logs. curl can read credentials from ~/.netrc instead,
# keeping the token out of argv.
#
# This script writes ~/.netrc from the agent's existing env credentials
# (GIT_HTTP_USERNAME / GIT_HTTP_PASSWORD) so that subsequent `curl --netrc`
# calls authenticate without exposing the token on the command line.
#
# Security: the token is written to a tempfile that is created mode 0600
# BEFORE any credential bytes land, then moved atomically into place. No
# intermediate file ever holds the token at a permission wider than 0600,
# regardless of the caller's umask.
#
# Owner/harness integration: run this once per agent session startup, before
# any Gitea API calls. The file is created with mode 600.

set -euo pipefail

# _create_private_tempfile <dir>: create a tempfile with mode 0600 and echo
# its path. Exposed as a function so tests can verify the create-before-write
# ordering independently.
_create_private_tempfile() {
  local dir="${1:-${TMPDIR:-/tmp}}"
  local tmp
  tmp=$(mktemp "$dir/.netrc.tmp.XXXXXX")
  # Guarantee 0600 before any caller writes sensitive bytes. mktemp may create
  # the file with 0600 on most systems, but we set it explicitly so the script
  # is umask-independent and auditable.
  chmod 600 "$tmp"
  printf '%s' "$tmp"
}

# _write_netrc <path> <host> <user> <pass>: write a netrc entry to the
# already-private file. Should only be called after the file is confirmed 0600.
_write_netrc() {
  local path="$1" host="$2" user="$3" pass="$4"
  cat > "$path" <<EOF
machine $host
login $user
password $pass
EOF
}

# main: orchestrate writing ~/.netrc from env credentials.
main() {
  local netrc="${HOME}/.netrc"
  local host="${GITEA_HOST:-git.moleculesai.app}"
  local user="${GIT_HTTP_USERNAME:-}"
  local pass="${GIT_HTTP_PASSWORD:-}"

  if [ -z "$user" ] || [ -z "$pass" ]; then
    echo "::warning::GIT_HTTP_USERNAME or GIT_HTTP_PASSWORD not set; skipping ~/.netrc setup. Gitea curl calls will need an alternative safe-auth method." >&2
    return 0
  fi

  # Create a private tempfile in the same directory as the destination so the
  # final rename is atomic and cannot cross filesystems.
  local netrc_dir
  netrc_dir=$(dirname "$netrc")
  mkdir -p "$netrc_dir"
  local tmp
  tmp=$(_create_private_tempfile "$netrc_dir")

  # Write credentials only after the file is confirmed private.
  _write_netrc "$tmp" "$host" "$user" "$pass"

  # Atomic replace: readers either see the old file (or none) or the new file;
  # they never see a partially-written or under-permissioned file.
  mv -f "$tmp" "$netrc"

  # Defensive: ensure the final file is 0600 even if mktemp/umask/mv somehow
  # widened permissions (e.g., ACLs).
  chmod 600 "$netrc"

  echo "wrote $netrc (mode 600) for $host"
}

# Run main only when executed, not when sourced by tests.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi
