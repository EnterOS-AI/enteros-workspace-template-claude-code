# Runbook: Local Development Setup — claude-code Workspace Template

Use this runbook to set up a local development environment for the claude-code
workspace template. It covers cloning, dependency installation, running the adapter
outside Docker, overriding config for dev, building the container, and diagnosing
common problems.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | 3.12 recommended |
| pip | 23+ | |
| Docker | 24+ | |
| Docker Compose | v2 (standalone or compose plugin) | |
| Git | 2.40+ | |
| Gitea access | Current | Required for repository and package downloads |
| Molecule platform access | Token with `workspace:dev` scope | |

---

## Step 1 — Clone the Repository

```bash
git clone https://git.moleculesai.app/molecule-ai/molecule-ai-workspace-template-claude-code.git
cd molecule-ai-workspace-template-claude-code
```

> **Local platform development note:** If you are running `molecule-core` locally with `MOLECULE_IMAGE_REGISTRY` unset, the workspace-server provisioner clones this template repository automatically and `docker build`s it locally. Manual cloning is only required when you are modifying template code itself.

Always branch off `main` for local development:

```bash
git checkout -b feat/your-feature-name
```

---

## Step 2 — Install Dependencies

Create an isolated virtual environment so local dependencies do not modify the
system Python:

```bash
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .\.venv\Scripts\Activate.ps1  # Windows PowerShell
python -m pip install packaging
git clone --depth 1 https://git.moleculesai.app/molecule-ai/molecule-ci.git .molecule-ci-canonical
python .molecule-ci-canonical/scripts/install_workspace_dependencies.py --allow-missing
```

The canonical installer acquires `molecules-workspace-runtime` from the private
Gitea index only, then resolves the remaining public dependencies with the local
runtime wheel already selected. Do not add a second package index to a general
`pip install` command.

Verify the adapter is importable:

```bash
python -c "from adapter import MoleculeClaudeCodeAdapter; print('OK')"
```

---

## Step 3 — Configure Environment Variables

The adapter requires `CLAUDE_CODE_OAUTH_TOKEN` to authenticate with the LLM.
There is no API-key fallback — this variable must be set.

```bash
# Required — OAuth token for the LLM provider
export CLAUDE_CODE_OAUTH_TOKEN="your-oauth-token-here"

# Platform URL (required for agent task polling)
export MOLECULE_PLATFORM_URL="https://platform.molecule.ai"

# Workspace instance ID (assigned by the platform)
export MOLECULE_WORKSPACE_ID="ws-dev-local"

# Optional — override adapter log level
export LOG_LEVEL=DEBUG

# Optional — HEARTBEAT interval in seconds (prevents platform timeout on long tasks)
export HEARTBEAT_INTERVAL_SECONDS=30
```

> **Security note:** Never commit `.env.local` to version control. Create a
> `.env.local` file and ensure it is listed in `.gitignore`.

---

## Step 4 — Dev Overrides in `config.yaml`

The `config.yaml` shipped in the repo is production-oriented. For local dev,
the recommended approach is to pass environment-variable overrides. Alternatively,
create `config.dev.yaml` that the adapter merges on top of `config.yaml`:

```yaml
# config.dev.yaml — local development overrides
runtime_config:
  model: sonnet
  timeout: 300           # shorter timeout for faster dev cycles
```

Apply dev overrides when running locally:

```bash
python adapter.py --config config.yaml --config-override config.dev.yaml
```

If the adapter does not support `--config-override`, set `MOLECULE_CONFIG_OVERLAY`
to the path of the dev config file.

---

## Step 5 — Run the Adapter Locally

Start the adapter in foreground mode:

```bash
python adapter.py
```

Expected startup output:

```
[molecule.adapter] INFO  — resolved config template_schema_version=1
[molecule.adapter] INFO  — runtime: claude-code, model: sonnet
[molecule.adapter] INFO  — OAuth token: present
[molecule.adapter] INFO  — workspace ready, polling https://platform.molecule.ai/api/v1/tasks
```

Press `Ctrl+C` to stop. For background operation:

```bash
nohup python adapter.py > adapter.log 2>&1 &
echo $! > adapter.pid
```

Stop with:

```bash
kill $(cat adapter.pid)
```

---

## Step 6 — Test the Docker Build

Build the dev image:

```bash
docker build \
  --build-arg BUILDKIT_INLINE_CACHE=1 \
  -t molecule-claude-code-workspace:dev \
  .
```

Run a smoke test (adapter starts and reaches idle state):

```bash
docker run --rm \
  --env CLAUDE_CODE_OAUTH_TOKEN \
  --env MOLECULE_PLATFORM_URL \
  --env MOLECULE_WORKSPACE_ID \
  molecule-claude-code-workspace:dev \
  python -c "
from adapter import MoleculeClaudeCodeAdapter
a = MoleculeClaudeCodeAdapter()
a.load_config()
print('smoke test PASSED')
"
```

Full Docker Compose stack:

```bash
docker compose up --build
```

Logs:

```bash
docker compose logs -f workspace
```

Teardown:

```bash
docker compose down -v
```

---

## Common Issues Table

| Symptom | Likely Cause | Resolution |
|---|---|---|
| `401 Unauthorized — Bearer token invalid` | `CLAUDE_CODE_OAUTH_TOKEN` unset or expired | Set a valid token: `export CLAUDE_CODE_OAUTH_TOKEN=...` |
| `anthropic.AuthenticationError` | Wrong token or token lacks required scopes | Verify token has `model:read` and `agent:invoke` scopes |
| Adapter starts but never receives tasks | Wrong `MOLECULE_PLATFORM_URL` or token expired | Check URL; refresh token |
| Platform shows "silent" after ~60s | No HEARTBEAT configured; platform timed out the workspace | Set `HEARTBEAT_INTERVAL_SECONDS=30`; upgrade adapter |
| `docker build` fails during dependency installation | Gitea package or public dependency source is unreachable | Verify access to `git.moleculesai.app` and the public Python index, then rerun the canonical installer |
| `docker run` exits immediately with code 0 | `CLAUDE_CODE_OAUTH_TOKEN` not set | Pass `-e CLAUDE_CODE_OAUTH_TOKEN` or `--env-file .env.local` |
| `ValidationError: template schema version '1' not supported` | Platform minimum schema version increased | Update `template_schema_version` in `config.yaml` to match platform minimum |

---

## IDE Setup (VS Code)

```json
// .vscode/settings.json — create in workspace root
{
  "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python",
  "python.analysis.typeCheckingMode": "basic",
  "files.insertFinalNewline": true,
  "[python]": {
    "editor.formatOnSave": true,
    "editor.defaultFormatter": "charliermarsh.ruff"
  }
}
```
