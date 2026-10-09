#!/usr/bin/env python3
"""Separate the private runtime requirement from the public pip solve."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import InvalidVersion, Version


RUNTIME_PROJECT = "molecules-workspace-runtime"
RETIRED_RUNTIME_PROJECT = "molecule-ai-workspace-runtime"
_RUNTIME_NAME = canonicalize_name(RUNTIME_PROJECT)
_RETIRED_RUNTIME_NAME = canonicalize_name(RETIRED_RUNTIME_PROJECT)


class RuntimeRequirementError(ValueError):
    """Raised when requirements can bypass private runtime acquisition."""


def _requirement_text(raw_line: str) -> str:
    if raw_line.rstrip().endswith("\\"):
        raise RuntimeRequirementError(
            "backslash continuations are unsupported at the runtime trust boundary"
        )
    return re.split(r"(?<!\S)#", raw_line, maxsplit=1)[0].strip()


def _runtime_requirement(requirement: Requirement, line_number: int) -> str:
    if requirement.url:
        raise RuntimeRequirementError(
            f"requirements.txt:{line_number}: `{RUNTIME_PROJECT}` direct URLs "
            "are not allowed"
        )
    if requirement.extras or requirement.marker:
        raise RuntimeRequirementError(
            f"requirements.txt:{line_number}: `{RUNTIME_PROJECT}` must not use "
            "extras or markers"
        )
    return f"{RUNTIME_PROJECT}{requirement.specifier}"


def prepare_runtime_requirements(
    source: Path,
    filtered: Path,
    *,
    runtime_version: str = "",
) -> str:
    """Return the private requirement and write public-only requirements."""
    runtime_requirements: list[str] = []
    public_lines: list[str] = []

    for line_number, raw_line in enumerate(
        source.read_text().splitlines(keepends=True),
        1,
    ):
        text = _requirement_text(raw_line)
        if not text:
            public_lines.append(raw_line)
            continue
        if text.startswith("-"):
            raise RuntimeRequirementError(
                f"requirements.txt:{line_number}: pip directives, includes, "
                "constraints, editables, and index options are unsupported"
            )

        try:
            requirement = Requirement(text)
        except InvalidRequirement as exc:
            raise RuntimeRequirementError(
                f"requirements.txt:{line_number}: unsupported or invalid requirement"
            ) from exc

        name = canonicalize_name(requirement.name)
        if name == _RETIRED_RUNTIME_NAME:
            raise RuntimeRequirementError(
                f"requirements.txt:{line_number}: retired runtime distribution "
                f"`{RETIRED_RUNTIME_PROJECT}` is not allowed"
            )
        if name != _RUNTIME_NAME:
            if requirement.url:
                raise RuntimeRequirementError(
                    f"requirements.txt:{line_number}: direct URLs are unsupported "
                    "in the public dependency solve"
                )
            public_lines.append(raw_line)
            continue

        runtime_requirements.append(_runtime_requirement(requirement, line_number))

    if len(runtime_requirements) != 1:
        raise RuntimeRequirementError(
            f"requirements.txt must declare `{RUNTIME_PROJECT}` exactly once; "
            f"found {len(runtime_requirements)}"
        )

    result = runtime_requirements[0]
    if runtime_version:
        try:
            version = Version(runtime_version)
        except InvalidVersion as exc:
            raise RuntimeRequirementError(
                f"invalid RUNTIME_VERSION: {runtime_version!r}"
            ) from exc
        result = f"{RUNTIME_PROJECT}=={version}"

    filtered.write_text("".join(public_lines))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("filtered", type=Path)
    parser.add_argument("--runtime-version", default="")
    args = parser.parse_args()

    try:
        requirement = prepare_runtime_requirements(
            args.source,
            args.filtered,
            runtime_version=args.runtime_version,
        )
    except RuntimeRequirementError as exc:
        parser.error(str(exc))
    print(requirement)


if __name__ == "__main__":
    main()
