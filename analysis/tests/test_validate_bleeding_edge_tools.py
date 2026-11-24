import contextlib
import importlib.util
import io
import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock


def load_validator():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "validate_bleeding_edge_tools.py"
    spec = importlib.util.spec_from_file_location("validate_bleeding_edge_tools", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load spec for {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BleedingEdgeToolValidationTests(unittest.TestCase):
    def setUp(self):
        self.module = load_validator()
        self.commit = "9680bb1ef8bf0e11c00fff3e29c5f244d6eb1c85"
        self.digest = "f80a8ae4277b05d33f53a68dafa81c0f82d896da4712a164a577318b52a50d73"
        self.run = {
            "status": "completed",
            "conclusion": "success",
            "head_sha": self.commit,
            "repository": {"full_name": "crytic/echidna"},
        }
        self.artifacts = {
            "total_count": 1,
            "artifacts": [
                {
                    "id": 8249501578,
                    "name": "echidna-Linux",
                    "digest": f"sha256:{self.digest}",
                    "expired": False,
                    "expires_at": "2999-01-01T00:00:00Z",
                    "workflow_run": {"head_sha": self.commit},
                }
            ],
        }

    def fetch_metadata_request(self, url, env):
        with mock.patch.dict(self.module.os.environ, env, clear=True):
            with mock.patch.object(self.module.urllib.request, "urlopen") as opener:
                opener.return_value.__enter__.return_value = io.BytesIO(b'{"ok": true}')
                self.assertEqual({"ok": True}, self.module.fetch_json(url))
        return opener.call_args.args[0]

    def test_authenticates_github_metadata_with_workflow_token_or_cli_fallback(self):
        url = "https://api.github.com/repos/crytic/echidna/actions/runs/44184195243"
        for env, expected in (
            ({"GITHUB_TOKEN": "workflow-token", "GH_TOKEN": "cli-token"}, "workflow-token"),
            ({"GH_TOKEN": "cli-token"}, "cli-token"),
            ({"GITHUB_TOKEN": "  ", "GH_TOKEN": " cli-token "}, "cli-token"),
        ):
            with self.subTest(env=env):
                request = self.fetch_metadata_request(url, env)
                self.assertEqual(f"Bearer {expected}", request.get_header("Authorization"))

    def test_local_public_metadata_lookup_still_works_without_token(self):
        request = self.fetch_metadata_request("https://api.github.com/repos/crytic/echidna", {})
        self.assertIsNone(request.get_header("Authorization"))

    def test_github_token_is_not_sent_to_go_or_other_origins(self):
        for url in (
            "https://go.dev/dl/?mode=json&include=all",
            "https://api.github.com.example.org/metadata",
            "http://api.github.com/repos/crytic/echidna",
            "https://api.github.com@other.example/metadata",
        ):
            with self.subTest(url=url):
                request = self.fetch_metadata_request(url, {"GITHUB_TOKEN": "workflow-token"})
                self.assertIsNone(request.get_header("Authorization"))

    def test_github_authorization_is_not_forwarded_by_redirect_handler(self):
        request = self.fetch_metadata_request(
            "https://api.github.com/repos/crytic/echidna",
            {"GITHUB_TOKEN": "workflow-token"},
        )
        redirected = urllib.request.HTTPRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, "https://other.example/metadata",
        )
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_rate_limit_remains_fatal_without_anonymous_retry_or_token_disclosure(self):
        url = "https://api.github.com/repos/crytic/echidna/actions/runs/44184195243"
        failure = urllib.error.HTTPError(url, 403, "rate limit exceeded", {}, None)
        self.addCleanup(failure.close)
        with mock.patch.dict(self.module.os.environ, {"GITHUB_TOKEN": "private-token"}, clear=True):
            with mock.patch.object(self.module.urllib.request, "urlopen", side_effect=failure) as opener:
                with self.assertRaisesRegex(self.module.ValidationError, "rate limit exceeded") as error:
                    self.module.fetch_json(url)
        opener.assert_called_once()
        self.assertNotIn("private-token", str(error.exception))

    def validate_echidna(self, artifact_name="echidna-Linux"):
        return self.module.validate_echidna_artifact(
            repo_url="https://github.com/crytic/echidna",
            run_id="29156015131",
            artifact_name=artifact_name,
            artifact_sha256=self.digest,
            expected_commit=self.commit,
        )

    def test_accepts_exact_successful_artifact_pin(self):
        with mock.patch.object(self.module, "github_json", side_effect=[self.run, self.artifacts]):
            result = self.validate_echidna()

        self.assertEqual(self.commit, result["commit"])
        self.assertEqual("8249501578", result["artifact_id"])
        self.assertEqual(f"sha256:{self.digest}", result["artifact_digest"])

    def test_accepts_redistributable_fuzzer_archive(self):
        name = "echidna-redistributable-x86_64-linux"
        artifacts = {"artifacts": [dict(self.artifacts["artifacts"][0], name=name)]}
        with mock.patch.object(self.module, "github_json", side_effect=[self.run, artifacts]):
            self.assertEqual(name, self.validate_echidna(name)["artifact_name"])

    def test_rejects_auxiliary_unknown_and_other_architecture_artifacts_before_api(self):
        for name in (
            "echidna-testsuite-Linux", "echidna-testsuite-linux-amd64",
            "echidna-checksums-linux-amd64", "digests-linux-amd64",
            "echidna-redistributable-aarch64-linux", "echidna-linux-arm64",
            "echidna-Windows", "echidna-macOS", "my-echidna-linux-bundle",
        ):
            with self.subTest(name=name):
                with mock.patch.object(self.module, "github_json") as api:
                    with self.assertRaisesRegex(self.module.ValidationError, "unsupported Echidna CI artifact"):
                        self.validate_echidna(name)
                api.assert_not_called()

    def test_cli_rejects_testsuite_in_variant_after_validating_primary_build(self):
        argv = [
            "validate_bleeding_edge_tools.py",
            "--echidna-ci-repo", "https://github.com/crytic/echidna",
            "--echidna-ci-run-id", "29156015131",
            "--echidna-ci-artifact-name", "echidna-Linux",
            "--echidna-ci-artifact-sha256", self.digest,
            "--echidna-ci-commit", self.commit,
            "--fuzzer-variants-json", json.dumps([{
                "key": "echidna-pr-1654", "base": "echidna",
                "ci": {
                    "run_id": "45395915062", "artifact_name": "echidna-testsuite-Linux",
                    "artifact_sha256": self.digest, "commit": self.commit,
                },
            }]),
        ]
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", argv):
            with mock.patch.object(self.module, "github_json", side_effect=[self.run, self.artifacts]) as api:
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as error:
                        self.module.main()
        self.assertEqual(1, error.exception.code)
        self.assertIn("unsupported Echidna CI artifact", stderr.getvalue())
        self.assertEqual(2, api.call_count)

    def test_rejects_run_commit_drift(self):
        changed = dict(self.run, head_sha="0" * 40)
        with mock.patch.object(self.module, "github_json", side_effect=[changed, self.artifacts]):
            with self.assertRaisesRegex(self.module.ValidationError, "run commit drift"):
                self.validate_echidna()

    def test_rejects_expired_artifact(self):
        artifacts = {
            **self.artifacts,
            "artifacts": [{**self.artifacts["artifacts"][0], "expired": True}],
        }
        with mock.patch.object(self.module, "github_json", side_effect=[self.run, artifacts]):
            with self.assertRaisesRegex(self.module.ValidationError, "expired"):
                self.validate_echidna()

    def test_rejects_artifact_digest_drift(self):
        artifacts = {
            **self.artifacts,
            "artifacts": [{**self.artifacts["artifacts"][0], "digest": f"sha256:{'0' * 64}"}],
        }
        with mock.patch.object(self.module, "github_json", side_effect=[self.run, artifacts]):
            with self.assertRaisesRegex(self.module.ValidationError, "digest mismatch"):
                self.validate_echidna()

    def test_accepts_medusa_ref_at_expected_commit(self):
        expected = "3857153837ab90ed73adc484414b4b43703a54fb"
        with mock.patch.object(self.module, "resolve_git_ref", return_value=expected):
            result = self.module.validate_medusa_source(
                repo_url="https://github.com/crytic/medusa",
                git_ref="v1.4.1",
                expected_commit=expected,
            )

        self.assertEqual(expected, result["commit"])
        self.assertEqual("v1.4.1", result["requested_ref"])

    def test_rejects_medusa_ref_drift(self):
        expected = "3857153837ab90ed73adc484414b4b43703a54fb"
        with mock.patch.object(self.module, "resolve_git_ref", return_value="0" * 40):
            with self.assertRaisesRegex(self.module.ValidationError, "ref drift"):
                self.module.validate_medusa_source(
                    repo_url="https://github.com/crytic/medusa",
                    git_ref="master",
                    expected_commit=expected,
                )

    def test_accepts_only_official_go_linux_amd64_digest(self):
        metadata = [
            {
                "version": "go1.24.0",
                "files": [
                    {
                        "filename": "go1.24.0.linux-amd64.tar.gz",
                        "os": "linux",
                        "arch": "amd64",
                        "kind": "archive",
                        "sha256": self.digest,
                        "size": 78_382_844,
                    }
                ],
            }
        ]
        with mock.patch.object(self.module, "fetch_json", return_value=metadata):
            result = self.module.validate_go_toolchain(
                version="1.24.0",
                expected_sha256=self.digest,
            )
        self.assertEqual(self.digest, result["sha256"])
        self.assertEqual(78_382_844, result["size"])

        with mock.patch.object(self.module, "fetch_json", return_value=metadata):
            with self.assertRaisesRegex(self.module.ValidationError, "official metadata"):
                self.module.validate_go_toolchain(
                    version="1.24.0",
                    expected_sha256="0" * 64,
                )

    def test_rejects_option_like_medusa_ref_before_git(self):
        with self.assertRaisesRegex(self.module.ValidationError, "unsupported"):
            self.module.resolve_git_ref("https://github.com/crytic/medusa", "--help")


if __name__ == "__main__":
    unittest.main()
