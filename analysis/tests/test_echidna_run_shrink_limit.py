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
    def run_main_command(
        self,
        *,
        extra_args: str = "",
    ) -> list[str]:
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

            subprocess.check_call(["bash", str(SCRIPT)], env=env)
            line = (
                (log_dir / "commands.tsv").read_text(encoding="utf-8").splitlines()[0]
            )
            return line.split("\t")[1:]

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


if __name__ == "__main__":
    unittest.main()
