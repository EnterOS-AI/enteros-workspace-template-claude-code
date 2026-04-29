# template-claude-code-default

Molecule AI workspace template for the **claude-code-default** runtime.

## Usage

### In Molecule AI canvas
Select this template when creating a new workspace — it appears in the template picker automatically.

### From a URL (community install)
Paste this URL when creating a workspace:
```
github://Molecule-AI/template-claude-code-default
```

## Files
- `config.yaml` — workspace configuration (runtime, model, skills, etc.)
- `system-prompt.md` — agent system prompt (if present)

## Auth paths

| Path | Env var(s) | Where to get the key |
|---|---|---|
| OAuth (Claude Code subscription) | `CLAUDE_CODE_OAUTH_TOKEN` | `claude login` |
| Anthropic API (direct) | `ANTHROPIC_API_KEY` | console.anthropic.com |
| Third-party Anthropic-compat (e.g. Xiaomi MiMo) | `ANTHROPIC_API_KEY` (provider's key) | provider console |

For third-party providers, `entrypoint.sh` rewrites `ANTHROPIC_BASE_URL` based on the selected `MODEL` so the `claude` CLI routes there. Currently auto-routes `mimo-*` models to `https://api.xiaomimimo.com/anthropic`. Operator-set `ANTHROPIC_BASE_URL` is never overridden.

## Schema version
`template_schema_version: 1` — compatible with Molecule AI platform v1.x.

## License
Business Source License 1.1 — © Molecule AI.
