#!/bin/sh
# platform-agent-entrypoint.sh
#
# Wrapper entrypoint for the molecule-platform-agent image. Two jobs:
#
#   1. PER-FILE /opt→/configs reconcile (the #2919 risk-1+2 fix).
#      When the image bakes /opt/molecule-platform-agent-template/
#      (config.yaml + mcp_servers.yaml + prompts/concierge.md) AND
#      the asset-channel deliver has NOT populated /configs with a
#      matching file, copy the ABSENT file from /opt into /configs.
#      Fill-absent-only — a delivered /configs/<file> ALWAYS wins
#      (we never overwrite). This is the concierge self-host /
#      no-token / partial-fetch safety path: a partial template
#      delivery can't strip the baked identity, because we only
#      FILL what's missing, not REPLACE what's delivered.
#
#      What this is NOT:
#        - NOT a wholesale /configs copy. The previous concern
#          (augment-vs-strip) is moot because we never REPLACE.
#        - NOT a runtime read-fallback. The runtime-side
#          /opt fallback in workspace-runtime load_config
#          (PR #141, commit a432737) covers the empty-/configs
#          case for config.yaml. This wrapper is the OTHER
#          half — it makes /configs CONTAIN the identity content
#          so every reader of /configs (prompts, skills, plugins,
#          ExecRead on /configs/system-prompt.md for the
#          conciergeIdentityPresent probe) sees it. Without this
#          the concierge boots with the right model but an empty
#          system prompt (silently identity-less, Researcher
#          RC 12052 finding) and the in-core identity probe
#          loops-restarts it.
#
#   2. Exec the base image's /entrypoint.sh. Drop-privilege
#      (chown, gosu agent) is unchanged — the base entrypoint
#      does its normal work after we exit.
#
# Why a separate wrapper instead of modifying the base entrypoint:
#   - The base entrypoint (claude-code-default) runs on EVERY
#     claude-code workspace, not just platform-agent. Wiring the
#     /opt reconcile there means a no-op probe on every workspace
#     + risk of regressing ordinary claude-code boot if /opt
#     content shows up unpopulated. Keeping it scoped to the
#     platform-agent image (the ONLY image that bakes /opt/
#     molecule-platform-agent-template) avoids both.
#   - The base image is published on its own cadence; an image-
#     scoped wrapper is decoupled from that.
#
# Idempotency: the per-file copy is safe to re-run (the
#   [ ! -f "$DST/$rel" ] guard skips already-populated files).
#   If the asset-channel deliver lands AFTER the entrypoint
#   fires (mid-boot race), the delivered file wins on the
#   NEXT boot — the entrypoint never overwrites, so it never
#   conflicts.
#
# Fail-soft: if /opt/molecule-platform-agent-template is missing
#   (operator built the platform-agent image without baking the
#   template content), every guard fails, no copy happens, the
#   wrapper falls through to /entrypoint.sh unchanged. The
#   ordinary claude-code boot continues. Loud but non-fatal.

set -eu

SRC="/opt/molecule-platform-agent-template"
DST="/configs"

if [ -d "$SRC" ]; then
    # Make sure DST exists (typically a volume mount; this is a no-op
    # if the volume is already mounted, idempotent if the path is
    # missing for some reason).
    mkdir -p "$DST" 2>/dev/null || true

    filled=""
    # Per-file root-level copy — config.yaml + mcp_servers.yaml.
    # [ ! -f "$DST/$rel" ] is the FILL-ABSENT-ONLY guard: a delivered
    # file at /configs always wins; we only copy when the destination
    # is absent.
    for rel in config.yaml mcp_servers.yaml; do
        if [ -f "$SRC/$rel" ] && [ ! -f "$DST/$rel" ]; then
            cp "$SRC/$rel" "$DST/$rel"
            filled="$filled $rel"
        fi
    done

    # prompts/ is a directory; iterate its top-level files. Same
    # fill-absent-only semantic — a delivered /configs/prompts/foo.md
    # always wins; an absent one is filled from /opt.
    if [ -d "$SRC/prompts" ]; then
        mkdir -p "$DST/prompts" 2>/dev/null || true
        for f in "$SRC/prompts"/*; do
            [ -f "$f" ] || continue
            rel="$(basename "$f")"
            if [ ! -f "$DST/prompts/$rel" ]; then
                cp "$f" "$DST/prompts/$rel"
                filled="$filled prompts/$rel"
            fi
        done
    fi

    if [ -n "$filled" ]; then
        echo "platform-agent: filled absent files at $DST/ from $SRC/:$filled" >&2
    else
        echo "platform-agent: $DST/ already populated (no fills needed); $SRC/ is a no-op safety net this boot" >&2
    fi
else
    # /opt not populated. Not an error — the platform-agent image
    # was built without the template content (operator step). The
    # runtime's /opt fallback in workspace-runtime load_config
    # (PR #141) still covers config.yaml via direct /opt read.
    echo "platform-agent: $SRC absent (image built without template content); skipping /configs reconcile (runtime /opt fallback in load_config still covers config.yaml)" >&2
fi

# Drop into the base image's entrypoint. It does volume-ownership
# fix + gosu agent + exec molecule-runtime — the platform-agent
# image inherits this contract unchanged.
exec /entrypoint.sh "$@"
