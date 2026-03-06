import shlex
import subprocess
import unittest
from pathlib import Path


COMMON_SH = Path(__file__).resolve().parents[2] / "fuzzers" / "_shared" / "common.sh"


class RunnerCommandSanitizationTests(unittest.TestCase):
    def sanitize(self, *args: str) -> str:
        quoted_args = " ".join(shlex.quote(arg) for arg in args)
        command = f"source {shlex.quote(str(COMMON_SH))}; sanitize_command_for_log {quoted_args}"
        return subprocess.check_output(["bash", "-lc", command], text=True).strip()

    def test_redacts_named_secret_arguments_and_env_assignments(self):
        rendered = self.sanitize(
            "forge",
            "test",
            "--token",
            "abc123",
            "--api-key=xyz",
            "SECRET_VAR=shh",
            "--threads",
            "16",
        )
        self.assertIn("--token __SCFUZZBENCH_REDACTED__", rendered)
        self.assertIn("--api-key=__SCFUZZBENCH_REDACTED__", rendered)
        self.assertIn("SECRET_VAR=__SCFUZZBENCH_REDACTED__", rendered)
        self.assertIn("--threads 16", rendered)

    def test_redacts_url_userinfo_credentials(self):
        rendered = self.sanitize(
            "forge",
            "test",
            "--rpc-url=https://user:pass@example.test",
        )
        self.assertIn(
            "--rpc-url=https://__SCFUZZBENCH_REDACTED__@example.test",
            rendered,
        )
        self.assertNotIn("user:pass@", rendered)


if __name__ == "__main__":
    unittest.main()
