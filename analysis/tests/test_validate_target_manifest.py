import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path


def load_validator():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "validate_target_manifest.py"
    spec = importlib.util.spec_from_file_location("validate_target_manifest", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load spec for {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TargetManifestValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_validator()
        manifest_path = Path(__file__).resolve().parents[2] / "benchmarks" / "targets.json"
        cls.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    def test_repository_manifest_is_valid(self):
        self.assertEqual([], self.module.validate_manifest(self.manifest))

    def test_mutable_commit_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["targets"][0]["commit"] = "main"

        errors = self.module.validate_manifest(manifest)

        self.assertTrue(any(".commit must be a lowercase 40-character Git SHA" in error for error in errors))

    def test_duplicate_ids_and_repositories_are_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["targets"][1]["id"] = manifest["targets"][0]["id"]
        manifest["targets"][1]["repo"] = "https://github.com/SCFUZZBENCH/AAVE-V4-SCFUZZBENCH"

        errors = self.module.validate_manifest(manifest)

        self.assertTrue(any(".id duplicates" in error for error in errors))
        self.assertTrue(any(".repo duplicates" in error for error in errors))

    def test_invalid_path_and_related_work_reference_are_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["targets"][0]["properties_path"] = "../Properties.sol"
        manifest["targets"][0]["related_work_refs"][0]["url"] = "http://example.com/source"

        errors = self.module.validate_manifest(manifest)

        self.assertTrue(any(".properties_path must be a repo-relative path" in error for error in errors))
        self.assertTrue(any(".url must be an HTTPS URL" in error for error in errors))

    def test_properties_path_matches_benchmark_request_constraints(self):
        invalid_paths = {
            "/test/Properties.sol": "repo-relative",
            "test/Properties..sol": "without '..'",
            "test//Properties.sol": "normalized",
            "test\\Properties.sol": "forward slashes",
            "test/My Properties.sol": "contain only",
            "test/Properties!.sol": "contain only",
            f"test/{'a' * 196}.sol": "at most 200 characters",
            "test/Properties": "point to a .sol file",
        }

        for properties_path, expected_error in invalid_paths.items():
            with self.subTest(properties_path=properties_path):
                manifest = copy.deepcopy(self.manifest)
                manifest["targets"][0]["properties_path"] = properties_path

                errors = self.module.validate_manifest(manifest)

                self.assertTrue(
                    any(
                        ".properties_path" in error and expected_error in error
                        for error in errors
                    ),
                    errors,
                )

    def test_missing_and_unknown_fields_are_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        del manifest["targets"][0]["rationale"]
        manifest["targets"][0]["rationle"] = "typo"

        errors = self.module.validate_manifest(manifest)

        self.assertTrue(any("is missing fields: rationale" in error for error in errors))
        self.assertTrue(any("has unknown fields: rationle" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
