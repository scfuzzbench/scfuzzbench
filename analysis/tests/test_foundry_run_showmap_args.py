import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "fuzzers" / "foundry" / "run.sh"


def write_common_sh(
    tmp_dir: Path,
    *,
    include_timeout: bool = False,
    main_exit_code: int = 0,
    record_upload: bool = False,
) -> Path:
    timeout_line = (
        '    printf \'\\t%s\' "${SCFUZZBENCH_TIMEOUT_SECONDS}"\n'
        if include_timeout
        else ""
    )
    upload_body = (
        "printf 'UPLOAD\\n' >> \"${SCFUZZBENCH_LOG_DIR}/commands.tsv\""
        if record_upload
        else ":"
    )
    main_exit_block = (
        f"""
  if [[ "${{log_file}}" == *foundry.log ]]; then
    set -e
    return {main_exit_code}
  fi"""
        if main_exit_code
        else ""
    )
    common_sh = tmp_dir / "common.sh"
    common_sh.write_text(
        f"""
register_shutdown_trap() {{ :; }}
prepare_workspace() {{ mkdir -p "${{SCFUZZBENCH_WORKDIR}}/target" "${{SCFUZZBENCH_LOG_DIR}}"; }}
clone_target() {{ :; }}
apply_benchmark_type() {{ :; }}
build_target() {{ :; }}
set_default_worker_env() {{ :; }}
log() {{ printf '%s\\n' "$*" >> "${{SCFUZZBENCH_LOG_DIR}}/log.txt"; }}
require_env() {{ for name in "$@"; do if [[ -z "${{!name:-}}" ]]; then return 1; fi; done; }}
now_epoch_seconds() {{ date +%s; }}
log_duration() {{ :; }}
append_runner_command_log() {{
  timeout_seconds=$1
  grace_seconds=$2
  shift 2
  {{
    printf 'APPEND\\t%s\\t%s' "${{timeout_seconds}}" "${{grace_seconds}}"
    for arg in "$@"; do printf '\\t%s' "$arg"; done
    printf '\\n'
  }} >> "${{SCFUZZBENCH_LOG_DIR}}/commands.tsv"
}}
upload_results() {{ {upload_body}; }}
run_with_timeout() {{
  log_file=$1
  {{
    printf 'RUN'
{timeout_line}
    for arg in "$@"; do printf '\\t%s' "$arg"; done
    printf '\\n'
  }} >> "${{SCFUZZBENCH_LOG_DIR}}/commands.tsv"
{main_exit_block}
  return 0
}}
""",
        encoding="utf-8",
    )
    return common_sh


