import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "fuzzers" / "echidna" / "run.sh"


def write_common_sh(tmp_dir: Path) -> Path:
    common_sh = tmp_dir / "common.sh"
    common_sh.write_text(
        """
register_shutdown_trap() { :; }
prepare_workspace() {
  mkdir -p "${SCFUZZBENCH_WORKDIR}/target" "${SCFUZZBENCH_LOG_DIR}"
}
clone_target() {
  printf 'shrinkLimit: 100000\\n' > "${SCFUZZBENCH_WORKDIR}/target/echidna.yaml"
}
apply_benchmark_type() { :; }
build_target() { :; }
set_default_worker_env() { :; }
log() { printf '%s\\n' "$*" >> "${SCFUZZBENCH_LOG_DIR}/log.txt"; }
require_env() {
  for name in "$@"; do
    if [[ -z "${!name:-}" ]]; then
      return 1
    fi
  done
}
upload_results() { :; }
run_with_timeout() {
  shift
  {
    printf 'RUN'
    for arg in "$@"; do
      printf '\\t%s' "$arg"
    done
    printf '\\n'
  } >> "${SCFUZZBENCH_LOG_DIR}/commands.tsv"
}
""",
        encoding="utf-8",
    )
    return common_sh


class EchidnaRunShrinkLimitTests(unittest.TestCase):
    def run_script(
        self,
        *,
        extra_args: str = "",
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            log_dir = tmp_dir / "logs"
            work_dir = tmp_dir / "work"
            common_sh = write_common_sh(tmp_dir)

            env = os.environ.copy()
            env.update(
                {
                    "SCFUZZBENCH_COMMON_SH": str(common_sh),
                    "SCFUZZBENCH_WORKDIR": str(work_dir),
                    "SCFUZZBENCH_LOG_DIR": str(log_dir),
                    "SCFUZZBENCH_BENCHMARK_TYPE": "property",
                    "ECHIDNA_VERSION": "2.3.1",
                    "ECHIDNA_CONFIG": "echidna.yaml",
                }
            )
            if extra_args:
                env["ECHIDNA_EXTRA_ARGS"] = extra_args
            else:
                env.pop("ECHIDNA_EXTRA_ARGS", None)

            result = subprocess.run(
                ["bash", str(SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            commands_path = log_dir / "commands.tsv"
            args = []
            if commands_path.exists():
                line = commands_path.read_text(encoding="utf-8").splitlines()[0]
                args = line.split("\t")[1:]
            log_path = log_dir / "log.txt"
            if log_path.exists():
                result.stderr += log_path.read_text(encoding="utf-8")
            return result, args

    def run_main_command(
        self,
        *,
        extra_args: str = "",
    ) -> list[str]:
        result, args = self.run_script(extra_args=extra_args)
        result.check_returncode()
        return args

    def shrink_limit_values(self, args: list[str]) -> list[str]:
        values = []
        for index, arg in enumerate(args):
            if arg == "--shrink-limit":
                values.append(args[index + 1])
            elif arg.startswith("--shrink-limit="):
                values.append(arg.split("=", 1)[1])
        return values

    def test_default_overrides_large_target_config_value(self):
        args = self.run_main_command()

        self.assertEqual(self.shrink_limit_values(args), ["1"])
        self.assertIn("--config", args)
        self.assertIn("echidna.yaml", args)

    def test_unrelated_extra_args_keep_default(self):
        args = self.run_main_command(extra_args="--server 3000")

        self.assertEqual(self.shrink_limit_values(args), ["1"])
        self.assertIn("--server", args)
        self.assertIn("3000", args)

    def test_explicit_extra_args_override_replaces_default(self):
        for extra_args in (
            "--server 3000 --shrink-limit 7",
            "--server 3000 --shrink-limit=7",
        ):
            with self.subTest(extra_args=extra_args):
                args = self.run_main_command(extra_args=extra_args)

                self.assertEqual(self.shrink_limit_values(args), ["7"])
                self.assertIn("--server", args)
                self.assertIn("3000", args)

    def test_shell_quoting_preserves_one_argument_without_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "injected"
            literal = f"$(touch {marker})"

            args = self.run_main_command(
                extra_args=f'--crytic-args "{literal}" --shrink-limit 0'
            )

            self.assertFalse(marker.exists())
            self.assertEqual(args[args.index("--crytic-args") + 1], literal)
            self.assertEqual(self.shrink_limit_values(args), ["0"])

    def test_malformed_quoting_fails_before_launch(self):
        result, args = self.run_script(extra_args='--server "3000')

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(args, [])
        self.assertIn("Invalid ECHIDNA_EXTRA_ARGS", result.stderr)
        self.assertIn("No closing quotation", result.stderr)

    def test_invalid_or_duplicate_shrink_limit_fails_before_launch(self):
        cases = (
            ("--shrink-limit", "non-negative integer"),
            ("--shrink-limit=", "non-negative integer"),
            ("--shrink-limit nope", "non-negative integer"),
            ("--shrink-limit -1", "non-negative integer"),
            (
                "--shrink-limit 7 --shrink-limit=8",
                "at most one --shrink-limit",
            ),
        )
        for extra_args, expected_error in cases:
            with self.subTest(extra_args=extra_args):
                result, args = self.run_script(extra_args=extra_args)

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(args, [])
                self.assertIn(expected_error, result.stderr)


if __name__ == "__main__":
    unittest.main()
