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
    return {main_exit_code}
  fi"""
        if main_exit_code
        else ""
    )
    common_sh = tmp_dir / "common.sh"
    common_sh.write_text(
        f"""
prepare_workspace() {{
  mkdir -p "${{SCFUZZBENCH_WORKDIR}}/target" "${{SCFUZZBENCH_LOG_DIR}}"
  cat > "${{SCFUZZBENCH_WORKDIR}}/target/foundry.toml" <<'TOML'
[invariant]
shrink_run_limit = 100000
corpus_dir = "corpus/foundry"
TOML
  SCFUZZBENCH_LOG_ROOT_ANCHOR="${{SCFUZZBENCH_LOG_DIR}}"
  SCFUZZBENCH_LOG_ROOT_IDENTITY="test-log-anchor"
}}
register_shutdown_trap() {{ prepare_workspace; }}
resolve_target_corpus_dir() {{
  printf '%s/%s\\n' "${{SCFUZZBENCH_WORKDIR}}/target" "${{1:-$2}}"
}}
prepare_shared_seed_corpus() {{ mkdir -p "${{SCFUZZBENCH_CORPUS_DIR}}"; }}
remove_strict_descendant_tree() {{ rm -rf -- "$1"; }}
mkdir_strict_descendant() {{ mkdir -p -- "$1"; }}
clone_target() {{ :; }}
capture_target_workspace_anchor() {{
  SCFUZZBENCH_TARGET_ROOT_ANCHOR="${{SCFUZZBENCH_WORKDIR}}/target"
  SCFUZZBENCH_TARGET_ROOT_IDENTITY="test-anchor"
}}
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
  printf '%s\n' "${{FOUNDRY_INVARIANT_SHRINK_RUN_LIMIT-UNSET}}" \
    > "${{SCFUZZBENCH_LOG_DIR}}/foundry-shrink-run-limit.txt"
  printf '%s\n' "${{FOUNDRY_INVARIANT_CORPUS_DIR-UNSET}}" \
    > "${{SCFUZZBENCH_LOG_DIR}}/foundry-corpus-dir.txt"
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
    def run_with_corpus_mode(
        self, keep_corpus: str | None = None
    ) -> tuple[str, str, str]:
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
                    "FOUNDRY_LABEL": "foundry-master",
                }
            )
            env.pop("SCFUZZBENCH_FOUNDRY_KEEP_CORPUS", None)
            if keep_corpus is not None:
                env["SCFUZZBENCH_FOUNDRY_KEEP_CORPUS"] = keep_corpus

            subprocess.check_call(["bash", str(SCRIPT)], env=env)

            corpus_dir = (log_dir / "foundry-corpus-dir.txt").read_text(
                encoding="utf-8"
            )
            config = (work_dir / "target" / "foundry.toml").read_text(
                encoding="utf-8"
            )
            log = (log_dir / "log.txt").read_text(encoding="utf-8")
            return corpus_dir.strip(), config, log

    def test_coverage_guidance_is_enabled_by_default(self):
        corpus_dir, config, log = self.run_with_corpus_mode()

        self.assertTrue(corpus_dir.endswith("/target/corpus/foundry"))
        self.assertIn('corpus_dir = "corpus/foundry"', config)
        self.assertIn("Foundry coverage guidance enabled", log)

    def test_coverage_guidance_can_be_disabled_explicitly(self):
        corpus_dir, config, log = self.run_with_corpus_mode("0")

        self.assertEqual(corpus_dir, "UNSET")
        self.assertNotIn("corpus_dir", config)
        self.assertIn("SCFUZZBENCH_FOUNDRY_KEEP_CORPUS=0", log)

    def test_invalid_coverage_guidance_mode_is_rejected(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_with_corpus_mode("true")

    def run_main_command(self, foundry_test_args: str = "") -> list[str]:
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
                    "SCFUZZBENCH_FOUNDRY_KEEP_CORPUS": "1",
                    "FOUNDRY_LABEL": "foundry-master",
                }
            )
            env.pop("FOUNDRY_INVARIANT_SHRINK_RUN_LIMIT", None)
            if foundry_test_args:
                env["FOUNDRY_TEST_ARGS"] = foundry_test_args

            subprocess.check_call(["bash", str(SCRIPT)], env=env)

            line = (log_dir / "commands.tsv").read_text(encoding="utf-8").splitlines()[0]
            return line.split("\t")[2:]

    def test_invariant_workers_default_to_auto(self):
        args = self.run_main_command()

        workers_idx = args.index("--invariant-workers")
        self.assertEqual(args[workers_idx + 1], "auto")

    def test_explicit_invariant_worker_override_is_preserved(self):
        for override in ("--invariant-workers 4", "--invariant-workers=4"):
            with self.subTest(override=override):
                args = self.run_main_command(override)

                self.assertNotIn("auto", args)
                if "=" in override:
                    self.assertIn("--invariant-workers=4", args)
                else:
                    workers_idx = args.index("--invariant-workers")
                    self.assertEqual(args[workers_idx + 1], "4")

    def test_invariant_corpus_dir_override_is_rejected(self):
        for override in (
            "--invariant-corpus-dir /tmp/other-corpus",
            "--invariant-corpus-dir=/tmp/other-corpus",
        ):
            with self.subTest(override=override):
                with self.assertRaises(subprocess.CalledProcessError):
                    self.run_main_command(override)

    def run_with_shrink_limit(
        self, shrink_limit: str | None = None
    ) -> tuple[subprocess.CompletedProcess[bytes], str | None, list[str]]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = Path(tmp.name)
        log_dir = tmp_dir / "logs"
        work_dir = tmp_dir / "work"
        common_sh = write_common_sh(tmp_dir)

        env = os.environ.copy()
        env.update(
            {
                "SCFUZZBENCH_COMMON_SH": str(common_sh),
                "SCFUZZBENCH_WORKDIR": str(work_dir),
                "SCFUZZBENCH_LOG_DIR": str(log_dir),
                "SCFUZZBENCH_FOUNDRY_KEEP_CORPUS": "1",
                "FOUNDRY_LABEL": "foundry-master",
            }
        )
        env.pop("FOUNDRY_INVARIANT_SHRINK_RUN_LIMIT", None)
        if shrink_limit is not None:
            env["FOUNDRY_INVARIANT_SHRINK_RUN_LIMIT"] = shrink_limit

        completed = subprocess.run(["bash", str(SCRIPT)], env=env, check=False)
        captured_path = log_dir / "foundry-shrink-run-limit.txt"
        captured = None
        if captured_path.exists():
            captured = captured_path.read_text(encoding="utf-8").strip()
        commands_path = log_dir / "commands.tsv"
        commands = []
        if commands_path.exists():
            commands = commands_path.read_text(encoding="utf-8").splitlines()
        return completed, captured, commands

    def test_inline_shrink_limit_defaults_to_one(self):
        completed, captured, commands = self.run_with_shrink_limit()

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(captured, "1")
        self.assertNotEqual(commands, [])

    def test_explicit_inline_shrink_limit_override_is_preserved(self):
        for shrink_limit in ("0", "7", str(2**32 - 1)):
            with self.subTest(shrink_limit=shrink_limit):
                completed, captured, commands = self.run_with_shrink_limit(
                    shrink_limit
                )

                self.assertEqual(completed.returncode, 0)
                self.assertEqual(captured, shrink_limit)
                self.assertNotEqual(commands, [])

    def test_invalid_inline_shrink_limit_fails_before_forge_runs(self):
        for shrink_limit in ("", "-1", "1.5", "nope", str(2**32)):
            with self.subTest(shrink_limit=shrink_limit):
                completed, captured, commands = self.run_with_shrink_limit(
                    shrink_limit
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertIsNone(captured)
                self.assertEqual(commands, [])

    def test_inline_shrink_limit_is_not_shell_evaluated(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "injected"
            completed, captured, commands = self.run_with_shrink_limit(
                f"$(touch {marker})"
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(marker.exists())
            self.assertIsNone(captured)
            self.assertEqual(commands, [])

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
