"""Benchmark requests proposed from a watched fuzzer's pull request comments."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts import external_benchmark_requests as watcher  # noqa: E402

ECHIDNA = {
    "repo": "crytic/echidna",
    "base": "echidna",
    "build_mode": "ci",
    "artifact_name": "echidna-redistributable-x86_64-linux",
    "requesters": ["gustavo-grieco"],
    "requester_orgs": ["crytic"],
    "defaults": {
        "target_repo_url": "https://github.com/scfuzzbench/drips-fuzzing-scfuzzbench",
        "target_commit": "c50d160ead6bf82b1d1071e853dc74da0b23f595",
        "benchmark_type": "property",
        "instance_type": "c6a.4xlarge",
        "instances_per_fuzzer": 4,
        "timeout_hours": 1,
        "properties_path": "test/recon/Properties.sol",
        "git_token_ssm_parameter_name": "/scfuzzbench/recon/github_token",
        "echidna_ci_token_ssm_parameter_name": "/scfuzzbench/echidna-ci-token",
    },
}
BASE_BUILD = {
    "run_id": "33544949884",
    "artifact_name": "echidna-redistributable-x86_64-linux",
    "artifact_sha256": "d71654ccaf979fd9cf7a9d324827e11ba312dc3e94d8571a194ad68d8388701d",
    "commit": "cff8760d4b7ceba61448d795e1451fdd95f2376e",
}
PR_COMMIT = "55842ac2da34f40992cf48f211a0df1ede8e2fb9"
PR_BUILD = {
    "run_id": "33555211362",
    "artifact_name": "echidna-redistributable-x86_64-linux",
    "artifact_sha256": "c480d8599e643ee587cdb51fdffadb73a5291f46d97002398ab6ffadc55198b0",
    "commit": PR_COMMIT,
}

MEDUSA = {
    "repo": "crytic/medusa",
    "base": "medusa",
    "build_mode": "source",
    "requesters": [],
    "requester_orgs": ["crytic"],
    "defaults": {
        "target_repo_url": "https://github.com/scfuzzbench/drips-fuzzing-scfuzzbench",
        "target_commit": "c50d160ead6bf82b1d1071e853dc74da0b23f595",
        "medusa_go_version": "1.24.0",
        "medusa_go_sha256": "dea9ca38a0b852a74e81c26134671af7c0fbe65d81b0dc1c5bfe22cf7d4c8858",
    },
}
MEDUSA_BASELINE = {
    "git_ref": "master",
    "git_commit": "66bb59d05502a2a6362507df0bad6b5d0b3a6185",
}
MEDUSA_PR = {
    "git_ref": "refs/pull/512/head",
    "git_commit": "3857153837ab90ed73adc484414b4b43703a54fb",
}


class ShippedConfigTests(unittest.TestCase):
    def test_repository_configuration_is_valid(self):
        config = watcher.load_watch_config()

        self.assertEqual(config["command"], "/benchmark")
        self.assertIsNotNone(watcher.repository_config(config, "crytic/echidna"))
        self.assertIsNone(watcher.repository_config(config, "attacker/echidna"))

    def test_every_watched_repository_declares_a_usable_build_mode(self):
        config = watcher.load_watch_config()

        for entry in config["repositories"]:
            with self.subTest(repo=entry["repo"]):
                descriptor = watcher.build_mode_for(entry)
                self.assertIn(entry["base"], descriptor["bases"])

    def test_adding_a_repository_is_configuration_only(self):
        """A second fuzzer is an entry, not a code change."""
        config = watcher.load_watch_config()
        repos = {entry["repo"] for entry in config["repositories"]}

        self.assertIn("crytic/echidna", repos)
        self.assertIn("crytic/medusa", repos)
        self.assertEqual(
            {entry["build_mode"] for entry in config["repositories"]},
            {"ci", "source"},
        )

    def test_an_empty_allowlist_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "command": "/benchmark",
                        "repositories": [
                            dict(ECHIDNA, requesters=[], requester_orgs=[])
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(watcher.ConfigError, "empty allowlist"):
                watcher.load_watch_config(path)

    def test_malformed_entries_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch.json"
            for bad, expected in (
                ({"schema_version": 2}, "schema_version"),
                (
                    {"schema_version": 1, "command": "benchmark", "repositories": [ECHIDNA]},
                    "invalid watch command",
                ),
                (
                    {
                        "schema_version": 1,
                        "command": "/benchmark",
                        "repositories": [dict(ECHIDNA, repo="not-a-repo")],
                    },
                    "invalid watched repository",
                ),
                (
                    {
                        "schema_version": 1,
                        "command": "/benchmark",
                        "repositories": [dict(ECHIDNA, requesters=["not a login"])],
                    },
                    "invalid requester login",
                ),
            ):
                with self.subTest(expected=expected):
                    path.write_text(json.dumps(bad), encoding="utf-8")
                    with self.assertRaisesRegex(watcher.ConfigError, expected):
                        watcher.load_watch_config(path)


class AuthorizationTests(unittest.TestCase):
    def test_allowlisted_login_is_authorized(self):
        allowed, reason = watcher.authorize_requester("gustavo-grieco", ECHIDNA)

        self.assertTrue(allowed)
        self.assertIn("allowlisted", reason)

    def test_login_check_is_case_insensitive(self):
        allowed, _ = watcher.authorize_requester("Gustavo-Grieco", ECHIDNA)

        self.assertTrue(allowed)

    def test_unlisted_login_is_refused(self):
        allowed, reason = watcher.authorize_requester("drive-by", ECHIDNA)

        self.assertFalse(allowed)
        self.assertIn("not allowed", reason)

    def test_org_membership_must_be_verified_not_claimed(self):
        """The caller passes memberships it verified; a claim alone is not enough."""
        refused, _ = watcher.authorize_requester("someone", ECHIDNA)
        allowed, reason = watcher.authorize_requester(
            "someone", ECHIDNA, org_memberships=["crytic"]
        )

        self.assertFalse(refused)
        self.assertTrue(allowed)
        self.assertIn("crytic", reason)

    def test_membership_of_an_unrelated_org_is_refused(self):
        allowed, _ = watcher.authorize_requester(
            "someone", ECHIDNA, org_memberships=["some-other-org"]
        )

        self.assertFalse(allowed)

    def test_invalid_login_is_refused(self):
        for login in ("", "../../etc/passwd", "a" * 64, None):
            with self.subTest(login=login):
                allowed, _ = watcher.authorize_requester(login, ECHIDNA)
                self.assertFalse(allowed)


class CommandParsingTests(unittest.TestCase):
    def test_bare_command_uses_reviewed_defaults(self):
        self.assertEqual(watcher.parse_command("/benchmark", "/benchmark"), {})

    def test_command_is_found_on_its_own_line(self):
        body = "Looks like a regression.\n\n/benchmark timeout_hours=4\n\nThanks!"

        self.assertEqual(
            watcher.parse_command(body, "/benchmark"), {"timeout_hours": 4.0}
        )

    def test_unrelated_comments_are_ignored(self):
        for body in ("no command here", "we should /benchmark this later", ""):
            with self.subTest(body=body):
                self.assertIsNone(watcher.parse_command(body, "/benchmark"))

    def test_a_longer_command_does_not_trigger(self):
        self.assertIsNone(watcher.parse_command("/benchmarking", "/benchmark"))

    def test_overrides_are_typed(self):
        parsed = watcher.parse_command(
            "/benchmark instances_per_fuzzer=8 timeout_hours=2.5", "/benchmark"
        )

        self.assertEqual(parsed, {"instances_per_fuzzer": 8, "timeout_hours": 2.5})

    def test_unsupported_and_malformed_options_are_refused(self):
        for body, expected in (
            ("/benchmark fuzzers=echidna;rm -rf /", "unsupported option"),
            ("/benchmark timeout_hours=$(id)", "invalid value"),
            ("/benchmark instance_type=c6a.4xlarge extra", "expected key=value"),
            ("/benchmark timeout_hours=1 timeout_hours=2", "duplicate option"),
            ("/benchmark target_commit=../../etc/passwd", "invalid value"),
        ):
            with self.subTest(body=body):
                with self.assertRaisesRegex(watcher.CommandError, expected):
                    watcher.parse_command(body, "/benchmark")


class RequestBuildingTests(unittest.TestCase):
    def build(self, **kwargs):
        return watcher.build_request_payload(
            kwargs.pop("repo_config", ECHIDNA),
            variant_key=kwargs.pop("variant_key", "echidna-pr-1614"),
            pull_request_build=kwargs.pop("pull_request_build", PR_BUILD),
            baseline_build=kwargs.pop("baseline_build", BASE_BUILD),
            **kwargs,
        )

    def test_pull_request_build_becomes_a_variant_of_the_baseline(self):
        payload = self.build()

        self.assertEqual(payload["fuzzers"], ["echidna", "echidna-pr-1614"])
        self.assertEqual(payload["echidna_ci_commit"], BASE_BUILD["commit"])
        [variant] = payload["fuzzer_variants"]
        self.assertEqual(variant["key"], "echidna-pr-1614")
        self.assertEqual(variant["ci"]["commit"], PR_COMMIT)

    def test_payload_passes_the_shared_request_validators(self):
        from scripts.benchmark_run_state import validate_fuzzer_variants

        payload = self.build()
        variants = validate_fuzzer_variants(payload["fuzzer_variants"])

        self.assertEqual(variants[0]["ci"]["commit"], PR_COMMIT)
        self.assertTrue(
            all(key in payload["fuzzers"] for key in (v["key"] for v in variants))
        )

    def test_overrides_are_applied(self):
        payload = self.build(overrides={"timeout_hours": 4.0})

        self.assertEqual(payload["timeout_hours"], 4.0)

    def test_unsupported_override_is_refused(self):
        with self.assertRaisesRegex(watcher.CommandError, "unsupported option"):
            self.build(overrides={"echidna_ci_token_ssm_parameter_name": "/evil"})

    def test_a_build_identical_to_the_baseline_is_refused(self):
        with self.assertRaisesRegex(watcher.CommandError, "nothing to compare"):
            self.build(pull_request_build=dict(PR_BUILD, commit=BASE_BUILD["commit"]))

    def test_a_medusa_pull_request_becomes_a_source_variant(self):
        payload = self.build(
            repo_config=MEDUSA,
            variant_key="medusa-pr-512",
            pull_request_build=MEDUSA_PR,
            baseline_build=MEDUSA_BASELINE,
        )

        self.assertEqual(payload["fuzzers"], ["medusa", "medusa-pr-512"])
        self.assertEqual(payload["medusa_git_repo"], "https://github.com/crytic/medusa")
        self.assertEqual(payload["medusa_git_commit"], MEDUSA_BASELINE["git_commit"])
        self.assertEqual(payload["medusa_go_version"], "1.24.0")
        [variant] = payload["fuzzer_variants"]
        self.assertEqual(variant["source"], MEDUSA_PR)
        self.assertNotIn("ci", variant)

    def test_medusa_payload_passes_the_shared_request_validators(self):
        from scripts.benchmark_run_state import validate_fuzzer_variants

        payload = self.build(
            repo_config=MEDUSA,
            variant_key="medusa-pr-512",
            pull_request_build=MEDUSA_PR,
            baseline_build=MEDUSA_BASELINE,
        )

        [variant] = validate_fuzzer_variants(payload["fuzzer_variants"])
        self.assertEqual(variant["source"]["git_commit"], MEDUSA_PR["git_commit"])

    def test_an_identical_medusa_commit_is_refused(self):
        with self.assertRaisesRegex(watcher.CommandError, "nothing to compare"):
            self.build(
                repo_config=MEDUSA,
                variant_key="medusa-pr-512",
                pull_request_build=dict(
                    MEDUSA_PR, git_commit=MEDUSA_BASELINE["git_commit"]
                ),
                baseline_build=MEDUSA_BASELINE,
            )

    def test_a_partial_build_is_refused(self):
        with self.assertRaisesRegex(watcher.ConfigError, "missing: artifact_sha256"):
            self.build(
                pull_request_build={
                    "run_id": "1",
                    "artifact_name": "echidna-linux",
                    "commit": PR_COMMIT,
                }
            )

    def test_build_mode_must_match_the_fuzzer(self):
        with self.assertRaisesRegex(watcher.ConfigError, "does not apply to base"):
            self.build(repo_config=dict(MEDUSA, build_mode="ci"))
        with self.assertRaisesRegex(watcher.ConfigError, "unsupported build_mode"):
            self.build(repo_config=dict(ECHIDNA, build_mode="curl-a-binary"))

    def test_variant_key_stays_prefixed_with_its_base(self):
        self.assertEqual(
            watcher.variant_key_for_pull_request("echidna", 1614), "echidna-pr-1614"
        )
        with self.assertRaises(watcher.CommandError):
            watcher.variant_key_for_pull_request("echidna", 0)


class FakeGitHub:
    """Minimal stand-in for the GitHub API, recording what was asked for."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        for prefix, payload in self.responses.items():
            if path.startswith(prefix):
                return payload
        raise AssertionError(f"unexpected request: {path}")


