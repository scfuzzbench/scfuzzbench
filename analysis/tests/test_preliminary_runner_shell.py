import json
import shlex
import subprocess
from pathlib import Path
import tempfile
import time
import unittest


COMMON_SH = Path(__file__).resolve().parents[2] / "fuzzers" / "_shared" / "common.sh"
SNAPSHOT_HELPER = Path(__file__).resolve().parents[2] / "scripts" / "preliminary_snapshot.py"


def run_bash(script: str, *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-lc", script],
        text=True,
        capture_output=True,
        check=check,
        timeout=10,
    )


class PreliminaryRunnerShellTests(unittest.TestCase):
    def test_process_identity_disappearance_is_silent_and_fail_closed(self):
        common = shlex.quote(str(COMMON_SH))
        open_race = run_bash(
            f"""
            set -T
            source {common}
            sleep 30 &
            target=$!
            cleanup_target() {{
              trap - DEBUG
              kill "$target" 2>/dev/null || true
              wait "$target" 2>/dev/null || true
            }}
            trap cleanup_target EXIT
            arm_race=0
            remove_target_before_proc_open() {{
              if [[ "$arm_race" == 1 && "$BASH_COMMAND" == *'read -r process_stat'* ]]; then
                arm_race=0
                kill "$target"
                wait "$target" 2>/dev/null || true
              fi
            }}
            trap remove_target_before_proc_open DEBUG
            arm_race=1
            if preliminary_process_identity "$target"; then
              echo unexpected-success
              exit 1
            fi
            trap - DEBUG
            echo open-race-rejected
            """
        )
        self.assertEqual("open-race-rejected", open_race.stdout.strip())
        self.assertEqual("", open_race.stderr)

        read_race = run_bash(
            f"""
            source {common}
            read() {{
              echo 'read: read error: 0: No such process' >&2
              return 1
            }}
            if preliminary_process_identity "$$"; then
              echo unexpected-success
              exit 1
            fi
            echo read-race-rejected
            """
        )
        self.assertEqual("read-race-rejected", read_race.stdout.strip())
        self.assertEqual("", read_race.stderr)

    def test_interval_defaults_hourly_and_supports_explicit_disable(self):
        common = shlex.quote(str(COMMON_SH))
        result = run_bash(
            f"""
            source {common}
            unset SCFUZZBENCH_PRELIMINARY_INTERVAL_SECONDS
            first=$(preliminary_snapshot_interval_seconds)
            SCFUZZBENCH_PRELIMINARY_INTERVAL_SECONDS=off
            second=$(preliminary_snapshot_interval_seconds)
            printf '%s %s\n' "$first" "$second"
            """
        )
        self.assertEqual("3600 0", result.stdout.strip())

    def test_opaque_run_id_schedules_from_explicit_start_epoch_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = int(time.time())
            result = run_bash(
                f"""
                export SCFUZZBENCH_ROOT={shlex.quote(tmp)}
                export SCFUZZBENCH_RUN_ID=gh-24680-1
                export SCFUZZBENCH_RUN_STARTED_AT_EPOCH={now}
                export SCFUZZBENCH_BENCHMARK_UUID={'a' * 32}
                export SCFUZZBENCH_FUZZER_KEY=foundry
                export SCFUZZBENCH_RUN_INDEX=0
                export SCFUZZBENCH_TIMEOUT_SECONDS=86400
                export SCFUZZBENCH_PRELIMINARY_INTERVAL_SECONDS=3600
                export SCFUZZBENCH_PRELIMINARY_SNAPSHOT_SCRIPT={shlex.quote(str(SNAPSHOT_HELPER))}
                source {shlex.quote(str(COMMON_SH))}
                start_preliminary_snapshots
                pid="$SCFUZZBENCH_PRELIMINARY_PID"
                kill -0 "$pid"
                stop_preliminary_snapshots
                if kill -0 "$pid" 2>/dev/null; then
                  echo still-running
                  exit 1
                fi
                echo stopped
                """
            )
        self.assertEqual("stopped", result.stdout.strip())
        self.assertNotIn("numeric run ID", result.stderr)

    def test_detached_capture_rebuilds_trust_anchors_after_resourcing_common(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upload_record = root / "upload.json"
            upload_helper = root / "record_pinned_upload.py"
            upload_helper.write_text(
                """\
import json
import os
from pathlib import Path
import sys
import zipfile


def value(flag):
    index = sys.argv.index(flag)
    return sys.argv[index + 1]


root_path = Path(value("--root-path"))
root_anchor = value("--root-anchor")
root_identity = value("--root-identity")
source = Path(value("--path"))
resolved_root = str(root_path.resolve(strict=True))
root_stat = root_path.stat()
expected_identity = f"{root_stat.st_dev}:{root_stat.st_ino}"
resolved_source = str(source.resolve(strict=True))
if root_anchor != resolved_root:
    raise SystemExit("framework root anchor was not reconstructed exactly")
if root_identity != expected_identity:
    raise SystemExit("framework root identity was not reconstructed exactly")
if os.path.commonpath((resolved_root, resolved_source)) != resolved_root:
    raise SystemExit("snapshot archive escaped the reconstructed root")
if not source.is_file():
    raise SystemExit("snapshot archive was not created")
with zipfile.ZipFile(source) as archive:
    if "logs/runner.log" not in archive.namelist():
        raise SystemExit("snapshot did not capture the anchored log tree")

Path(os.environ["SCFUZZBENCH_TEST_UPLOAD_RECORD"]).write_text(
    json.dumps(
        {
            "root_path": str(root_path),
            "root_anchor": root_anchor,
            "root_identity": root_identity,
            "archive_size": source.stat().st_size,
            "runner_log_captured": True,
            "bucket": value("--bucket"),
            "key": value("--key"),
            "immutable": "--immutable" in sys.argv,
        },
        sort_keys=True,
    ),
    encoding="utf-8",
)
""",
                encoding="utf-8",
            )
            result = run_bash(
                f"""
                unset SCFUZZBENCH_LOCAL_MODE
                export SCFUZZBENCH_ROOT={shlex.quote(str(root))}
                export SCFUZZBENCH_WORKDIR={shlex.quote(str(root / "work"))}
                export SCFUZZBENCH_LOG_DIR={shlex.quote(str(root / "logs"))}
                export SCFUZZBENCH_COMMON_SH={shlex.quote(str(COMMON_SH))}
                export SCFUZZBENCH_SAFE_PATH_OPS={shlex.quote(
                    str(COMMON_SH.with_name("safe_path_ops.py"))
                )}
                export SCFUZZBENCH_PRELIMINARY_SNAPSHOT_SCRIPT={shlex.quote(str(SNAPSHOT_HELPER))}
                export SCFUZZBENCH_PINNED_UPLOAD_HELPER={shlex.quote(str(upload_helper))}
                export SCFUZZBENCH_TEST_UPLOAD_RECORD={shlex.quote(str(upload_record))}
                export SCFUZZBENCH_DISABLE_IMDS_CREDENTIAL_CACHE=1
                export SCFUZZBENCH_S3_BUCKET=test-bucket
                export SCFUZZBENCH_RUN_ID=gh-24680-1
                export SCFUZZBENCH_BENCHMARK_UUID={'a' * 32}
                export SCFUZZBENCH_FUZZER_KEY=foundry
                export SCFUZZBENCH_RUN_INDEX=0
                export SCFUZZBENCH_FUZZER_LABEL=foundry-test
                export SCFUZZBENCH_INSTANCE_ID=i-0123abcd
                export SCFUZZBENCH_TIMEOUT_SECONDS=180
                export SCFUZZBENCH_PRELIMINARY_INTERVAL_SECONDS=60
                export SCFUZZBENCH_PRELIMINARY_HELPER_TIMEOUT_SECONDS=30
                export SCFUZZBENCH_PRELIMINARY_UPLOAD_ATTEMPTS=1
                export SCFUZZBENCH_PRELIMINARY_UPLOAD_RETRY_SECONDS=0
                source {shlex.quote(str(COMMON_SH))}
                prepare_workspace
                printf 'runner started\n' >"$SCFUZZBENCH_LOG_DIR/runner.log"
                export SCFUZZBENCH_RUN_STARTED_AT_EPOCH=$(( $(date +%s) - 58 ))
                cleanup_test_loop() {{
                  stop_preliminary_snapshots 2>/dev/null || true
                }}
                trap cleanup_test_loop EXIT
                start_preliminary_snapshots
                for _ in $(seq 1 400); do
                  if [[ -s "$SCFUZZBENCH_TEST_UPLOAD_RECORD" \
                        && ! -e "$SCFUZZBENCH_ROOT/preliminary-checkpoints/active.pid" ]]; then
                    break
                  fi
                  sleep 0.02
                done
                [[ -s "$SCFUZZBENCH_TEST_UPLOAD_RECORD" ]]
                [[ ! -e "$SCFUZZBENCH_ROOT/preliminary-checkpoints/active.pid" ]]
                stop_preliminary_snapshots
                trap - EXIT
                echo captured
                """,
                check=False,
            )
            self.assertEqual(
                0,
                result.returncode,
                msg=f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
            )
            self.assertEqual("captured", result.stdout.strip())
            self.assertIn(
                "Uploaded preliminary checkpoint 000001", result.stderr
            )
            upload = json.loads(upload_record.read_text(encoding="utf-8"))
            root_stat = root.stat()
            self.assertEqual(str(root), upload["root_path"])
            self.assertEqual(str(root.resolve()), upload["root_anchor"])
            self.assertEqual(
                f"{root_stat.st_dev}:{root_stat.st_ino}",
                upload["root_identity"],
            )
            self.assertGreater(upload["archive_size"], 0)
            self.assertTrue(upload["runner_log_captured"])
            self.assertEqual("test-bucket", upload["bucket"])
            self.assertEqual(
                "preliminary/gh-24680-1/"
                f"{'a' * 32}/snapshots/000001/"
                "foundry-0-i-0123abcd/snapshot.zip",
                upload["key"],
            )
            self.assertTrue(upload["immutable"])

    def test_existing_matching_object_is_idempotent_but_divergent_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "snapshot.zip"
            source.write_bytes(b"stable snapshot bytes")
            remote = Path(tmp) / "remote.zip"
            common = shlex.quote(str(COMMON_SH))
            source_q = shlex.quote(str(source))
            remote_q = shlex.quote(str(remote))
            remote.write_bytes(source.read_bytes())
            matching = run_bash(
                f"""
                export SCFUZZBENCH_LOCAL_MODE=1
                export SCFUZZBENCH_ROOT={shlex.quote(tmp)}
                export SCFUZZBENCH_S3_BUCKET=bucket
                source {common}
                prepare_workspace
                upload_pinned_s3_file() {{
                  if cmp -s -- "$1" {remote_q}; then return 0; fi
                  echo "Refusing to overwrite preliminary object" >&2
                  return 1
                }}
                put_preliminary_immutable {source_q} preliminary/gh-1/{'a' * 32}/snapshot.zip
                """
            )
            self.assertEqual(0, matching.returncode)

            remote.write_bytes(b"different")
            divergent = run_bash(
                f"""
                export SCFUZZBENCH_LOCAL_MODE=1
                export SCFUZZBENCH_ROOT={shlex.quote(tmp)}
                export SCFUZZBENCH_S3_BUCKET=bucket
                source {common}
                prepare_workspace
                upload_pinned_s3_file() {{
                  if cmp -s -- "$1" {remote_q}; then return 0; fi
                  echo "Refusing to overwrite preliminary object" >&2
                  return 1
                }}
                put_preliminary_immutable {source_q} preliminary/gh-1/{'a' * 32}/snapshot.zip
                """,
                check=False,
            )
            self.assertNotEqual(0, divergent.returncode)
            self.assertIn("Refusing to overwrite preliminary object", divergent.stderr)

    def test_stop_kills_entire_inflight_checkpoint_process_group_promptly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_bash(
                f"""
                export SCFUZZBENCH_ROOT={shlex.quote(tmp)}
                source {shlex.quote(str(COMMON_SH))}
                prepare_workspace
                mkdir -p {shlex.quote(str(root / "preliminary-checkpoints"))}
                setsid bash -c 'sleep 30' >/dev/null 2>&1 &
                capture_pid=$!
                capture_start=$(preliminary_wait_for_process_start_ticks "$capture_pid")
                sleep 30 >/dev/null 2>&1 &
                loop_pid=$!
                loop_start=$(preliminary_wait_for_process_start_ticks "$loop_pid")
                export SCFUZZBENCH_PRELIMINARY_PID="$loop_pid"
                export SCFUZZBENCH_PRELIMINARY_PID_START_TICKS="$loop_start"
                preliminary_write_active_owner \
                  {shlex.quote(str(root / "preliminary-checkpoints" / "active.pid"))} \
                  "$loop_pid" "$loop_start" "$capture_pid" "$capture_start"
                stop_preliminary_snapshots
                if kill -0 -- "-${{capture_pid}}" 2>/dev/null; then
                  echo capture-still-running
                  exit 1
                fi
                echo stopped-group
                """
            )
        self.assertEqual("stopped-group", result.stdout.strip())

    def test_stale_cached_loop_token_never_signals_reused_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_bash(
                f"""
                export SCFUZZBENCH_ROOT={shlex.quote(tmp)}
                source {shlex.quote(str(COMMON_SH))}
                sleep 30 &
                unrelated_pid=$!
                unrelated_start=$(preliminary_wait_for_process_start_ticks "$unrelated_pid")
                export SCFUZZBENCH_PRELIMINARY_PID="$unrelated_pid"
                export SCFUZZBENCH_PRELIMINARY_PID_START_TICKS=$((unrelated_start + 1))
                stop_preliminary_snapshots
                if ! preliminary_process_owned "$unrelated_pid" "$unrelated_start"; then
                  echo unrelated-was-signaled
                  exit 1
                fi
                kill "$unrelated_pid"
                wait "$unrelated_pid" 2>/dev/null || true
                echo stale-token-ignored
                """
            )
        self.assertEqual("stale-token-ignored", result.stdout.strip())

    def test_stop_before_preliminary_start_is_silent_and_creates_no_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "preliminary-checkpoints"
            result = run_bash(
                f"""
                export SCFUZZBENCH_ROOT={shlex.quote(tmp)}
                source {shlex.quote(str(COMMON_SH))}
                prepare_workspace
                stop_preliminary_snapshots
                if [[ -e {shlex.quote(str(state_dir))} ]]; then
                  echo preliminary-state-created
                  exit 1
                fi
                echo stopped-before-start
                """
            )
        self.assertEqual("stopped-before-start", result.stdout.strip())
        self.assertEqual("", result.stderr)

    def test_atomic_owner_write_does_not_change_runner_umask(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner = Path(tmp) / "preliminary-checkpoints" / "active.pid"
            result = run_bash(
                f"""
                export SCFUZZBENCH_ROOT={shlex.quote(tmp)}
                source {shlex.quote(str(COMMON_SH))}
                prepare_workspace
                umask 0022
                before=$(umask)
                preliminary_write_active_owner \
                  {shlex.quote(str(owner))} 100 200 300 400
                after=$(umask)
                read -r loop_pid loop_start capture_pid capture_start \
                  < {shlex.quote(str(owner))}
                printf '%s %s %s %s %s %s\n' \
                  "$before" "$after" "$loop_pid" "$loop_start" \
                  "$capture_pid" "$capture_start"
                """
            )
        self.assertEqual("0022 0022 100 200 300 400", result.stdout.strip())

    def test_stale_capture_token_and_nonleader_group_are_never_group_signaled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_bash(
                f"""
                export SCFUZZBENCH_ROOT={shlex.quote(tmp)}
                source {shlex.quote(str(COMMON_SH))}
                prepare_workspace
                mkdir -p {shlex.quote(str(root / "preliminary-checkpoints"))}
                sleep 30 &
                capture_pid=$!
                capture_start=$(preliminary_wait_for_process_start_ticks "$capture_pid")
                sleep 30 &
                loop_pid=$!
                loop_start=$(preliminary_wait_for_process_start_ticks "$loop_pid")
                export SCFUZZBENCH_PRELIMINARY_PID="$loop_pid"
                export SCFUZZBENCH_PRELIMINARY_PID_START_TICKS="$loop_start"
                preliminary_write_active_owner \
                  {shlex.quote(str(root / "preliminary-checkpoints" / "active.pid"))} \
                  "$loop_pid" "$loop_start" "$capture_pid" "$((capture_start + 1))"
                stop_preliminary_snapshots
                if ! preliminary_process_owned "$capture_pid" "$capture_start"; then
                  echo stale-capture-was-signaled
                  exit 1
                fi
                if preliminary_signal_group_if_owned TERM "$capture_pid" "$capture_start"; then
                  echo nonleader-group-was-signaled
                  exit 1
                fi
                if ! preliminary_process_owned "$capture_pid" "$capture_start"; then
                  echo nonleader-process-was-signaled
                  exit 1
                fi
                kill "$capture_pid"
                wait "$capture_pid" 2>/dev/null || true
                echo stale-capture-ignored
                """
            )
        self.assertEqual("stale-capture-ignored", result.stdout.strip())

    def test_mismatched_stop_caller_cannot_remove_live_owner_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner = Path(tmp) / "preliminary-checkpoints" / "active.pid"
            result = run_bash(
                f"""
                export SCFUZZBENCH_ROOT={shlex.quote(tmp)}
                source {shlex.quote(str(COMMON_SH))}
                prepare_workspace
                live_loop=""
                live_loop_start=""
                live_capture=""
                live_capture_start=""
                cleanup_live_owner() {{
                  if [[ "$live_capture" =~ ^[0-9]+$ && "$live_capture_start" =~ ^[0-9]+$ ]]; then
                    preliminary_signal_pid_if_owned \
                      KILL "$live_capture" "$live_capture_start" 2>/dev/null || true
                  fi
                  if [[ "$live_loop" =~ ^[0-9]+$ && "$live_loop_start" =~ ^[0-9]+$ ]]; then
                    preliminary_signal_pid_if_owned \
                      KILL "$live_loop" "$live_loop_start" 2>/dev/null || true
                  fi
                  wait "$live_capture" "$live_loop" 2>/dev/null || true
                }}
                trap cleanup_live_owner EXIT
                sleep 30 &
                live_loop=$!
                live_loop_start=$(preliminary_wait_for_process_start_ticks "$live_loop")
                sleep 30 &
                live_capture=$!
                live_capture_start=$(preliminary_wait_for_process_start_ticks "$live_capture")
                preliminary_write_active_owner \
                  {shlex.quote(str(owner))} \
                  "$live_loop" "$live_loop_start" \
                  "$live_capture" "$live_capture_start"

                sleep 30 &
                stale_caller=$!
                stale_caller_start=$(preliminary_wait_for_process_start_ticks "$stale_caller")
                export SCFUZZBENCH_PRELIMINARY_PID="$stale_caller"
                export SCFUZZBENCH_PRELIMINARY_PID_START_TICKS="$stale_caller_start"
                stop_preliminary_snapshots

                record=$(preliminary_read_active_owner {shlex.quote(str(owner))})
                if [[ "$record" != "$live_loop $live_loop_start $live_capture $live_capture_start" ]]; then
                  echo owner-record-was-removed
                  exit 1
                fi
                if ! preliminary_process_owned "$live_loop" "$live_loop_start" \
                  || ! preliminary_process_owned "$live_capture" "$live_capture_start"; then
                  echo live-owner-was-signaled
                  exit 1
                fi
                echo mismatched-caller-ignored
                """
            )
        self.assertEqual("mismatched-caller-ignored", result.stdout.strip())

    def test_term_resistant_grandchild_cannot_outlive_capture_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_file = root / "grandchild.pid"
            common = shlex.quote(str(COMMON_SH))
            result = run_bash(
                f"""
                export SCFUZZBENCH_ROOT={shlex.quote(tmp)}
                export SCFUZZBENCH_PRELIMINARY_TERM_GRACE_SECONDS=1
                source {common}
                prepare_workspace
                mkdir -p {shlex.quote(str(root / "preliminary-checkpoints"))}
                capture_pid=""
                capture_start=""
                loop_pid=""
                loop_start=""
                grandchild_pid=""
                grandchild_start=""
                cleanup_test_processes() {{
                  if [[ "$capture_pid" =~ ^[0-9]+$ && "$capture_start" =~ ^[0-9]+$ ]]; then
                    preliminary_signal_group_if_owned \
                      KILL "$capture_pid" "$capture_start" 2>/dev/null || true
                    preliminary_signal_pid_if_owned \
                      KILL "$capture_pid" "$capture_start" 2>/dev/null || true
                  fi
                  if [[ "$loop_pid" =~ ^[0-9]+$ && "$loop_start" =~ ^[0-9]+$ ]]; then
                    preliminary_signal_pid_if_owned \
                      KILL "$loop_pid" "$loop_start" 2>/dev/null || true
                  fi
                  if [[ "$grandchild_pid" =~ ^[0-9]+$ && "$grandchild_start" =~ ^[0-9]+$ ]]; then
                    preliminary_signal_pid_if_owned \
                      KILL "$grandchild_pid" "$grandchild_start" 2>/dev/null || true
                  fi
                }}
                trap cleanup_test_processes EXIT
                setsid bash -c '
                  set -euo pipefail
                  source "$1"
                  grandchild_file="$2"
                  capture_preliminary_snapshot() (
                    bash -c '\\''trap "" TERM INT; echo "$BASHPID" >"$1"; exec >/dev/null 2>&1; while :; do sleep 30; done'\\'' \
                      stubborn-grandchild "$grandchild_file" &
                    trap "exit 0" TERM INT
                    wait
                  )
                  preliminary_capture_supervisor 1 1
                ' capture-supervisor {common} {shlex.quote(str(child_file))} \
                  &
                capture_pid=$!
                capture_start=$(preliminary_wait_for_process_start_ticks "$capture_pid")
                for _ in $(seq 1 100); do
                  [[ -s {shlex.quote(str(child_file))} ]] && break
                  sleep 0.02
                done
                grandchild_pid=$(< {shlex.quote(str(child_file))})
                grandchild_start=$(preliminary_wait_for_process_start_ticks "$grandchild_pid")
                sleep 30 &
                loop_pid=$!
                loop_start=$(preliminary_wait_for_process_start_ticks "$loop_pid")
                export SCFUZZBENCH_PRELIMINARY_PID="$loop_pid"
                export SCFUZZBENCH_PRELIMINARY_PID_START_TICKS="$loop_start"
                preliminary_write_active_owner \
                  {shlex.quote(str(root / "preliminary-checkpoints" / "active.pid"))} \
                  "$loop_pid" "$loop_start" "$capture_pid" "$capture_start"
                stop_preliminary_snapshots
                for _ in $(seq 1 100); do
                  if ! preliminary_process_running_owned "$grandchild_pid" "$grandchild_start"; then
                    break
                  fi
                  sleep 0.02
                done
                if preliminary_process_running_owned "$capture_pid" "$capture_start"; then
                  echo supervisor-still-running
                  exit 1
                fi
                if preliminary_process_running_owned "$grandchild_pid" "$grandchild_start"; then
                  echo grandchild-still-running
                  exit 1
                fi
                echo resistant-group-stopped
                """,
                check=False,
            )
        self.assertEqual(
            0,
            result.returncode,
            msg=f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
        )
        self.assertEqual("resistant-group-stopped", result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
