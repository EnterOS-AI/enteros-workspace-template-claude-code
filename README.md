# Molecule AI workspace template — Claude Code

This repository builds the `claude-code` workspace image used by Molecule AI.
The canonical source is this Gitea repository; the canvas template picker is the
supported way to create a workspace from it.

## Runtime shape

- `entrypoint.sh` prepares the mounted workspace/config directories, exposes
  plugin-provided skills to Claude Code, drops to the `agent` user, and executes
  `molecule-runtime`.
- `adapter.py` resolves the configured model/provider and creates the
  Claude Code executor.
- `claude_sdk_executor.py` owns the Claude Agent SDK session, recovery, and
  channel behavior.
- `config.yaml` is the template's model/provider source. The copy under
  `internal/providers/` is a CI-checked projection of the control-plane
  registry, not a second runtime configuration.

## Authentication

Authentication follows the selected provider. Claude subscription workspaces
can use `CLAUDE_CODE_OAUTH_TOKEN`; direct or compatible provider routes use the
credential names declared in `config.yaml` (for example
`ANTHROPIC_API_KEY`). `MOLECULE_RESOLVED_PROVIDER`, when injected by the
platform, has precedence over heuristic provider selection.

`adapter.py` applies provider-specific endpoint routing. An explicitly supplied
`ANTHROPIC_BASE_URL` remains an override and is not replaced at boot.

Never commit credentials or put them in command-line examples. Configure them
through the workspace/platform secret surfaces.

## Important files

| Path | Purpose |
|---|---|
| `Dockerfile` | Builds the runtime image and installs the private runtime wheel from the Gitea package registry |
| `entrypoint.sh` | Container boot and privilege-drop path |
| `adapter.py` | Provider resolution and runtime adapter |
| `claude_sdk_executor.py` | Claude Agent SDK execution/session behavior |
| `config.yaml` | Template metadata, providers, models, and runtime settings |
| `tests/` | Adapter, entrypoint, provenance, and documentation contracts |
| `tests_conformance/` | SDK-owned adapter conformance suite |

The current file contains `template_schema_version: 1`; change it only with a
corresponding platform contract change and validation.

## Development and delivery

See [`runbooks/local-dev-setup.md`](runbooks/local-dev-setup.md) for commands
that mirror CI. Pull requests run validation and tests. A push to `main` invokes
the repository's `publish-image` workflow, which builds the image, pushes it to
the Gitea OCI registry, and applies the configured pin checks. Do not substitute
a manual registry or direct-main-push procedure.

## License

Business Source License 1.1 — © Molecule AI.