class BuildResolutionTests(unittest.TestCase):
    def test_ci_build_resolves_run_and_digest(self):
        fetch = FakeGitHub(
            {
                "/repos/crytic/echidna/actions/runs?head_sha=": {
                    "workflow_runs": [
                        {"id": 1, "status": "completed", "conclusion": "failure"},
                        {"id": 33555211362, "status": "completed", "conclusion": "success"},
                    ]
                },
                "/repos/crytic/echidna/actions/runs/1/artifacts": {"artifacts": []},
                "/repos/crytic/echidna/actions/runs/33555211362/artifacts": {
                    "artifacts": [
                        {
                            "name": "echidna-redistributable-x86_64-macos",
                            "expired": False,
                            "digest": "sha256:" + "e" * 64,
                        },
                        {
                            "name": ECHIDNA["artifact_name"],
                            "expired": False,
                            "digest": "sha256:" + PR_BUILD["artifact_sha256"],
                        },
                    ]
                },
            }
        )

        build = watcher.resolve_ci_build(
            fetch,
            repo="crytic/echidna",
            commit=PR_COMMIT,
            artifact_name=ECHIDNA["artifact_name"],
        )

        self.assertEqual(build, PR_BUILD)

    def test_failed_and_expired_builds_are_skipped(self):
        fetch = FakeGitHub(
            {
                "/repos/crytic/echidna/actions/runs?head_sha=": {
                    "workflow_runs": [
                        {"id": 7, "status": "completed", "conclusion": "success"}
                    ]
                },
                "/repos/crytic/echidna/actions/runs/7/artifacts": {
                    "artifacts": [
                        {
                            "name": ECHIDNA["artifact_name"],
                            "expired": True,
                            "digest": "sha256:" + "f" * 64,
                        }
                    ]
                },
            }
        )

        with self.assertRaisesRegex(watcher.CommandError, "unexpired"):
            watcher.resolve_ci_build(
                fetch,
                repo="crytic/echidna",
                commit=PR_COMMIT,
                artifact_name=ECHIDNA["artifact_name"],
            )

    def test_source_build_resolves_a_ref_to_a_commit(self):
        fetch = FakeGitHub(
            {"/repos/crytic/medusa/commits/": {"sha": MEDUSA_PR["git_commit"].upper()}}
        )

        build = watcher.resolve_source_build(
            fetch, repo="crytic/medusa", ref=MEDUSA_PR["git_ref"]
        )

        self.assertEqual(build, MEDUSA_PR)

    def test_refs_and_commits_are_validated_before_any_request(self):
        fetch = FakeGitHub({})

        with self.assertRaises(watcher.CommandError):
            watcher.resolve_source_build(fetch, repo="crytic/medusa", ref="a;rm -rf /")
        with self.assertRaises(watcher.CommandError):
            watcher.resolve_ci_build(
                fetch, repo="crytic/echidna", commit="HEAD", artifact_name="x-linux"
            )
        self.assertEqual(fetch.calls, [])

    def test_comparison_resolves_both_sides_for_a_source_fuzzer(self):
        fetch = FakeGitHub(
            {
                "/repos/crytic/medusa/commits/master": {
                    "sha": MEDUSA_BASELINE["git_commit"]
                },
                "/repos/crytic/medusa/commits/": {"sha": MEDUSA_PR["git_commit"]},
            }
        )

        baseline, pull_request = watcher.resolve_comparison(
            fetch,
            dict(MEDUSA, baseline_ref="master"),
            pull_request_ref=MEDUSA_PR["git_ref"],
            pull_request_commit="",
        )

        self.assertEqual(baseline, MEDUSA_BASELINE)
        self.assertEqual(pull_request, MEDUSA_PR)


