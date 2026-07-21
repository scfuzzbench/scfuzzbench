import tempfile
import unittest
from pathlib import Path

from analysis import analyze


class FoundryParserTests(unittest.TestCase):
    def write_log(self, lines):
        tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        try:
            tmp.write("\n".join(lines) + "\n")
            tmp.close()
            return Path(tmp.name)
        except Exception:
            tmp.close()
            raise

    def test_parses_failure_event_records(self):
        log_path = self.write_log(
            [
                '{"timestamp":100,"event":"pulse","metrics":{"cumulative_edges_seen":1}}',
                '{"timestamp":101,"event":"failure","target":"CryticToFoundry:invariant_a","type":"invariant"}',
                '{"timestamp":102,"event":"failure","target":"CryticToFoundry:invariant_a","type":"invariant"}',
                '{"timestamp":103,"event":"failure","target":"CryticToFoundry:invariant_b","type":"assertion"}',
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual([event.event for event in events], ["invariant_a", "invariant_b"])
        self.assertEqual(
            [event.source for event in events],
            ["foundry-failure-event", "foundry-failure-event"],
        )
        self.assertAlmostEqual(events[0].elapsed_seconds, 1.0)
        self.assertAlmostEqual(events[1].elapsed_seconds, 3.0)

    def test_handler_assertion_events_do_not_name_bugs_by_address(self):
        # foundry-rs/foundry#15689 emits mid-run handler-assertion failure
        # events whose `target` is the harness contract ADDRESS. Selectors other
        # than the standardized assertion canary must stay unnamed; naming by
        # address would collapse every handler bug into one identity.
        log_path = self.write_log(
            [
                '{"timestamp":100,"event":"pulse","metrics":{"cumulative_edges_seen":1}}',
                '{"timestamp":105,"event":"failure","failure_type":"handler_assertion",'
                '"target":"0x7fa9385be102ac3eac297483dd6233d62b3e1496","selector":"0xa9cc4718",'
                '"reason":"assertion failed"}',
                '{"timestamp":106,"event":"failure","failure_type":"handler_assertion",'
                '"target":"0x7fa9385be102ac3eac297483dd6233d62b3e1496","selector":"0xdeadbeef",'
                '"reason":"assertion failed"}',
                '{"timestamp":110,"event":"failure","invariant":"invariant_canary",'
                '"target":"test/recon/CryticToFoundry.sol:CryticToFoundry","reason":"x"}',
                "[FAIL: assertion failed] test/recon/CryticToFoundry.sol:CryticToFoundry::assert_canary_ASSERTION_CANARY() (runs: 1)",
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-test")
        names = [event.event for event in events]
        self.assertIn("invariant_canary", names)
        self.assertIn("assert_canary_ASSERTION_CANARY", names)
        self.assertNotIn("0x7fa9385be102ac3eac297483dd6233d62b3e1496", names)
        self.assertEqual(len(names), len(set(names)))

    def test_names_standardized_canary_selector_in_preliminary_log(self):
        # Exact handler-assertion and pulse shape captured from both Liquity CP1
        # replicas. A preliminary snapshot has no terminal assertion summary.
        log_path = self.write_log(
            [
                '{"timestamp":1784625666,"event":"failure","invariant":"invariant_GV01",'
                '"target":"test/recon/CryticToFoundry.sol:CryticToFoundry","reason":"x"}',
                '{"timestamp":1784625666,"event":"failure","invariant":"invariant_canary",'
                '"target":"test/recon/CryticToFoundry.sol:CryticToFoundry","reason":"x"}',
                '{"timestamp":1784625666,"event":"failure","invariant":"invariant_BI03",'
                '"target":"test/recon/CryticToFoundry.sol:CryticToFoundry","reason":"x"}',
                '{"timestamp":1784625666,"event":"failure","invariant":"invariant_BI04",'
                '"target":"test/recon/CryticToFoundry.sol:CryticToFoundry","reason":"x"}',
                '{"timestamp":1784625666,"event":"failure","failure_type":"handler_assertion",'
                '"target":"0x7fa9385be102ac3eac297483dd6233d62b3e1496",'
                '"selector":"0xf24a44c9","reason":"assertion failed"}',
                '{"timestamp":1784625671,"event":"pulse",'
                '"contract":"test/recon/CryticToFoundry.sol:CryticToFoundry",'
                '"metrics":{"broken_invariants":4,"broken_assertions":1}}',
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-test")

        self.assertEqual(
            [event.event for event in events],
            [
                "invariant_GV01",
                "invariant_canary",
                "invariant_BI03",
                "invariant_BI04",
                "assert_canary_ASSERTION_CANARY",
            ],
        )
        self.assertEqual(events[-1].source, "foundry-handler-selector")
        self.assertAlmostEqual(events[-1].elapsed_seconds, 0.0)
        self.assertNotIn("foundry_handler_bug_1", [event.event for event in events])

    def test_unknown_handler_selector_keeps_synthetic_fallback(self):
        partial_lines = [
            '{"timestamp":100,"event":"failure","failure_type":"handler_assertion",'
            '"target":"0x7fa9385be102ac3eac297483dd6233d62b3e1496",'
            '"selector":"0xdeadbeef","reason":"assertion failed"}',
            '{"timestamp":105,"event":"pulse","contract":"CryticToFoundry",'
            '"metrics":{"broken_invariants":0,"broken_assertions":1}}',
        ]
        log_path = self.write_log(partial_lines)

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-test")

        self.assertEqual([event.event for event in events], ["foundry_handler_bug_1"])
        self.assertEqual(events[0].source, "foundry-broken-handler-metric")
        self.assertAlmostEqual(events[0].elapsed_seconds, 5.0)

        terminal_log_path = self.write_log(
            partial_lines
            + [
                "[FAIL: panic: assertion failed (0x01)] "
                "test/recon/CryticToFoundry.sol:CryticToFoundry::unknown_handler"
            ]
        )
        terminal_events = analyze.parse_foundry_log(
            terminal_log_path, "run-1", "i-1", "foundry-git-test"
        )

        self.assertEqual([event.event for event in terminal_events], ["unknown_handler"])
        self.assertEqual(terminal_events[0].source, "foundry-handler-summary")
        self.assertAlmostEqual(terminal_events[0].elapsed_seconds, 5.0)

    def test_terminal_summary_dedupes_standardized_canary_selector(self):
        log_path = self.write_log(
            [
                '{"timestamp":100,"event":"failure","failure_type":"handler_assertion",'
                '"target":"0x7fa9385be102ac3eac297483dd6233d62b3e1496",'
                '"selector":"0xf24a44c9","reason":"assertion failed"}',
                '{"timestamp":105,"event":"pulse","contract":"CryticToFoundry",'
                '"metrics":{"broken_invariants":0,"broken_assertions":1}}',
                "[FAIL: assertion failed] "
                "test/recon/CryticToFoundry.sol:CryticToFoundry::"
                "assert_canary_ASSERTION_CANARY",
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-test")

        self.assertEqual(
            [event.event for event in events],
            ["assert_canary_ASSERTION_CANARY"],
        )
        self.assertEqual(events[0].source, "foundry-handler-selector")
        self.assertAlmostEqual(events[0].elapsed_seconds, 0.0)

    def test_parses_legacy_foundry_failure_records(self):
        log_path = self.write_log(
            [
                '{"type":"invariant_failure","timestamp":100,"invariant":"legacy_invariant","failed_total":1}',
                '{"timestamp":100,"invariant":"legacy_invariant","failed":1,"metrics":{"cumulative_edges_seen":1}}',
                "[FAIL: legacy] legacy_invariant()",
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, "legacy_invariant")
        self.assertEqual(events[0].source, "foundry-invariant-failure")
        self.assertAlmostEqual(events[0].elapsed_seconds, 0.0)

    def test_dedupes_across_foundry_failure_formats(self):
        log_path = self.write_log(
            [
                '{"timestamp":100,"event":"failure","target":"CryticToFoundry:invariant_a","type":"invariant"}',
                '{"type":"invariant_failure","timestamp":101,"invariant":"invariant_a","failed_total":1}',
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, "invariant_a")
        self.assertEqual(events[0].source, "foundry-failure-event")

    def test_parses_fail_on_assert_failure_events(self):
        log_path = self.write_log(
            [
                '{"timestamp":200,"event":"failure","target":"CryticToFoundry:assert_canary_ASSERTION_CANARY","type":"assertion"}',
                '{"timestamp":201,"event":"pulse","metrics":{"cumulative_edges_seen":4}}',
                '{"timestamp":202,"event":"failure","target":"CryticToFoundry:assert_canary_ASSERTION_CANARY","type":"assertion"}',
                '{"timestamp":203,"event":"failure","target":"CryticToFoundry:invariant_canary","type":"invariant"}',
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual(
            [event.event for event in events],
            ["assert_canary_ASSERTION_CANARY", "invariant_canary"],
        )
        self.assertEqual(
            [event.source for event in events],
            ["foundry-failure-event", "foundry-failure-event"],
        )
        self.assertAlmostEqual(events[0].elapsed_seconds, 0.0)
        self.assertAlmostEqual(events[1].elapsed_seconds, 3.0)

    def test_promotes_broken_handler_metrics_to_bug_events(self):
        log_path = self.write_log(
            [
                '{"timestamp":100,"event":"pulse","metrics":{"unique_failures":0,"broken_handlers":0}}',
                '{"timestamp":101,"event":"failure","invariant":"invariant_a","target":"CryticToFoundry","reason":"broken"}',
                '{"timestamp":102,"event":"pulse","metrics":{"unique_failures":1,"broken_handlers":2}}',
                '{"timestamp":103,"event":"failure","invariant":"invariant_b","target":"CryticToFoundry","reason":"broken"}',
                '{"timestamp":104,"event":"pulse","metrics":{"unique_failures":2,"broken_handlers":3}}',
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual(
            [event.event for event in events],
            [
                "invariant_a",
                "foundry_handler_bug_1",
                "foundry_handler_bug_2",
                "invariant_b",
                "foundry_handler_bug_3",
            ],
        )
        self.assertEqual(
            [event.source for event in events],
            [
                "foundry-failure-event",
                "foundry-broken-handler-metric",
                "foundry-broken-handler-metric",
                "foundry-failure-event",
                "foundry-broken-handler-metric",
            ],
        )
        self.assertAlmostEqual(events[1].elapsed_seconds, 2.0)
        self.assertAlmostEqual(events[4].elapsed_seconds, 4.0)

    def test_promotes_broken_handler_metrics_from_oss333_pulse_events(self):
        log_path = self.write_log(
            [
                '{"timestamp":100,"event":"pulse","contract":"CryticToFoundry","metrics":{"broken_invariants":0,"broken_assertions":0},"tps":10,"gps":100,"worker":{"id":0,"count":1}}',
                '{"timestamp":101,"event":"failure","invariant":"invariant_a","target":"CryticToFoundry","reason":"broken"}',
                '{"timestamp":102,"event":"pulse","contract":"CryticToFoundry","metrics":{"broken_invariants":1,"broken_assertions":2},"tps":12,"gps":120,"worker":{"id":0,"count":1}}',
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual(
            [event.event for event in events],
            [
                "invariant_a",
                "foundry_handler_bug_1",
                "foundry_handler_bug_2",
            ],
        )
        self.assertEqual(
            [event.source for event in events],
            [
                "foundry-failure-event",
                "foundry-broken-handler-metric",
                "foundry-broken-handler-metric",
            ],
        )
        self.assertAlmostEqual(events[1].elapsed_seconds, 2.0)
        self.assertAlmostEqual(events[2].elapsed_seconds, 2.0)

    def test_names_synthetic_handler_bugs_from_end_of_run_summary(self):
        log_path = self.write_log(
            [
                '{"timestamp":100,"event":"pulse","contract":"CryticToFoundry","metrics":{"broken_invariants":0,"broken_assertions":0}}',
                '{"timestamp":130,"event":"pulse","contract":"CryticToFoundry","metrics":{"broken_invariants":0,"broken_assertions":1}}',
                '{"timestamp":200,"event":"pulse","contract":"CryticToFoundry","metrics":{"broken_invariants":0,"broken_assertions":2}}',
                "Assertion Tests: 2 assertion bug(s) found",
                "[FAIL: assertion failed] test/recon/CryticToFoundry.sol:CryticToFoundry::assert_canary_ASSERTION_CANARY",
                "[FAIL: panic: assertion failed (0x01)] test/recon/CryticToFoundry.sol:CryticToFoundry::doomsday_probe_STATELESS",
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual(
            [event.event for event in events],
            ["assert_canary_ASSERTION_CANARY", "doomsday_probe_STATELESS"],
        )
        self.assertEqual(
            [event.source for event in events],
            ["foundry-handler-summary", "foundry-handler-summary"],
        )
        # Discovery times from the pulse deltas are preserved through the rename.
        self.assertAlmostEqual(events[0].elapsed_seconds, 30.0)
        self.assertAlmostEqual(events[1].elapsed_seconds, 100.0)

    def test_handler_summary_without_pulse_synthetics_emits_named_event(self):
        log_path = self.write_log(
            [
                '{"timestamp":100,"event":"pulse","contract":"CryticToFoundry","metrics":{"cumulative_edges_seen":1}}',
                '{"timestamp":160,"event":"pulse","contract":"CryticToFoundry","metrics":{"cumulative_edges_seen":2}}',
                "[FAIL: assertion failed] test/recon/CryticToFoundry.sol:CryticToFoundry::assert_canary_ASSERTION_CANARY",
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, "assert_canary_ASSERTION_CANARY")
        self.assertEqual(events[0].source, "foundry-handler-summary")
        # Anchored to the last JSON pulse timestamp rather than 0.0.
        self.assertAlmostEqual(events[0].elapsed_seconds, 60.0)

    def test_handler_summary_dedupes_repeated_entries(self):
        log_path = self.write_log(
            [
                '{"timestamp":100,"event":"pulse","contract":"CryticToFoundry","metrics":{"broken_invariants":0,"broken_assertions":1}}',
                "[FAIL: assertion failed] test/recon/CryticToFoundry.sol:CryticToFoundry::assert_canary_ASSERTION_CANARY",
                "[FAIL: assertion failed] test/recon/CryticToFoundry.sol:CryticToFoundry::assert_canary_ASSERTION_CANARY",
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, "assert_canary_ASSERTION_CANARY")

    def test_parses_foundry_text_failure_summary_lines(self):
        log_path = self.write_log(
            [
                "fuzz: elapsed: 6s, calls: 61658 (20486/sec), seq/s: 211, branches hit: 537, corpus: 137, failures: 15/762, gas/s: 4500638777",
                "Failing tests:",
                "[FAIL: invariant broken] invariant_poolBalance() (runs: 256, calls: 3840, reverts: 0)",
                "[FAIL: assertion failed] assert_healthFactor_ASSERTION_CANARY() (gas: 12345)",
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-master")
        self.assertEqual(
            [event.event for event in events],
            ["invariant_poolBalance", "assert_healthFactor_ASSERTION_CANARY"],
        )
        self.assertEqual(
            [event.source for event in events],
            ["foundry-text-failure", "foundry-text-failure"],
        )
        self.assertAlmostEqual(events[0].elapsed_seconds, 6.0)
        self.assertAlmostEqual(events[1].elapsed_seconds, 6.0)

    def test_parses_prefixed_foundry_text_failure_summary_lines(self):
        log_path = self.write_log(
            [
                "fuzz: elapsed: 6s, calls: 61658 (20486/sec), failures: 15/762",
                "│ [FAIL: invariant broken] invariant_poolBalance() (runs: 256, calls: 3840, reverts: 0)",
                "2026-06-09T00:00:00Z [FAIL: assertion failed] assert_healthFactor_ASSERTION_CANARY() (gas: 12345)",
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-master")
        self.assertEqual(
            [event.event for event in events],
            ["invariant_poolBalance", "assert_healthFactor_ASSERTION_CANARY"],
        )
        self.assertEqual(
            [event.source for event in events],
            ["foundry-text-failure", "foundry-text-failure"],
        )
        self.assertAlmostEqual(events[0].elapsed_seconds, 6.0)
        self.assertAlmostEqual(events[1].elapsed_seconds, 6.0)

    def test_parses_bare_invariant_summary_lines(self):
        # Pinned-foundry end-of-run summaries list invariants as bare names with
        # no parens: "[FAIL: ] invariant_canary" (observed on run 1783612049's
        # superform foundry leg, where these were silently dropped).
        log_path = self.write_log(
            [
                "Ran 1 test for test/recon/CryticToFoundry.sol:CryticToFoundry",
                "[FAIL: ] invariant_canary",
                "[FAIL: panic: division or modulo by zero (0x12)] invariant_maxRedeemMaxWithdrawSymmetry",
                "[FAIL: assertion failed] test/recon/CryticToFoundry.sol:CryticToFoundry::assert_canary_ASSERTION_CANARY",
                "[PASS] invariant_noop",
                " CryticToFoundry invariants (runs: 45847, calls: 4584700, reverts: 2115499)",
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-907ba08")
        self.assertEqual(
            sorted(event.event for event in events),
            [
                "assert_canary_ASSERTION_CANARY",
                "invariant_canary",
                "invariant_maxRedeemMaxWithdrawSymmetry",
            ],
        )

    def test_parses_foundry_text_test_result_lines_without_elapsed(self):
        log_path = self.write_log(
            [
                "[FAIL. Reason: invariant broken] invariant_debtAccounting(): FAIL",
                "Suite result: FAILED. 0 passed; 1 failed; 0 skipped; finished in 5.00s",
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-new")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, "invariant_debtAccounting")
        self.assertEqual(events[0].source, "foundry-text-failure")
        self.assertAlmostEqual(events[0].elapsed_seconds, 0.0)

    def test_dedupes_foundry_text_failures_against_json_failures(self):
        log_path = self.write_log(
            [
                '{"timestamp":100,"event":"failure","target":"CryticToFoundry:invariant_a","type":"invariant"}',
                "[FAIL: invariant broken] invariant_a() (runs: 256, calls: 3840, reverts: 0)",
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, "invariant_a")
        self.assertEqual(events[0].source, "foundry-failure-event")

    def test_parses_throughput_from_json_cumulative_metrics(self):
        log_path = self.write_log(
            [
                '{"type":"invariant_metrics","timestamp":100,"invariant":"invariant_a","metrics":{"cumulative_tx_count":20,"cumulative_gas_used":2000}}',
                '{"type":"invariant_metrics","timestamp":110,"invariant":"invariant_a","metrics":{"cumulative_tx_count":140,"cumulative_gas_used":15400}}',
            ]
        )

        samples = analyze.parse_throughput_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].source, "json-cumulative")
        self.assertAlmostEqual(samples[0].elapsed_seconds, 10.0)
        self.assertAlmostEqual(samples[0].tx_per_second, 14.0)
        self.assertAlmostEqual(samples[0].gas_per_second, 1540.0)

    def test_parses_throughput_from_json_rate_metrics(self):
        log_path = self.write_log(
            [
                '{"type":"invariant_metrics","timestamp":200,"invariant":"invariant_a","metrics":{"tx_per_second":11.5,"gas_per_second":900}}',
            ]
        )

        samples = analyze.parse_throughput_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].source, "json-rate")
        self.assertAlmostEqual(samples[0].tx_per_second, 11.5)
        self.assertAlmostEqual(samples[0].gas_per_second, 900.0)


if __name__ == "__main__":
    unittest.main()