class FoundryRunShowmapArgsTests(unittest.TestCase):
    def test_showmap_replay_keeps_test_args_but_uses_script_showmap_args(self):
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
                    "SCFUZZBENCH_RUN_ID": "bench-trial",
                    "SCFUZZBENCH_FOUNDRY_SHOWMAP": "1",
                    "FOUNDRY_LABEL": "foundry-master",
                    "FOUNDRY_TEST_ARGS": "--fork-url http://rpc --threads 3 --showmap-out /tmp/user-showmap --showmap-trial user-trial",
                }
            )

            subprocess.check_call(["bash", str(SCRIPT)], env=env)

            lines = (log_dir / "commands.tsv").read_text(encoding="utf-8").splitlines()
            commands = [line.split("\t") for line in lines]
            self.assertEqual(len(commands), 2)
            replay = commands[1]
            replay_args = replay[2:]

            self.assertEqual(replay[1], str(log_dir / "foundry_showmap.log"))
            self.assertIn("--fork-url", replay_args)
            self.assertIn("http://rpc", replay_args)
            self.assertIn("--threads", replay_args)
            self.assertIn("3", replay_args)
            self.assertNotIn("/tmp/user-showmap", replay_args)
            self.assertNotIn("user-trial", replay_args)

            showmap_out_idx = replay_args.index("--showmap-out")
            showmap_trial_idx = replay_args.index("--showmap-trial")
            self.assertEqual(replay_args[showmap_out_idx + 1], str(log_dir / "showmap"))
            self.assertEqual(replay_args[showmap_trial_idx + 1], "bench-trial")
            # Without an explicit FOUNDRY_SHOWMAP_CORPUS_DIR override we must NOT
            # pass --showmap-corpus-dir: forge then resolves the per-test corpus
            # dir from config (`[invariant] corpus_dir/<Contract>`), which is the
            # actual directory the fuzz campaign persisted the corpus to. Passing
            # the un-nested base dir would make the replay read an empty directory.
            self.assertNotIn("--showmap-corpus-dir", replay_args)

    def test_showmap_replay_uses_explicit_corpus_override_only_when_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            log_dir = tmp_dir / "logs"
            work_dir = tmp_dir / "work"
            corpus_dir = tmp_dir / "seed-corpus"
            common_sh = write_common_sh(tmp_dir)

            env = os.environ.copy()
            env.update(
                {
                    "SCFUZZBENCH_COMMON_SH": str(common_sh),
                    "SCFUZZBENCH_WORKDIR": str(work_dir),
                    "SCFUZZBENCH_LOG_DIR": str(log_dir),
                    "SCFUZZBENCH_RUN_ID": "bench-trial",
                    "SCFUZZBENCH_FOUNDRY_SHOWMAP": "1",
                    "FOUNDRY_LABEL": "foundry-master",
                    "FOUNDRY_SHOWMAP_CORPUS_DIR": str(corpus_dir),
                }
            )

            subprocess.check_call(["bash", str(SCRIPT)], env=env)

            lines = (log_dir / "commands.tsv").read_text(encoding="utf-8").splitlines()
            commands = [line.split("\t") for line in lines]
            replay_args = commands[1][2:]
            corpus_idx = replay_args.index("--showmap-corpus-dir")
            self.assertEqual(replay_args[corpus_idx + 1], str(corpus_dir))

    def test_showmap_replay_uses_bounded_default_timeout(self):
        def run_case(timeout: str, override: str | None = None) -> list[list[str]]:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_dir = Path(tmp)
                log_dir = tmp_dir / "logs"
                work_dir = tmp_dir / "work"
                common_sh = write_common_sh(tmp_dir, include_timeout=True)

                env = os.environ.copy()
                env.update(
                    {
                        "SCFUZZBENCH_COMMON_SH": str(common_sh),
                        "SCFUZZBENCH_WORKDIR": str(work_dir),
                        "SCFUZZBENCH_LOG_DIR": str(log_dir),
                        "SCFUZZBENCH_RUN_ID": "bench-trial",
                        "SCFUZZBENCH_TIMEOUT_SECONDS": timeout,
                        "SCFUZZBENCH_FOUNDRY_SHOWMAP": "1",
                        "FOUNDRY_LABEL": "foundry-master",
                    }
                )
                if override is not None:
                    env["SCFUZZBENCH_FOUNDRY_SHOWMAP_TIMEOUT_SECONDS"] = override

                subprocess.check_call(["bash", str(SCRIPT)], env=env)
                lines = (log_dir / "commands.tsv").read_text(encoding="utf-8").splitlines()
                return [line.split("\t") for line in lines]

        long_campaign = run_case("86400")
        self.assertEqual(long_campaign[0][1], "86400")
        self.assertEqual(long_campaign[1][1], "1800")

        short_campaign = run_case("60")
        self.assertEqual(short_campaign[0][1], "60")
        self.assertEqual(short_campaign[1][1], "60")

        explicit_override = run_case("86400", "42")
        self.assertEqual(explicit_override[0][1], "86400")
        self.assertEqual(explicit_override[1][1], "42")

    def test_showmap_and_upload_run_after_main_forge_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            log_dir = tmp_dir / "logs"
            work_dir = tmp_dir / "work"
            common_sh = write_common_sh(tmp_dir, main_exit_code=7, record_upload=True)

            env = os.environ.copy()
            env.update(
                {
                    "SCFUZZBENCH_COMMON_SH": str(common_sh),
                    "SCFUZZBENCH_WORKDIR": str(work_dir),
                    "SCFUZZBENCH_LOG_DIR": str(log_dir),
                    "SCFUZZBENCH_RUN_ID": "bench-trial",
                    "SCFUZZBENCH_FOUNDRY_SHOWMAP": "1",
                    "FOUNDRY_LABEL": "foundry-master",
                }
            )

            completed = subprocess.run(["bash", str(SCRIPT)], env=env, check=False)

            self.assertEqual(completed.returncode, 7)
            lines = (log_dir / "commands.tsv").read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[-1], "UPLOAD")
            self.assertIn("foundry.log", lines[0])
            self.assertIn("foundry_showmap.log", lines[1])


if __name__ == "__main__":
    unittest.main()
