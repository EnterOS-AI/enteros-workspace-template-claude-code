"""Regression tests for fail-loud runtime-image pin readback."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
PUBLISH_WORKFLOW = ROOT / ".gitea" / "workflows" / "publish-image.yml"
VERIFY_SCRIPT = ROOT / ".gitea" / "scripts" / "verify-runtime-pin.sh"
TEMPLATE_NAME = "claude-code"
CANONICAL_VERIFY_SHA256 = (
    "55cc7fc0ecff3eb75ecb1be7895dd018fa921101b863f987c2220d47d2cc9bf0"
)


def _job(workflow: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
        workflow,
    )
    assert match is not None, f"job not found: {name}"
    return match.group(0)


def _run_verifier(tmp_path: Path, *, body: str, token: str = "test-secret"):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
out=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w|-H) shift 2 ;;
    *) shift ;;
  esac
done
[ -n "$out" ] || { echo "fake curl: missing -o" >&2; exit 2; }
printf '%s' "$FAKE_CURL_BODY" > "$out"
printf '%s' "${FAKE_CURL_CODE:-200}"
"""
    )
    fake_curl.chmod(0o755)
    expected_digest = "sha256:" + "a" * 64
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "CP_HOST": "staging-api.moleculesai.app",
            "CP_ADMIN_API_TOKEN": token,
            "TEMPLATE_NAME": TEMPLATE_NAME,
            "EXPECTED_DIGEST": expected_digest,
            "ENV_NAME": "staging",
            "TOKEN_SECRET_NAME": "CP_STAGING_ADMIN_API_TOKEN",
            "FAKE_CURL_BODY": body,
            "FAKE_CURL_CODE": "200",
        }
    )
    result = subprocess.run(
        ["bash", str(VERIFY_SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, expected_digest


def test_publish_workflow_reads_back_exact_pushed_digest_after_staging_promote() -> None:
    workflow = PUBLISH_WORKFLOW.read_text()
    verify_job = _job(workflow, "verify-pin")

    assert "needs: [resolve-version, publish, promote-pin]" in verify_job
    assert "if: ${{ success() && github.ref == 'refs/heads/main' }}" in verify_job
    assert "env_name: staging" in verify_job
    assert "cp_host: staging-api.moleculesai.app" in verify_job
    assert "token_secret:" not in verify_job
    assert "env_name: prod" not in verify_job
    assert f"TEMPLATE_NAME: {TEMPLATE_NAME}" in verify_job
    assert "EXPECTED_DIGEST: ${{ needs.publish.outputs.digest }}" in verify_job
    assert (
        "CP_ADMIN_API_TOKEN: ${{ secrets.CP_STAGING_ADMIN_API_TOKEN }}" in verify_job
    )
    assert "TOKEN_SECRET_NAME: CP_STAGING_ADMIN_API_TOKEN" in verify_job
    assert "secrets[matrix." not in verify_job
    assert "run: bash .gitea/scripts/verify-runtime-pin.sh" in verify_job


def test_pin_verifier_is_the_canonical_byte_identical_guard() -> None:
    assert hashlib.sha256(VERIFY_SCRIPT.read_bytes()).hexdigest() == (
        CANONICAL_VERIFY_SHA256
    )


def test_pin_verifier_accepts_only_the_expected_digest(tmp_path: Path) -> None:
    expected_digest = "sha256:" + "a" * 64
    body = (
        '{"pins":[{"template_name":"'
        + TEMPLATE_NAME
        + '","region":"global","image_digest":"'
        + expected_digest
        + '"}]}'
    )

    result, _ = _run_verifier(tmp_path, body=body)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_pin_verifier_fails_loud_on_digest_mismatch_without_leaking_token(
    tmp_path: Path,
) -> None:
    token = "pin-readback-test-secret"
    wrong_digest = "sha256:" + "b" * 64
    body = (
        '{"pins":[{"template_name":"'
        + TEMPLATE_NAME
        + '","region":"global","image_digest":"'
        + wrong_digest
        + '"}]}'
    )

    result, _ = _run_verifier(tmp_path, body=body, token=token)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "PIN MISMATCH" in output
    assert token not in output


def test_pin_verifier_fails_closed_when_staging_secret_is_empty(tmp_path: Path) -> None:
    result, _ = _run_verifier(tmp_path, body='{"pins":[]}', token="")

    assert result.returncode != 0
    assert "CP_STAGING_ADMIN_API_TOKEN secret not configured" in (
        result.stdout + result.stderr
    )
