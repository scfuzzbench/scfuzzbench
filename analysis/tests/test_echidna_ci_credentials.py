import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import echidna_ci_credentials as credentials


PARAMETER = "/scfuzzbench/test/actions_token"
REGION = "us-east-1"


def aws_response(metadata=None, *, error=""):
    if metadata is None:
        metadata = {"Name": PARAMETER, "Type": "SecureString"}
    return subprocess.CompletedProcess(
        ["aws"], 1 if error else 0, json.dumps(metadata), error
    )


class EchidnaCredentialTests(unittest.TestCase):
    def test_checks_exact_parameter_and_region_without_decryption_or_value_output(self):
        with mock.patch.object(credentials.subprocess, "run", return_value=aws_response()) as run:
            self.assertEqual(PARAMETER, credentials.validate_parameter(f" {PARAMETER} ", REGION))
        command = run.call_args.args[0]
        self.assertEqual(PARAMETER, command[command.index("--name") + 1])
        self.assertEqual(REGION, command[command.index("--region") + 1])
        self.assertIn("--no-with-decryption", command)
        self.assertNotIn("--with-decryption", command)
        self.assertEqual("Parameter.{Name:Name,Type:Type}", command[command.index("--query") + 1])
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_missing_parameter_fails_launch_with_actionable_error(self):
        error = "An error occurred (ParameterNotFound): sensitive diagnostic data"
        stderr = io.StringIO()
        with mock.patch.object(credentials.subprocess, "run", return_value=aws_response(error=error)):
            with contextlib.redirect_stderr(stderr):
                code = credentials.main(["check", "--parameter", PARAMETER, "--region", REGION])
        self.assertEqual(1, code)
        self.assertIn(f"{PARAMETER} was not found in {REGION}", stderr.getvalue())
        self.assertIn("AWS owner", stderr.getvalue())
        self.assertNotIn("sensitive diagnostic data", stderr.getvalue())

    def test_access_denied_does_not_claim_parameter_is_missing(self):
        response = aws_response(error="An error occurred (AccessDeniedException): private details")
        with mock.patch.object(credentials.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(credentials.ValidationError, "need ssm:GetParameter") as error:
                credentials.validate_parameter(PARAMETER, REGION)
        self.assertNotIn("not found", str(error.exception))
        self.assertNotIn("private details", str(error.exception))

    def test_rejects_plaintext_or_wrong_parameter_metadata(self):
        for metadata in (
            {"Name": PARAMETER, "Type": "String", "Value": "secret"},
            {"Name": PARAMETER, "Type": "StringList", "Value": "secret"},
            {"Name": "/scfuzzbench/wrong/token", "Type": "SecureString"},
            {}, [], None,
        ):
            with self.subTest(metadata=metadata):
                response = subprocess.CompletedProcess(["aws"], 0, json.dumps(metadata), "")
                with mock.patch.object(credentials.subprocess, "run", return_value=response):
                    with self.assertRaises(credentials.ValidationError) as error:
                        credentials.validate_parameter(PARAMETER, REGION)
                self.assertNotIn("secret", str(error.exception))

    def test_invalid_or_unconfigured_inputs_do_not_call_aws(self):
        for parameter, region in (
            ("", REGION), ("/elsewhere/token", REGION), ("/scfuzzbench/", REGION),
            ("/scfuzzbench/" + "a" * 200, REGION), (PARAMETER, ""),
        ):
            with self.subTest(parameter=parameter, region=region):
                with mock.patch.object(credentials.subprocess, "run") as run:
                    with self.assertRaises(credentials.ValidationError):
                        credentials.validate_parameter(parameter, region)
                run.assert_not_called()

    def test_unavailable_cli_timeout_and_bad_responses_fail_closed(self):
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired(["aws"], 30)):
            with mock.patch.object(credentials.subprocess, "run", side_effect=failure):
                with self.assertRaises(credentials.ValidationError):
                    credentials.validate_parameter(PARAMETER, REGION)
        for response in (
            aws_response(error="ExpiredToken: private details"),
            subprocess.CompletedProcess(["aws"], 0, "private details", ""),
        ):
            with mock.patch.object(credentials.subprocess, "run", return_value=response):
                with self.assertRaises(credentials.ValidationError) as error:
                    credentials.validate_parameter(PARAMETER, REGION)
            self.assertNotIn("private details", str(error.exception))

    def test_docs_publish_only_verified_name(self):
        response = aws_response({"Name": PARAMETER, "Type": "SecureString", "Value": "secret"})
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated" / "defaults.json"
            with mock.patch.object(credentials.subprocess, "run", return_value=response):
                code = credentials.main([
                    "docs", "--parameter", PARAMETER, "--region", REGION, "--output", str(output),
                ])
            self.assertEqual(0, code)
            self.assertEqual({"echidna_ci_token_ssm_parameter_name": PARAMETER}, json.loads(output.read_text()))

    def test_docs_clear_stale_default_when_missing_unreadable_or_unconfigured(self):
        for parameter, error in (
            (PARAMETER, "ParameterNotFound"),
            (PARAMETER, "AccessDeniedException"),
            ("", ""),
        ):
            with self.subTest(parameter=parameter, error=error):
                with tempfile.TemporaryDirectory() as directory:
                    output = Path(directory) / "defaults.json"
                    output.write_text(json.dumps({"echidna_ci_token_ssm_parameter_name": PARAMETER}))
                    stderr = io.StringIO()
                    with mock.patch.object(credentials.subprocess, "run", return_value=aws_response(error=error)):
                        with contextlib.redirect_stderr(stderr):
                            code = credentials.main([
                                "docs", "--parameter", parameter, "--region", REGION, "--output", str(output),
                            ])
                    self.assertEqual(0, code)
                    self.assertEqual({"echidna_ci_token_ssm_parameter_name": ""}, json.loads(output.read_text()))
                    self.assertIn("::warning::", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
