"""Absence-guard tests for the decommissioned baked platform-agent image.

The platform-agent image (claude-code base + baked org-management MCP) was
retired in favor of the molecule-platform-mcp plugin installed on the ordinary
claude-code runtime image. These tests fail closed if any of the baked
artifacts reappear, so a revert or cherry-pick cannot silently re-bake the
image.
"""

import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))


BAKED_ARTIFACTS = [
    "Dockerfile.platform-agent",
    "scripts/platform-agent-entrypoint.sh",
    "tests/test_platform_agent_entrypoint.sh",
]


def _artifact_path(name: str) -> str:
    return os.path.join(REPO_ROOT, name)


@pytest.mark.parametrize("artifact", BAKED_ARTIFACTS)
def test_baked_platform_agent_artifact_is_absent(artifact: str) -> None:
    """FAIL if a baked platform-agent file reappears in the repo."""
    path = _artifact_path(artifact)
    assert not os.path.exists(path), (
        f"Baked platform-agent artifact reappeared: {path}. "
        "The platform-agent image is decommissioned; use the molecule-platform-mcp "
        "plugin on the ordinary claude-code runtime image instead."
    )


def test_publish_image_workflow_has_no_platform_agent_job() -> None:
    """FAIL if publish-image.yml reintroduces a platform-agent build/promote job."""
    workflow_path = os.path.join(REPO_ROOT, ".gitea", "workflows", "publish-image.yml")
    with open(workflow_path, "r", encoding="utf-8") as f:
        content = f.read()

    banned_job_keys = [
        "publish-platform-agent",
        "promote-platform-agent-pin",
    ]
    for key in banned_job_keys:
        assert f"{key}:" not in content, (
            f"Banned platform-agent job '{key}' reappeared in publish-image.yml. "
            "The molecule-platform-agent image build is decommissioned."
        )

    banned_image_refs = [
        "molecule-platform-agent",
        "MOLECULE_PLATFORM_AGENT_IMAGE_BAKED",
    ]
    for ref in banned_image_refs:
        assert ref not in content, (
            f"Banned platform-agent reference '{ref}' reappeared in publish-image.yml. "
            "The molecule-platform-agent image build is decommissioned."
        )


def test_dockerfile_has_no_baked_mcp_marker() -> None:
    """FAIL if the legitimate claude-code Dockerfile reintroduces baked MCP wiring."""
    dockerfile_path = os.path.join(REPO_ROOT, "Dockerfile")
    with open(dockerfile_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "MOLECULE_PLATFORM_AGENT_IMAGE_BAKED" not in content, (
        "The legitimate claude-code Dockerfile must not carry the platform-agent "
        "baked-image marker. The plugin-MCP path is the de-baked runtime."
    )
    assert "molecule-platform-mcp" not in content or "plugin" in content.lower(), (
        "The legitimate claude-code Dockerfile must not bake the platform MCP "
        "server into the image. Install it as a plugin at runtime."
    )
