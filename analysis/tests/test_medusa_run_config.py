import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "fuzzers" / "medusa" / "run.sh"


def write_common_sh(tmp_dir: Path) -> Path:
    common_sh = tmp_dir / "common.sh"
    common_sh.write_text(
        """
prepare_workspace() { mkdir -p "${SCFUZZBENCH_WORKDIR}/target" "${SCFUZZBENCH_LOG_DIR}"; }
register_shutdown_trap() { prepare_workspace; }
resolve_target_corpus_dir() {
  printf '%s/%s\\n' "${SCFUZZBENCH_WORKDIR}/target" "${1:-$2}"
}
prepare_shared_seed_corpus() { :; }
clone_target() { :; }
capture_target_workspace_anchor() {
  SCFUZZBENCH_TARGET_ROOT_ANCHOR=$(realpath -e "${SCFUZZBENCH_WORKDIR}/target")
  SCFUZZBENCH_TARGET_ROOT_IDENTITY=$(stat -Lc '%d:%i' "${SCFUZZBENCH_WORKDIR}/target")
}
read_strict_descendant_file() { cat -- "$1"; }
write_strict_descendant_file() {
  local destination=$1
  mkdir -p "$(dirname "${destination}")"
  umask 077
  local temporary="${destination}.test-tmp"
  cat >"${temporary}"
  mv -f -- "${temporary}" "${destination}"
}
remove_strict_descendant_file() { rm -f -- "$1"; }
apply_benchmark_type() { :; }
build_target() { :; }
set_default_worker_env() { :; }
log() { printf '%s\\n' "$*" >> "${SCFUZZBENCH_LOG_DIR}/log.txt"; }
require_env() { for name in "$@"; do if [[ -z "${!name:-}" ]]; then return 1; fi; done; }
upload_results() { :; }
run_with_timeout() {
  local log_file=$1
  shift
  {
    printf 'RUN\\t%s' "${log_file}"
    for arg in "$@"; do printf '\\t%s' "${arg}"; done
    printf '\\n'
  } >> "${SCFUZZBENCH_LOG_DIR}/commands.tsv"

  local expect_config=0
  local arg
  for arg in "$@"; do
    if [[ "${expect_config}" -eq 1 ]]; then
      cp "${arg}" "${SCFUZZBENCH_LOG_DIR}/effective-medusa.json"
      printf '%s\\n' "${arg}" > "${SCFUZZBENCH_LOG_DIR}/effective-config-path.txt"
      stat -c '%a' "${arg}" > "${SCFUZZBENCH_LOG_DIR}/effective-config-mode.txt"
      break
    fi
    if [[ "${arg}" == "--config" ]]; then
      expect_config=1
    fi
  done

  case "${TEST_MEDUSA_RUN_MODE:-success}" in
    failure) return 23 ;;
    signal) kill -TERM "${BASHPID}" ;;
  esac
}
""",
        encoding="utf-8",
    )
    return common_sh


