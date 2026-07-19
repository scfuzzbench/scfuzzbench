import importlib.util
import sys
import unittest
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

    def validate_echidna(self):
        return self.module.validate_echidna_artifact(
            repo_url="https://github.com/crytic/echidna",
            run_id="29156015131",
            artifact_name="echidna-Linux",
            artifact_sha256=self.digest,
            expected_commit=self.commit,
        )

    def test_accepts_exact_successful_artifact_pin(self):
        with mock.patch.object(self.module, "github_json", side_effect=[self.run, self.artifacts]):
            result = self.validate_echidna()

        self.assertEqual(self.commit, result["commit"])
        self.assertEqual("8249501578", result["artifact_id"])
        self.assertEqual(f"sha256:{self.digest}", result["artifact_digest"])

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
