#!/usr/bin/env python3
"""Check the Echidna token parameter without decrypting or publishing its value."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


class ValidationError(RuntimeError):
    """The configured token parameter could not be verified."""


def validate_parameter(parameter: str, region: str) -> str:
    parameter = parameter.strip()
    if len(parameter) > 200 or not re.fullmatch(r"/scfuzzbench/[A-Za-z0-9_./-]+", parameter):
        raise ValidationError(
            "Set the Echidna CI token parameter to a SecureString path under "
            "/scfuzzbench/ (maximum 200 characters)."
        )
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-\d+", region):
        raise ValidationError("Set a valid AWS region for the token parameter check.")

    try:
        result = subprocess.run(
            [
                "aws", "ssm", "get-parameter",
                "--region", region,
                "--name", parameter,
                "--no-with-decryption",
                "--query", "Parameter.{Name:Name,Type:Type}",
                "--output", "json",
                "--no-cli-pager",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError(
            "Could not run the AWS parameter check; verify AWS CLI access and retry."
        ) from exc

    if result.returncode:
        # Never echo raw AWS output: it can contain sensitive diagnostic data.
        if "ParameterNotFound" in result.stderr:
            raise ValidationError(
                f"Echidna CI token parameter {parameter} was not found in {region}. "
                "Ask the AWS owner to create it as a SecureString or supply the "
                "existing parameter name in this account and region."
            )
        if "AccessDenied" in result.stderr:
            raise ValidationError(
                f"Cannot verify Echidna CI token parameter {parameter} in {region}: "
                "the workflow credentials need ssm:GetParameter on this parameter."
            )
        raise ValidationError(
            f"Could not verify Echidna CI token parameter {parameter} in {region}; "
            "check AWS credentials, region, and SSM availability."
        )

    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError("AWS returned invalid parameter metadata.") from exc
    if not isinstance(metadata, dict) or metadata.get("Name") != parameter:
        raise ValidationError("AWS did not return metadata for the requested parameter.")
    if metadata.get("Type") != "SecureString":
        raise ValidationError(
            f"Echidna CI token parameter {parameter} in {region} must be a SecureString."
        )
    return parameter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "docs"):
        command = commands.add_parser(name)
        command.add_argument("--parameter", default="")
        command.add_argument("--region", required=True)
        if name == "docs":
            command.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    verified = ""
    try:
        verified = validate_parameter(args.parameter, args.region)
    except ValidationError as exc:
        if args.command == "check":
            print(f"Echidna CI credential preflight failed: {exc}", file=sys.stderr)
            return 1
        print(
            f"::warning::Echidna CI default is unavailable: {exc} "
            "Set repository variable ECHIDNA_CI_TOKEN_SSM_PARAMETER_NAME to a "
            "verified parameter name and rebuild the docs.",
            file=sys.stderr,
        )

    if args.command == "docs":
        # Always replace the payload, including after a failed check, so a stale
        # parameter name cannot remain available in the generated request form.
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps({"echidna_ci_token_ssm_parameter_name": verified}, indent=2) + "\n",
            encoding="utf-8",
        )
    elif verified:
        print(f"Verified Echidna CI SecureString parameter {verified} in {args.region}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
