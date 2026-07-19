import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path


def load_validator():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "validate_known_bugs.py"
    spec = importlib.util.spec_from_file_location("validate_known_bugs", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load spec for {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class KnownBugCatalogValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_validator()
        repo_root = Path(__file__).resolve().parents[2]
        cls.catalog = json.loads(
            (repo_root / "benchmarks" / "known_bugs.json").read_text(encoding="utf-8")
        )
        cls.targets = json.loads(
            (repo_root / "benchmarks" / "targets.json").read_text(encoding="utf-8")
        )

    def test_repository_catalog_is_valid(self):
        self.assertEqual(
            [],
            self.module.validate_catalog(self.catalog, self.targets),
        )

    def test_catalog_must_cover_every_active_target(self):
        catalog = copy.deepcopy(self.catalog)
        removed = catalog["targets"].pop()

        errors = self.module.validate_catalog(catalog, self.targets)

        self.assertTrue(
            any(removed["target_id"] in error and "missing target" in error for error in errors)
        )

    def test_target_commit_must_match_authoritative_manifest(self):
        catalog = copy.deepcopy(self.catalog)
        catalog["targets"][0]["target_commit"] = "0" * 40

        errors = self.module.validate_catalog(catalog, self.targets)

        self.assertTrue(any("must match the target manifest commit" in error for error in errors))

    def test_duplicate_fuzzer_event_alias_is_rejected_across_entries(self):
        catalog = copy.deepcopy(self.catalog)
        duplicate = copy.deepcopy(catalog["targets"][0]["canaries"][0])
        duplicate["id"] = "duplicate-canary"
        catalog["targets"][0]["canaries"].append(duplicate)

        errors = self.module.validate_catalog(catalog, self.targets)

        self.assertTrue(any("duplicates the echidna/assert_canary alias" in error for error in errors))

    def test_aliases_require_normalized_events_and_supported_fuzzers(self):
        catalog = copy.deepcopy(self.catalog)
        alias = catalog["targets"][0]["canaries"][0]["aliases"][0]
        alias["event"] = "CryticTester.assert_canary(uint256)"
        alias["fuzzers"] = ["unknown-fuzzer"]

        errors = self.module.validate_catalog(catalog, self.targets)

        self.assertTrue(any("normalized Solidity function identifier" in error for error in errors))
        self.assertTrue(any("must be one of" in error for error in errors))

    def test_aliases_reject_parser_specific_assertion_wrappers(self):
        for event in (
            "invariant_assertion_failure_assert_canary",
            "assert_canary_ASSERTION_CANARY",
        ):
            with self.subTest(event=event):
                catalog = copy.deepcopy(self.catalog)
                catalog["targets"][0]["canaries"][0]["aliases"][0]["event"] = event

                errors = self.module.validate_catalog(catalog, self.targets)

                self.assertTrue(
                    any(
                        "normalized Solidity function identifier" in error
                        for error in errors
                    )
                )

    def test_each_entry_requires_commit_pinned_evidence(self):
        catalog = copy.deepcopy(self.catalog)
        catalog["targets"][0]["canaries"][0]["evidence"] = [
            {
                "url": "https://example.com/unpinned",
                "description": "Not tied to the target revision.",
            }
        ]

        errors = self.module.validate_catalog(catalog, self.targets)

        self.assertTrue(any("must include a URL pinned to target_commit" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
