import csv
import gzip
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from analysis import selector_analytics


FIXTURES = Path(__file__).parent / "fixtures" / "selectors"


def _instance(summary, fuzzer):
    return next(item for item in summary["instances"] if item["fuzzer"] == fuzzer)


class SelectorAnalyticsTests(unittest.TestCase):
    def test_parses_all_four_real_corpus_shapes_and_deduplicates_sequences(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "corpus"
            shutil.copytree(FIXTURES / "corpus", corpus)
            echidna_coverage = (
                corpus
                / "i-aaaa-echidna-v2.3.1"
                / "echidna"
                / "coverage"
            )
            shutil.copy2(
                echidna_coverage / "sequence.txt",
                echidna_coverage / "duplicate-with-new-name.txt",
            )

            foundry_json = next(
                (corpus / "i-dddd-foundry-git-deadbee").rglob("*.json")
            )
            foundry_gzip = foundry_json.with_suffix(".json.gz")
            with gzip.open(foundry_gzip, "wt", encoding="utf-8") as handle:
                handle.write(foundry_json.read_text(encoding="utf-8"))
            foundry_json.unlink()

            rows, summary = selector_analytics.analyze_selector_artifacts(
                corpus_dir=corpus,
                logs_dir=None,
                run_id="12345678",
            )

            self.assertEqual(
                {"echidna", "foundry", "medusa", "recon-fuzzer"},
                {item["fuzzer"] for item in summary["fuzzers"]},
            )
            for fuzzer in ("echidna", "foundry", "medusa", "recon-fuzzer"):
                item = _instance(summary, fuzzer)
                self.assertEqual("available", item["status"])
                self.assertGreater(item["saved_corpus_calls"], 0)
                self.assertGreaterEqual(item["unique_selectors"], 1)

            echidna = _instance(summary, "echidna")
            self.assertEqual(1, echidna["parsed_sequences"])
            self.assertEqual(1, echidna["duplicate_sequences"])
            transfer_rows = [
                row
                for row in rows
                if row["selector"] == "0xa9059cbb"
                and row["fuzzer"] == "echidna"
            ]
            self.assertEqual(1, len(transfer_rows))
            self.assertEqual(2, transfer_rows[0]["saved_corpus_calls"])
            self.assertEqual(
                "transfer(address,uint256)",
                transfer_rows[0]["function_signatures"],
            )

            expected = summary["expected_set"]
            self.assertEqual("peer-consensus-heuristic", expected["kind"])
            self.assertIn("not benchmark ground truth", expected["source"])
            self.assertEqual(
                ["0xa9059cbb"],
                [item["selector"] for item in expected["selectors"]],
            )
            self.assertEqual([], summary["health_warnings"])

    def test_peer_heuristic_flags_missing_selector_but_never_uses_empty_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "corpus"
            shutil.copytree(FIXTURES / "corpus", corpus)
            medusa_file = next(
                (corpus / "i-cccc-medusa-v1.4.1").rglob("*.json")
            )
            payload = json.loads(medusa_file.read_text(encoding="utf-8"))
            payload[0]["call"]["data"] = "0x095ea7b3"
            payload[0]["call"]["dataAbiValues"]["methodSignature"] = (
                "approve(address,uint256)"
            )
            medusa_file.write_text(json.dumps(payload), encoding="utf-8")

            empty_foundry = (
                corpus
                / "i-eeee-foundry-git-empty"
                / "foundry"
            )
            empty_foundry.mkdir(parents=True)

            _, summary = selector_analytics.analyze_selector_artifacts(
                corpus_dir=corpus,
                logs_dir=None,
            )

            expected = {
                item["selector"] for item in summary["expected_set"]["selectors"]
            }
            self.assertEqual({"0xa9059cbb"}, expected)
            medusa = _instance(summary, "medusa")
            self.assertEqual(["0xa9059cbb"], medusa["missing_expected_selectors"])
            self.assertTrue(
                any(
                    "i-cccc-medusa-v1.4.1: peer-heuristic gap"
                    in finding["message"]
                    for finding in summary["health_findings"]
                )
            )
            empty = next(
                item
                for item in summary["instances"]
                if item["instance_label"] == "i-eeee-foundry-git-empty"
            )
            self.assertEqual("unavailable", empty["status"])
            self.assertFalse(
                any(
                    "i-eeee-foundry-git-empty" in warning
                    for warning in summary["health_warnings"]
                )
            )

    def test_distinguishes_observed_zero_malformed_and_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "corpus"
            logs = root / "logs"

            empty = (
                corpus
                / "i-aaaa-echidna-v2.3.1"
                / "echidna"
                / "coverage"
            )
            empty.mkdir(parents=True)
            (empty / "empty.txt").write_text("[]\n", encoding="utf-8")

            malformed = (
                corpus
                / "i-bbbb-medusa-v1.4.1"
                / "medusa"
                / "call_sequences"
            )
            malformed.mkdir(parents=True)
            (malformed / "broken.json").write_text("{not-json", encoding="utf-8")

            foundry_logs = logs / "i-cccc-foundry-git-deadbee"
            foundry_logs.mkdir(parents=True)
            (foundry_logs / "foundry.log").write_text(
                '{"event":"failure","failure_type":"handler_assertion",'
                '"selector":"0xdeadbeef"}\n',
                encoding="utf-8",
            )

            rows, summary = selector_analytics.analyze_selector_artifacts(
                corpus_dir=corpus,
                logs_dir=logs,
            )

            self.assertEqual([], rows)
            self.assertEqual(
                "observed_zero", _instance(summary, "echidna")["status"]
            )
            self.assertEqual(
                "malformed", _instance(summary, "medusa")["status"]
            )
            self.assertEqual(
                "unavailable", _instance(summary, "foundry")["status"]
            )
            warnings = "\n".join(summary["health_warnings"])
            self.assertIn("i-aaaa-echidna-v2.3.1: selector telemetry observed zero", warnings)
            self.assertIn("i-bbbb-medusa-v1.4.1: 1 malformed", warnings)
            self.assertNotIn("i-cccc-foundry-git-deadbee", warnings)
            self.assertNotIn("0xdeadbeef", json.dumps(summary))
            self.assertTrue(
                any(
                    "failure-event selectors are intentionally not used"
                    in limitation
                    for limitation in summary["limitations"]
                )
            )

    def test_correlated_echidna_and_recon_do_not_define_peer_consensus(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp) / "corpus"
            shutil.copytree(FIXTURES / "corpus", corpus)
            shutil.rmtree(corpus / "i-cccc-medusa-v1.4.1")
            shutil.rmtree(corpus / "i-dddd-foundry-git-deadbee")

            _, summary = selector_analytics.analyze_selector_artifacts(
                corpus_dir=corpus,
                logs_dir=None,
            )

            self.assertEqual([], summary["expected_set"]["selectors"])
            self.assertIn(
                "Echidna and Recon count as one related typed-corpus family",
                summary["expected_set"]["source"],
            )

    def test_recon_compound_types_match_v046_serialization(self):
        self.assertEqual(
            "bytes32",
            selector_analytics._recon_abi_type(
                {"FixedBytes": ["0x" + "00" * 32, 32]}
            ),
        )
        self.assertEqual(
            "bytes",
            selector_analytics._recon_abi_type({"Bytes": [8, 31, 255]}),
        )
        self.assertEqual(
            "uint16[2]",
            selector_analytics._recon_abi_type(
                {"FixedArray": [{"Uint": ["0x1", 16]}, {"Uint": ["0x2", 16]}]}
            ),
        )
        self.assertEqual(
            "(address,uint8[])",
            selector_analytics._recon_abi_type(
                {
                    "Tuple": [
                        {"Address": "0x" + "00" * 20},
                        {"Array": [{"Uint": ["0x1", 8]}]},
                    ]
                }
            ),
        )

    def test_echidna_compound_types_preserve_tuple_and_array_shape(self):
        uint16_type = {"tag": "AbiUIntType", "contents": 16}
        uint16_value = {"tag": "AbiUInt", "contents": [16, "1"]}
        self.assertEqual(
            "uint16[]",
            selector_analytics._echidna_abi_type(
                {
                    "tag": "AbiArrayDynamic",
                    "contents": [uint16_type, [uint16_value]],
                }
            ),
        )
        self.assertEqual(
            "uint16[2]",
            selector_analytics._echidna_abi_type(
                {
                    "tag": "AbiArray",
                    "contents": [2, uint16_type, [uint16_value, uint16_value]],
                }
            ),
        )
        self.assertEqual(
            "(address,uint16[])",
            selector_analytics._echidna_abi_type(
                {
                    "tag": "AbiTuple",
                    "contents": [
                        {
                            "tag": "AbiAddress",
                            "contents": "0x" + "00" * 20,
                        },
                        {
                            "tag": "AbiArrayDynamic",
                            "contents": [uint16_type, [uint16_value]],
                        },
                    ],
                }
            ),
        )

    def test_medusa_mismatch_keeps_raw_selector_without_false_abi_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = (
                Path(tmp)
                / "i-aaaa-medusa-v1.4.1"
                / "medusa"
                / "call_sequences"
            )
            corpus.mkdir(parents=True)
            (corpus / "sequence.json").write_text(
                json.dumps(
                    [
                        {
                            "call": {
                                "data": "0xa9059cbb",
                                "dataAbiValues": {
                                    "methodSignature": "approve(address,uint256)"
                                },
                            }
                        }
                    ]
                ),
                encoding="utf-8",
            )

            rows, summary = selector_analytics.analyze_selector_artifacts(
                corpus_dir=Path(tmp),
                logs_dir=None,
            )

            self.assertEqual("0xa9059cbb", rows[0]["selector"])
            self.assertEqual("", rows[0]["function_signatures"])
            self.assertEqual("", rows[0]["function_names"])
            instance = _instance(summary, "medusa")
            self.assertIn("disagreed with raw calldata", "\n".join(instance["warnings"]))

    def test_explicit_catalog_rejects_selector_signature_disagreement(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = Path(tmp) / "expected.json"
            expected.write_text(
                json.dumps(
                    [
                        {
                            "selector": "0xa9059cbb",
                            "signature": "approve(address,uint256)",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "disagrees"):
                selector_analytics.analyze_selector_artifacts(
                    corpus_dir=None,
                    logs_dir=None,
                    expected_selectors_json=expected,
                )

    def test_unavailable_instances_never_report_missing_selectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            foundry_logs = root / "logs" / "i-aaaa-foundry-git-deadbee"
            foundry_logs.mkdir(parents=True)
            expected = root / "expected.json"
            expected.write_text('["0xa9059cbb"]\n', encoding="utf-8")

            _, summary = selector_analytics.analyze_selector_artifacts(
                corpus_dir=root / "missing",
                logs_dir=root / "logs",
                expected_selectors_json=expected,
            )

            instance = _instance(summary, "foundry")
            self.assertEqual("unavailable", instance["status"])
            self.assertEqual([], instance["missing_expected_selectors"])

    def test_artifact_count_limit_is_bounded_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            coverage = (
                Path(tmp)
                / "i-aaaa-echidna-v2.3.1"
                / "echidna"
                / "coverage"
            )
            coverage.mkdir(parents=True)
            for name in ("a.txt", "b.txt"):
                (coverage / name).write_text("[]\n", encoding="utf-8")

            with mock.patch.object(selector_analytics, "MAX_ARTIFACT_FILES", 1):
                _, summary = selector_analytics.analyze_selector_artifacts(
                    corpus_dir=Path(tmp),
                    logs_dir=None,
                )

            instance = _instance(summary, "echidna")
            self.assertEqual("partial", instance["status"])
            self.assertEqual(1, instance["limit_violations"])
            self.assertTrue(
                any(
                    finding["kind"] == "resource_limit"
                    for finding in summary["health_findings"]
                )
            )

    def test_symlinked_artifacts_are_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coverage = (
                root
                / "i-aaaa-echidna-v2.3.1"
                / "echidna"
                / "coverage"
            )
            coverage.mkdir(parents=True)
            outside = root / "outside.txt"
            outside.write_text(
                json.dumps(
                    [
                        {
                            "call": {
                                "tag": "SolCalldata",
                                "contents": "0xa9059cbb",
                            }
                        }
                    ]
                ),
                encoding="utf-8",
            )
            (coverage / "linked.txt").symlink_to(outside)

            rows, summary = selector_analytics.analyze_selector_artifacts(
                corpus_dir=root,
                logs_dir=None,
            )

            self.assertEqual([], rows)
            self.assertEqual("observed_zero", _instance(summary, "echidna")["status"])

    def test_foundry_candidate_match_is_relative_to_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus" / "unzipped"
            foundry = root / "i-aaaa-foundry-git-deadbee" / "foundry"
            foundry.mkdir(parents=True)
            (foundry / "metadata.json").write_text(
                '[{"calldata":"0xa9059cbb"}]\n',
                encoding="utf-8",
            )

            rows, summary = selector_analytics.analyze_selector_artifacts(
                corpus_dir=root,
                logs_dir=None,
            )

            self.assertEqual([], rows)
            self.assertEqual("unavailable", _instance(summary, "foundry")["status"])

    def test_explicit_expected_catalog_and_outputs_are_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "corpus"
            shutil.copytree(FIXTURES / "corpus", corpus)
            expected_path = root / "expected.json"
            expected_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "selectors": [
                            {"signature": "transfer( address payable , uint )"}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            rows, summary = selector_analytics.analyze_selector_artifacts(
                corpus_dir=corpus,
                logs_dir=None,
                expected_selectors_json=expected_path,
            )
            self.assertEqual("explicit", summary["expected_set"]["kind"])
            self.assertEqual(
                "0xa9059cbb",
                summary["expected_set"]["selectors"][0]["selector"],
            )

            csv_a, json_a = root / "a.csv", root / "a.json"
            csv_b, json_b = root / "b.csv", root / "b.json"
            selector_analytics.write_selector_outputs(rows, summary, csv_a, json_a)
            selector_analytics.write_selector_outputs(rows, summary, csv_b, json_b)
            self.assertEqual(csv_a.read_bytes(), csv_b.read_bytes())
            self.assertEqual(json_a.read_bytes(), json_b.read_bytes())
            with csv_a.open(newline="", encoding="utf-8") as handle:
                parsed_rows = list(csv.DictReader(handle))
            self.assertTrue(parsed_rows)
            self.assertEqual(
                list(selector_analytics.SELECTOR_DISTRIBUTION_FIELDS),
                list(parsed_rows[0]),
            )

    def test_signature_and_selector_normalization(self):
        self.assertEqual(
            "transfer(address,uint256)",
            selector_analytics.normalize_signature(
                " transfer( address payable , uint ) "
            ),
        )
        self.assertEqual(
            "0xa9059cbb",
            selector_analytics.selector_from_signature(
                "transfer(address,uint256)"
            ),
        )
        self.assertEqual(
            "0xdeadbeef",
            selector_analytics.selector_from_calldata("0xDEADBEEF00"),
        )
        self.assertIsNone(selector_analytics.selector_from_calldata("0x1234"))


if __name__ == "__main__":
    unittest.main()
