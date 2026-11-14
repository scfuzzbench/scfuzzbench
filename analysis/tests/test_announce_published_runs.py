"""Closing a benchmark request only once its run page is actually served."""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts import announce_published_runs as announce  # noqa: E402

RUN_ID = "1770053924"
BENCHMARK_UUID = "0123456789abcdef0123456789abcdef"
RUN_URL = f"https://scfuzzbench.com/runs/{RUN_ID}/{BENCHMARK_UUID}/"

STARTED_COMMENT = "\n".join(
    [
        announce.BOT_MARKER,
        "## Benchmark started",
        "",
        "Run metadata:",
        f"- Run ID: `{RUN_ID}`",
        f"- Benchmark UUID: `{BENCHMARK_UUID}`",
    ]
)


class RunReferenceTests(unittest.TestCase):
    def test_run_identity_is_read_from_the_bot_comment(self):
        self.assertEqual(
            announce.parse_run_reference(STARTED_COMMENT),
            {"run_id": RUN_ID, "benchmark_uuid": BENCHMARK_UUID},
        )

    def test_comments_from_other_authors_are_ignored(self):
        forged = STARTED_COMMENT.replace(announce.BOT_MARKER, "")

        self.assertIsNone(announce.parse_run_reference(forged))

    def test_incomplete_metadata_is_ignored(self):
        without_uuid = "\n".join(
            [announce.BOT_MARKER, "## Benchmark started", f"- Run ID: `{RUN_ID}`"]
        )

        self.assertIsNone(announce.parse_run_reference(without_uuid))

    def test_the_latest_reference_wins(self):
        newer_uuid = "f" * 32
        newer = STARTED_COMMENT.replace(BENCHMARK_UUID, newer_uuid)

        reference = announce.find_run_reference(
            [{"body": STARTED_COMMENT}, {"body": "unrelated"}, {"body": newer}]
        )

        self.assertEqual(reference["benchmark_uuid"], newer_uuid)

    def test_run_page_url_is_built_from_the_reference(self):
        self.assertEqual(announce.run_page_url(RUN_ID, BENCHMARK_UUID), RUN_URL)


class SelectionTests(unittest.TestCase):
    def select(self, *, live_urls, comments_by_issue, issues):
        self.checked = []

        def page_is_live(url):
            self.checked.append(url)
            return url in live_urls

        return announce.select_publishable(
            issues,
            get_comments=lambda number: comments_by_issue.get(number, []),
            page_is_live=page_is_live,
        )

    def test_a_request_is_announced_once_its_page_is_served(self):
        ready = self.select(
            live_urls={RUN_URL},
            comments_by_issue={7: [{"body": STARTED_COMMENT}]},
            issues=[{"number": 7}],
        )

        self.assertEqual(
            ready, [{"issue": 7, "url": RUN_URL, "run_id": RUN_ID, "benchmark_uuid": BENCHMARK_UUID}]
        )

    def test_a_request_whose_page_still_404s_is_left_open(self):
        """The old behaviour closed the issue with a link that did not work."""
        ready = self.select(
            live_urls=set(),
            comments_by_issue={7: [{"body": STARTED_COMMENT}]},
            issues=[{"number": 7}],
        )

        self.assertEqual(ready, [])
        self.assertEqual(self.checked, [RUN_URL])

    def test_a_request_without_run_metadata_is_skipped_without_a_request(self):
        ready = self.select(
            live_urls={RUN_URL},
            comments_by_issue={7: [{"body": "no metadata here"}]},
            issues=[{"number": 7}],
        )

        self.assertEqual(ready, [])
        self.assertEqual(self.checked, [])

    def test_each_request_is_judged_on_its_own_page(self):
        other_uuid = "a" * 32
        other_comment = STARTED_COMMENT.replace(BENCHMARK_UUID, other_uuid)

        ready = self.select(
            live_urls={RUN_URL},
            comments_by_issue={7: [{"body": STARTED_COMMENT}], 8: [{"body": other_comment}]},
            issues=[{"number": 7}, {"number": 8}],
        )

        self.assertEqual([item["issue"] for item in ready], [7])


class AnnouncementTests(unittest.TestCase):
    def test_announcement_links_the_published_page(self):
        body = announce.render_announcement(
            {"run_id": RUN_ID, "benchmark_uuid": BENCHMARK_UUID}
        )

        self.assertIn(announce.BOT_MARKER, body)
        self.assertIn(RUN_URL, body)
        self.assertIn(f"- Run ID: `{RUN_ID}`", body)


class RequestWorkflowContractTests(unittest.TestCase):
    """The request must survive provisioning so this job can close it."""

    def setUp(self):
        self.workflow = (
            REPO_ROOT / ".github" / "workflows" / "benchmark-request.yml"
        ).read_text(encoding="utf-8")
        self.docs = (REPO_ROOT / ".github" / "workflows" / "docs.yml").read_text(
            encoding="utf-8"
        )

    def test_a_started_run_is_labelled_running_and_left_open(self):
        finalize = self.workflow.split("## Benchmark started", 1)[1]

        self.assertIn("setStatusLabel(LABEL_RUNNING)", finalize)
        self.assertNotIn('state: "closed"', finalize)

    def test_the_running_label_is_a_tracked_status(self):
        self.assertIn('LABEL_RUNNING = "benchmark/04-running"', self.workflow)
        self.assertIn("LABEL_RUNNING,", self.workflow)

    def test_the_docs_deployment_announces_published_runs(self):
        self.assertIn("announce-published-runs:", self.docs)
        self.assertIn("scripts/announce_published_runs.py", self.docs)
        self.assertIn("issues: write", self.docs)


if __name__ == "__main__":
    unittest.main()