class MedusaRunConfigTests(unittest.TestCase):
    def run_medusa(
        self,
        *,
        source_config: object | None = None,
        config_relative_path: str | None = "configs/medusa.json",
        prune_frequency: str | None = None,
        shrink_limit: str | None = None,
        workers: str | None = None,
        run_mode: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path, dict | None, list[str]]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = Path(tmp.name)
        log_dir = tmp_dir / "logs"
        work_dir = tmp_dir / "work"
        target_dir = work_dir / "target"
        target_dir.mkdir(parents=True)
        common_sh = write_common_sh(tmp_dir)

        source_path = None
        if source_config is not None:
            if config_relative_path is None:
                raise AssertionError("source config needs a path")
            source_path = target_dir / config_relative_path
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text(json.dumps(source_config), encoding="utf-8")

        env = os.environ.copy()
        env.update(
            {
                "SCFUZZBENCH_COMMON_SH": str(common_sh),
                "SCFUZZBENCH_WORKDIR": str(work_dir),
                "SCFUZZBENCH_LOG_DIR": str(log_dir),
                "MEDUSA_VERSION": "1.4.1",
            }
        )
        env.pop("MEDUSA_PRUNE_FREQUENCY", None)
        env.pop("MEDUSA_SHRINK_LIMIT", None)
        if config_relative_path is not None:
            env["MEDUSA_CONFIG"] = config_relative_path
        if prune_frequency is not None:
            env["MEDUSA_PRUNE_FREQUENCY"] = prune_frequency
        if shrink_limit is not None:
            env["MEDUSA_SHRINK_LIMIT"] = shrink_limit
        if workers is not None:
            env["MEDUSA_WORKERS"] = workers
        if run_mode is not None:
            env["TEST_MEDUSA_RUN_MODE"] = run_mode

        completed = subprocess.run(
            ["bash", str(SCRIPT)],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

        effective = None
        effective_path = log_dir / "effective-medusa.json"
        if effective_path.exists():
            effective = json.loads(effective_path.read_text(encoding="utf-8"))

        command_args: list[str] = []
        commands_path = log_dir / "commands.tsv"
        if commands_path.exists():
            command_args = commands_path.read_text(encoding="utf-8").splitlines()[0].split(
                "\t"
            )[2:]

        if source_path is not None:
            self.assertEqual(
                json.loads(source_path.read_text(encoding="utf-8")),
                source_config,
                "the checked-in target config must remain unchanged",
            )

        self.assertEqual(
            [],
            list(target_dir.rglob(".scfuzzbench-medusa*")),
            "all effective-config temporary files must be cleaned up",
        )

        return completed, log_dir, effective, command_args

    def test_background_corpus_pruner_is_disabled_by_default(self):
        source = {
            "fuzzing": {
                "pruneFrequency": 5,
                "shrinkLimit": 100000,
                "coverageEnabled": True,
                "workers": 91,
                "targetContracts": ["TargetA", "TargetB"],
            },
            "compilation": {
                "platform": "crytic-compile",
                "target": "../contracts/Target.sol",
            },
            "logging": {"level": "info"},
        }

        completed, log_dir, effective, args = self.run_medusa(
            source_config=source,
            workers="3",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        expected = json.loads(json.dumps(source))
        expected["fuzzing"]["pruneFrequency"] = 0
        expected["fuzzing"]["shrinkLimit"] = 1
        self.assertEqual(effective, expected)
        self.assertIn("--workers", args)
        self.assertEqual(args[args.index("--workers") + 1], "3")
        self.assertNotIn("--prune-frequency", args)
        self.assertEqual(
            (log_dir / "effective-config-mode.txt").read_text(encoding="utf-8").strip(),
            "600",
        )

        invoked_config = Path(
            (log_dir / "effective-config-path.txt").read_text(encoding="utf-8").strip()
        )
        self.assertEqual(invoked_config.parent.name, "configs")
        self.assertFalse(invoked_config.exists(), "working config should be removed")

    def test_explicit_prune_frequency_override_is_preserved(self):
        completed, _, effective, _ = self.run_medusa(
            source_config={"fuzzing": {"pruneFrequency": 5}},
            prune_frequency="17",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(effective["fuzzing"]["pruneFrequency"], 17)
        self.assertEqual(effective["fuzzing"]["shrinkLimit"], 1)

    def test_explicit_shrink_limit_override_is_preserved(self):
        completed, _, effective, _ = self.run_medusa(
            source_config={"fuzzing": {"shrinkLimit": 100000}},
            shrink_limit="17",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(effective["fuzzing"]["shrinkLimit"], 17)
        self.assertEqual(effective["fuzzing"]["pruneFrequency"], 0)

    def test_no_source_config_still_gets_a_disabled_pruner(self):
        completed, _, effective, args = self.run_medusa(
            source_config=None,
            config_relative_path=None,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            effective,
            {"fuzzing": {"pruneFrequency": 0, "shrinkLimit": 1}},
        )
        self.assertIn("--config", args)

    def test_invalid_prune_frequency_fails_before_medusa_runs(self):
        completed, _, effective, args = self.run_medusa(
            source_config={"fuzzing": {}},
            prune_frequency="-1",
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIsNone(effective)
        self.assertEqual(args, [])

    def test_invalid_shrink_limit_fails_before_medusa_runs(self):
        for shrink_limit in ("", "-1", "1.5", "nope", str(2**64)):
            with self.subTest(shrink_limit=shrink_limit):
                completed, _, effective, args = self.run_medusa(
                    source_config={"fuzzing": {}},
                    shrink_limit=shrink_limit,
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertIsNone(effective)
                self.assertEqual(args, [])

    def test_shrink_limit_is_not_shell_evaluated(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "injected"
            completed, _, effective, args = self.run_medusa(
                source_config={"fuzzing": {}},
                shrink_limit=f"$(touch {marker})",
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(marker.exists())
            self.assertIsNone(effective)
            self.assertEqual(args, [])

    def test_invalid_source_config_is_cleaned_up_before_medusa_runs(self):
        completed, _, effective, args = self.run_medusa(source_config=[])

        self.assertNotEqual(completed.returncode, 0)
        self.assertIsNone(effective)
        self.assertEqual(args, [])

    def test_effective_config_is_cleaned_up_when_medusa_fails(self):
        completed, _, effective, args = self.run_medusa(
            source_config={"fuzzing": {}},
            run_mode="failure",
        )

        self.assertEqual(completed.returncode, 23)
        self.assertEqual(
            effective,
            {"fuzzing": {"pruneFrequency": 0, "shrinkLimit": 1}},
        )
        self.assertIn("--config", args)

    def test_effective_config_is_cleaned_up_on_signal(self):
        completed, _, effective, args = self.run_medusa(
            source_config={"fuzzing": {}},
            run_mode="signal",
        )

        self.assertEqual(completed.returncode, 143)
        self.assertEqual(
            effective,
            {"fuzzing": {"pruneFrequency": 0, "shrinkLimit": 1}},
        )
        self.assertIn("--config", args)

    def test_missing_explicit_config_fails_instead_of_using_defaults(self):
        completed, _, effective, args = self.run_medusa(
            source_config=None,
            config_relative_path="missing.json",
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIsNone(effective)
        self.assertEqual(args, [])


if __name__ == "__main__":
    unittest.main()