class ProvenanceTests(unittest.TestCase):
    def body(self):
        payload = watcher.build_request_payload(
            ECHIDNA,
            variant_key="echidna-pr-1614",
            pull_request_build=PR_BUILD,
            baseline_build=BASE_BUILD,
        )
        return watcher.render_issue_body(
            payload,
            repo="crytic/echidna",
            pull_number=1614,
            comment_id=987654321,
            requester="gustavo-grieco",
            reason="gustavo-grieco is an allowlisted requester",
        )

    def test_issue_body_is_a_valid_request_carrying_its_origin(self):
        body = self.body()

        self.assertIn(watcher.REQUEST_MARKER, body)
        payload = json.loads(body.split("```json", 1)[1].split("```", 1)[0])
        self.assertEqual(payload["fuzzers"], ["echidna", "echidna-pr-1614"])

        provenance = watcher.parse_provenance(body)
        self.assertEqual(
            provenance,
            {
                "source_repo": "crytic/echidna",
                "source_pull_request": 1614,
                "source_comment_id": 987654321,
                "requested_by": "gustavo-grieco",
            },
        )

    def test_body_states_that_approval_is_still_required(self):
        self.assertIn("benchmark/03-approved", self.body())

    def test_dedupe_marker_identifies_the_source_comment(self):
        body = self.body()

        self.assertIn(watcher.dedupe_key("crytic/echidna", 987654321), body)
        self.assertNotIn(watcher.dedupe_key("crytic/echidna", 987654322), body)

    def test_missing_or_malformed_provenance_reads_as_absent(self):
        for body in ("", "no provenance", "<!-- scfuzzbench-external-provenance: { -->"):
            with self.subTest(body=body):
                self.assertIsNone(watcher.parse_provenance(body))


class ReVerificationTests(unittest.TestCase):
    """The recorded requester is re-checked, not trusted because a bot filed it."""

    def test_provenance_requester_is_re_authorized_against_the_same_allowlist(self):
        config = watcher.load_watch_config()
        body = ProvenanceTests().body()
        provenance = watcher.parse_provenance(body)
        repo_config = watcher.repository_config(config, provenance["source_repo"])

        allowed, _ = watcher.authorize_requester(
            provenance["requested_by"], repo_config
        )
        self.assertTrue(allowed)

        forged = dict(provenance, requested_by="drive-by")
        allowed, _ = watcher.authorize_requester(forged["requested_by"], repo_config)
        self.assertFalse(allowed)


if __name__ == "__main__":
    unittest.main()
