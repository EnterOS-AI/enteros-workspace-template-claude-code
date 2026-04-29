# Known Issues — claude-code Workspace Template

This document tracks unresolved and partially-resolved issues that are known to occur when
running this workspace template. Each entry includes the symptom, affected versions,
workaround, and (where applicable) a link to the upstream or internal tracker.

---

## 1. `CLAUDE_CODE_OAUTH_TOKEN` Missing Causes Silent Auth Failures

**Status:** ✅ **RESOLVED** (2026-04-23)

`adapter.py:setup()` now emits a `logger.warning()` if `CLAUDE_CODE_OAUTH_TOKEN` is absent,
so operators see the problem immediately at startup rather than a silent `AuthenticationError`
on the first LLM call. Fix shipped in PR #1753 (`fix/oauth-token-startup-warning`).

---

## 2. HEARTBEAT Not Emitted — Platform Shows "Silent" Status

**Severity:** Medium
**Affects:** All template versions prior to explicit HEARTBEAT wiring.

**Symptom:**
The Molecule platform activity dashboard shows the workspace as "silent" even
though the agent is actively processing tasks. No heartbeat events arrive at the
platform. The platform may timeout the workspace as inactive.

**Root cause:**
The `entrypoint.sh` launches the adapter but does not configure a HEARTBEAT
interval. The platform relies on periodic POSTs to `/api/v1/heartbeat` to confirm
liveness. Without this, long-running agent tasks (> ~60s) may trigger platform
timeouts.

**Workaround:**
Set `HEARTBEAT_INTERVAL_SECONDS` in the environment (if supported by the adapter):

```bash
export HEARTBEAT_INTERVAL_SECONDS=30
python adapter.py
```

Or, if the adapter does not support this env var, keep agent tasks short (< 60s)
or use `delegate_task_async` to return control immediately.

**Fix:** The adapter should emit a HEARTBEAT event every 30 seconds when running
in platform mode. A future template update will add explicit HEARTBEAT wiring.

---

## 3. `system-prompt.md` Customisations Overwritten on Template Update

**Severity:** Medium
**Affects:** Users who customise `system-prompt.md` directly in the workspace.

**Symptom:**
After pulling a new template version (e.g. `git pull` in a persistent workspace),
the agent's behaviour changes unexpectedly even though `config.yaml` was not
modified. On inspection, `system-prompt.md` has been overwritten with the
template's canonical version.

**Root cause:**
`system-prompt.md` is a template-managed file. When the platform rebuilds or
refreshes the workspace container it copies files from the registered template
tag, overwriting any local customisations.

**Workaround — Option A (recommended):**
Do not edit `system-prompt.md` directly. If the platform supports an override
mechanism, use `MOLECULE_SYSTEM_PROMPT_OVERRIDE` environment variable or the
`system_prompt_override` field in `config.yaml` (platform v1.2+).

**Workaround — Option B:**
Fork the template and pin to a specific tag. Apply your customisations as patches
on top of that tag.

---

## 4. `template_schema_version` Drift After Platform Upgrade

**Severity:** High
**Affects:** Any workspace pinned to a schema version below the platform minimum
after a platform upgrade.

**Symptom:**
The adapter fails to start with:

```
ValidationError: template schema version '1' is not supported.
Minimum supported version: '2'. Please update config.yaml.
```

**Root cause:**
The Molecule platform increments the minimum supported `template_schema_version`
when it makes backward-incompatible changes to the config format. Workspaces that
pin an older schema version will fail validation immediately.

**Workaround:**
After a platform upgrade, edit `config.yaml` and update the
`template_schema_version` field to the new minimum reported in the platform's
release notes:

```yaml
template_schema_version: 2   # change from 1 to 2
```

**Prevention:**
Check the platform release notes before updating the platform. The release
checklist in `CLAUDE.md` includes a step to review the platform's minimum
schema version before tagging a new template release.

**Fix:** Once `template_schema_version` is updated, the adapter starts normally.
No adapter code changes are required for schema-only bumps.
