"""claude-agent-sdk is pinned to the version the build-time hotfix was checked on.

scripts/patch_claude_sdk_2_1_150.py rewrites a block of the installed
claude_agent_sdk/_internal/query.py during the image build and exits 3 when
the block is missing. claude-agent-sdk 0.2.140 (2026-08-18) rewrote that
block. requirements.txt allowed any version >=0.1.58, so every claude-code
image build after that date installed the newest SDK and failed at the patch
step. The runtime 0.4.92 bump (#399) hit it on 2026-09-29 with 0.2.162.
"""

from __future__ import annotations

from pathlib import Path

from packaging.requirements import Requirement

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"

# The SDK version whose _internal/query.py contains the block that
# scripts/patch_claude_sdk_2_1_150.py replaces (its ORIG string). It is also
# the version in the staging image promoted on 2026-08-13 (sha256:12c7f00d).
# Before changing it, read the new SDK's query.py: either the hotfix still
# applies (update ORIG/PATCHED if the block moved) or upstream fixed the bug
# (remove the hotfix). Then change requirements.txt and this constant together.
HOTFIX_VERIFIED_SDK_VERSION = "0.2.137"


def _sdk_requirements() -> list[Requirement]:
    found = []
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        if requirement.name.lower().replace("_", "-") == "claude-agent-sdk":
            found.append(requirement)
    return found


def test_sdk_is_pinned_to_one_exact_version():
    requirements = _sdk_requirements()
    assert len(requirements) == 1, (
        f"expected one claude-agent-sdk requirement, found {requirements}"
    )
    specifiers = list(requirements[0].specifier)
    assert len(specifiers) == 1 and specifiers[0].operator == "==", (
        f"claude-agent-sdk must be an exact `==` pin, not {requirements[0]}: "
        "with a range, each image build installs whatever PyPI published "
        "last, and the build-time hotfix fails when upstream rewrites its block"
    )
    assert "*" not in specifiers[0].version, (
        f"claude-agent-sdk pin must not be a wildcard: {requirements[0]}"
    )


def test_pin_is_the_version_the_hotfix_was_verified_on():
    (requirement,) = _sdk_requirements()
    (specifier,) = list(requirement.specifier)
    assert specifier.version == HOTFIX_VERIFIED_SDK_VERSION, (
        f"requirements.txt pins claude-agent-sdk {specifier.version}, but "
        "scripts/patch_claude_sdk_2_1_150.py was checked against "
        f"{HOTFIX_VERIFIED_SDK_VERSION}. Read claude_agent_sdk/_internal/"
        "query.py in the new version, update or remove the hotfix, then "
        "update HOTFIX_VERIFIED_SDK_VERSION."
    )
