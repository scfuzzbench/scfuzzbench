import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
COMMON_SH = REPO_ROOT / "fuzzers" / "_shared" / "common.sh"


def run_bash(
    script: str,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 10,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, **(env or {})},
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


class RunnerFilesystemSafetyTests(unittest.TestCase):
    def base_script(self, root: Path) -> str:
        return (
            "unset SCFUZZBENCH_LOCAL_MODE\n"
            "unset SCFUZZBENCH_DISABLE_IMDS_CREDENTIAL_CACHE\n"
            f"export SCFUZZBENCH_ROOT={shlex.quote(str(root))}\n"
            f"export SCFUZZBENCH_WORKDIR={shlex.quote(str(root / 'work'))}\n"
            f"export SCFUZZBENCH_LOG_DIR={shlex.quote(str(root / 'logs'))}\n"
            f"source {shlex.quote(str(COMMON_SH))}\n"
            "prepare_workspace\n"
            "wait_for_sleep_child() {\n"
            "  local parent=$1 candidate\n"
            "  local attempt\n"
            "  for ((attempt = 0; attempt < 300; attempt++)); do\n"
            "    if [[ -r /proc/${parent}/task/${parent}/children ]]; then\n"
            "      for candidate in $(</proc/${parent}/task/${parent}/children); do\n"
            "        if [[ -r /proc/${candidate}/comm ]] &&\n"
            "          [[ $(</proc/${candidate}/comm) == sleep ]]; then\n"
            "          printf '%s\\n' \"${candidate}\"\n"
            "          return 0\n"
            "        fi\n"
            "      done\n"
            "    fi\n"
            "    sleep 0.01\n"
            "  done\n"
            "  return 1\n"
            "}\n"
        )

    def test_cached_credentials_are_strict_json_and_never_sourced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside-creds"
            marker = root / "executed"
            outside.write_text(
                f"touch {shlex.quote(str(marker))}\n",
                encoding="utf-8",
            )
            result = run_bash(
                self.base_script(root)
                + f"""
                ln -s {shlex.quote(str(outside))} "$SCFUZZBENCH_ROOT/aws_creds.json"
                export AWS_ACCESS_KEY_ID=old-ak
                export AWS_SECRET_ACCESS_KEY=old-sk
                export AWS_SESSION_TOKEN=old-st
                if load_cached_aws_creds; then
                  echo unsafe-cache-accepted
                  exit 1
                fi
                printf '%s %s %s\n' \
                  "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" "$AWS_SESSION_TOKEN"
                """
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("old-ak old-sk old-st", result.stdout.strip())
            self.assertFalse(marker.exists())
            self.assertEqual(
                f"touch {shlex.quote(str(marker))}\n",
                outside.read_text(encoding="utf-8"),
            )

    def test_manifest_boto_helper_gets_fresh_scoped_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "captured"
            helper = root / "manifest-helper.py"
            helper.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, sys\n"
                f"path = pathlib.Path({str(capture)!r})\n"
                "path.write_text(' '.join([\n"
                " os.environ.get('AWS_ACCESS_KEY_ID',''),\n"
                " os.environ.get('AWS_SECRET_ACCESS_KEY',''),\n"
                " os.environ.get('AWS_SESSION_TOKEN',''),\n"
                " os.environ.get('AWS_EC2_METADATA_DISABLED',''),\n"
                "]) + '\\n' + sys.stdin.read())\n",
                encoding="utf-8",
            )
            helper.chmod(0o755)
            expiration_epoch = int(time.time()) + 3600
            cache = {
                "version": 1,
                "access_key_id": "fresh-ak",
                "secret_access_key": "fresh-sk",
                "session_token": "fresh-st",
                "expiration": "2099-01-01T00:00:00Z",
                "expiration_epoch": expiration_epoch,
            }
            manifest = root / "manifest.json"
            result = run_bash(
                self.base_script(root)
                + f"""
                printf '%s\n' {shlex.quote(json.dumps(cache))} |
                  write_strict_descendant_file \
                    "$SCFUZZBENCH_ROOT/aws_creds.json" "$SCFUZZBENCH_ROOT" \
                    "$SCFUZZBENCH_FRAMEWORK_ROOT_ANCHOR" \
                    "$SCFUZZBENCH_FRAMEWORK_ROOT_IDENTITY" 1
                printf '%s\n' '{{"run":"one"}}' |
                  write_strict_descendant_file \
                    {shlex.quote(str(manifest))} "$SCFUZZBENCH_ROOT" \
                    "$SCFUZZBENCH_FRAMEWORK_ROOT_ANCHOR" \
                    "$SCFUZZBENCH_FRAMEWORK_ROOT_IDENTITY" 1
                export SCFUZZBENCH_MANIFEST_OBJECT_HELPER={shlex.quote(str(helper))}
                export AWS_ACCESS_KEY_ID=outer-ak
                export AWS_SECRET_ACCESS_KEY=outer-sk
                export AWS_SESSION_TOKEN=outer-st
                upload_manifest_once_or_verify \
                  {shlex.quote(str(manifest))} bucket manifest.json
                printf '%s %s %s\n' \
                  "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" "$AWS_SESSION_TOKEN"
                """
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("outer-ak outer-sk outer-st", result.stdout.strip())
            captured = capture.read_text(encoding="utf-8")
            self.assertTrue(
                captured.startswith("fresh-ak fresh-sk fresh-st true\n"),
                captured,
            )
            self.assertIn('{"run":"one"}', captured)

    def test_near_expiry_cache_leaves_existing_environment_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = {
                "version": 1,
                "access_key_id": "near-ak",
                "secret_access_key": "near-sk",
                "session_token": "near-st",
                "expiration": "soon",
                "expiration_epoch": int(time.time()) + 30,
            }
            result = run_bash(
                self.base_script(root)
                + f"""
                printf '%s\n' {shlex.quote(json.dumps(cache))} |
                  write_strict_descendant_file \
                    "$SCFUZZBENCH_ROOT/aws_creds.json" "$SCFUZZBENCH_ROOT" \
                    "$SCFUZZBENCH_FRAMEWORK_ROOT_ANCHOR" \
                    "$SCFUZZBENCH_FRAMEWORK_ROOT_IDENTITY" 1
                export AWS_ACCESS_KEY_ID=old-ak
                export AWS_SECRET_ACCESS_KEY=old-sk
                export AWS_SESSION_TOKEN=old-st
                if load_cached_aws_creds; then
                  echo near-expiry-cache-accepted
                  exit 1
                fi
                printf '%s %s %s\n' \
                  "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" "$AWS_SESSION_TOKEN"
                """
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("old-ak old-sk old-st", result.stdout.strip())

    def test_heartbeat_uses_json_encoder_pinned_files_and_pid_start_ticks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captured = root / "captured-heartbeat.json"
            outside_pid = root / "outside-pid"
            outside_json = root / "outside-json"
            outside_pid.write_text("keep-pid", encoding="utf-8")
            outside_json.write_text("keep-json", encoding="utf-8")
            result = run_bash(
                self.base_script(root)
                + f"""
                ln -s {shlex.quote(str(outside_pid))} \
                  "$SCFUZZBENCH_ROOT/run-heartbeat.pid"
                ln -s {shlex.quote(str(outside_json))} \
                  "$SCFUZZBENCH_ROOT/run-heartbeat.json"
                export SCFUZZBENCH_S3_BUCKET=bucket
                export SCFUZZBENCH_RUN_ID='run-"quoted'
                export SCFUZZBENCH_BENCHMARK_UUID={'a' * 32}
                export SCFUZZBENCH_INSTANCE_ID='instance-one'
                upload_pinned_s3_file() {{
                  read_strict_descendant_file \
                    "$1" "$2" "$3" "$4" 1048576 \
                    > {shlex.quote(str(captured))}
                }}
                start_run_heartbeat
                for _ in $(seq 1 200); do
                  [[ -s {shlex.quote(str(captured))} ]] && break
                  sleep 0.01
                done
                owner=$(read_strict_descendant_file \
                  "$SCFUZZBENCH_ROOT/run-heartbeat.pid" "$SCFUZZBENCH_ROOT" \
                  "$SCFUZZBENCH_FRAMEWORK_ROOT_ANCHOR" \
                  "$SCFUZZBENCH_FRAMEWORK_ROOT_IDENTITY" 64)
                read -r heartbeat_pid heartbeat_start extra <<<"$owner"
                [[ -z "$extra" && "$heartbeat_pid" =~ ^[0-9]+$ &&
                   "$heartbeat_start" =~ ^[0-9]+$ ]]
                preliminary_signal_pid_if_owned \
                  TERM "$heartbeat_pid" "$heartbeat_start"
                wait "$heartbeat_pid" 2>/dev/null || true
                """
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(captured.read_text(encoding="utf-8"))
            self.assertEqual('run-"quoted', payload["run_id"])
            self.assertEqual("instance-one", payload["instance_id"])
            self.assertIsInstance(payload["observed_at_epoch"], int)
            self.assertEqual("keep-pid", outside_pid.read_text(encoding="utf-8"))
            self.assertEqual("keep-json", outside_json.read_text(encoding="utf-8"))

    def test_stale_heartbeat_owner_does_not_suppress_new_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captured = root / "captured-heartbeat.json"
            result = run_bash(
                self.base_script(root)
                + f"""
                sleep 30 &
                unrelated=$!
                unrelated_start=$(preliminary_wait_for_process_start_ticks "$unrelated")
                printf '%s %s\n' "$unrelated" "$((unrelated_start + 1))" |
                  write_strict_descendant_file \
                    "$SCFUZZBENCH_ROOT/run-heartbeat.pid" "$SCFUZZBENCH_ROOT" \
                    "$SCFUZZBENCH_FRAMEWORK_ROOT_ANCHOR" \
                    "$SCFUZZBENCH_FRAMEWORK_ROOT_IDENTITY" 1
                export SCFUZZBENCH_S3_BUCKET=bucket
                export SCFUZZBENCH_RUN_ID=run-one
                export SCFUZZBENCH_BENCHMARK_UUID={'b' * 32}
                export SCFUZZBENCH_INSTANCE_ID=instance-one
                upload_pinned_s3_file() {{
                  read_strict_descendant_file \
                    "$1" "$2" "$3" "$4" 1048576 \
                    > {shlex.quote(str(captured))}
                }}
                start_run_heartbeat
                owner=$(read_strict_descendant_file \
                  "$SCFUZZBENCH_ROOT/run-heartbeat.pid" "$SCFUZZBENCH_ROOT" \
                  "$SCFUZZBENCH_FRAMEWORK_ROOT_ANCHOR" \
                  "$SCFUZZBENCH_FRAMEWORK_ROOT_IDENTITY" 64)
                read -r heartbeat_pid heartbeat_start extra <<<"$owner"
                [[ "$heartbeat_pid" != "$unrelated" ]]
                preliminary_process_owned "$unrelated" "$unrelated_start"
                preliminary_signal_pid_if_owned \
                  TERM "$heartbeat_pid" "$heartbeat_start"
                wait "$heartbeat_pid" 2>/dev/null || true
                kill "$unrelated"
                wait "$unrelated" 2>/dev/null || true
                """
            )

            self.assertEqual(0, result.returncode, result.stderr)

    def test_runner_metrics_replaces_stale_owner_and_reaps_sleep_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_bash(
                self.base_script(root)
                + """
                unrelated=""
                cleanup() {
                  stop_runner_metrics || true
                  if [[ "${unrelated}" =~ ^[0-9]+$ ]]; then
                    kill "${unrelated}" 2>/dev/null || true
                    wait "${unrelated}" 2>/dev/null || true
                  fi
                }
                trap cleanup EXIT

                sleep 30 >/dev/null 2>&1 &
                unrelated=$!
                unrelated_start=$(
                  preliminary_wait_for_process_start_ticks "${unrelated}"
                )
                export SCFUZZBENCH_RUNNER_METRICS_PID="${unrelated}"
                export SCFUZZBENCH_RUNNER_METRICS_PID_START_TICKS="$((unrelated_start + 1))"
                export SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS=60

                start_runner_metrics
                metrics_pid="${SCFUZZBENCH_RUNNER_METRICS_PID}"
                metrics_start="${SCFUZZBENCH_RUNNER_METRICS_PID_START_TICKS}"
                [[ "${metrics_pid}" != "${unrelated}" ]]
                metrics_sleep_pid=$(wait_for_sleep_child "${metrics_pid}")
                metrics_sleep_start=$(
                  preliminary_wait_for_process_start_ticks "${metrics_sleep_pid}"
                )

                stop_runner_metrics
                if preliminary_process_running_owned \
                  "${metrics_pid}" "${metrics_start}"; then
                  echo "metrics loop survived stop" >&2
                  exit 1
                fi
                if preliminary_process_running_owned \
                  "${metrics_sleep_pid}" "${metrics_sleep_start}"; then
                  echo "metrics sleep child survived stop" >&2
                  exit 1
                fi
                preliminary_process_running_owned \
                  "${unrelated}" "${unrelated_start}"

                kill "${unrelated}"
                wait "${unrelated}" 2>/dev/null || true
                unrelated=""
                trap - EXIT
                """
            )

            self.assertEqual(0, result.returncode, result.stderr)

    def test_credential_refresher_replaces_stale_owner_and_reaps_sleep_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_bash(
                self.base_script(root)
                + """
                unrelated=""
                cleanup() {
                  stop_aws_creds_refresher || true
                  if [[ "${unrelated}" =~ ^[0-9]+$ ]]; then
                    kill "${unrelated}" 2>/dev/null || true
                    wait "${unrelated}" 2>/dev/null || true
                  fi
                }
                trap cleanup EXIT
                cache_aws_creds_from_imds() { :; }

                sleep 30 >/dev/null 2>&1 &
                unrelated=$!
                unrelated_start=$(
                  preliminary_wait_for_process_start_ticks "${unrelated}"
                )
                export SCFUZZBENCH_AWS_CREDS_REFRESH_PID="${unrelated}"
                export SCFUZZBENCH_AWS_CREDS_REFRESH_PID_START_TICKS="$((unrelated_start + 1))"
                export SCFUZZBENCH_AWS_CREDS_REFRESH_SECONDS=60

                start_aws_creds_refresher
                refresh_pid="${SCFUZZBENCH_AWS_CREDS_REFRESH_PID}"
                refresh_start="${SCFUZZBENCH_AWS_CREDS_REFRESH_PID_START_TICKS}"
                [[ "${refresh_pid}" != "${unrelated}" ]]
                refresh_sleep_pid=$(wait_for_sleep_child "${refresh_pid}")
                refresh_sleep_start=$(
                  preliminary_wait_for_process_start_ticks "${refresh_sleep_pid}"
                )

                stop_aws_creds_refresher
                if preliminary_process_running_owned \
                  "${refresh_pid}" "${refresh_start}"; then
                  echo "credential refresh loop survived stop" >&2
                  exit 1
                fi
                if preliminary_process_running_owned \
                  "${refresh_sleep_pid}" "${refresh_sleep_start}"; then
                  echo "credential refresh sleep child survived stop" >&2
                  exit 1
                fi
                preliminary_process_running_owned \
                  "${unrelated}" "${unrelated_start}"

                kill "${unrelated}"
                wait "${unrelated}" 2>/dev/null || true
                unrelated=""
                trap - EXIT
                """
            )

            self.assertEqual(0, result.returncode, result.stderr)

    def test_finalize_keeps_credential_refresher_through_final_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            order = root / "finalize-order"
            result = run_bash(
                self.base_script(root)
                + f"""
                stop_preliminary_snapshots() {{
                  echo preliminary >> {shlex.quote(str(order))}
                }}
                stop_runner_metrics() {{
                  echo metrics >> {shlex.quote(str(order))}
                }}
                is_local_mode() {{ return 1; }}
                upload_results() {{
                  echo upload >> {shlex.quote(str(order))}
                }}
                stop_aws_creds_refresher() {{
                  echo credentials >> {shlex.quote(str(order))}
                }}
                shutdown_instance() {{
                  echo shutdown >> {shlex.quote(str(order))}
                }}
                export SCFUZZBENCH_S3_BUCKET=bucket
                export SCFUZZBENCH_RUN_ID=run-one
                export SCFUZZBENCH_FUZZER_LABEL=foundry
                unset SCFUZZBENCH_UPLOAD_DONE

                true
                finalize_run
                finalize_status=$?
                [[ "${{finalize_status}}" -eq 0 ]]
                """
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                [
                    "preliminary",
                    "metrics",
                    "upload",
                    "credentials",
                    "shutdown",
                ],
                order.read_text(encoding="utf-8").splitlines(),
            )

    def test_runner_log_writes_replace_symlinks_without_touching_outside(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside_command = root / "outside-command"
            outside_metrics = root / "outside-metrics"
            outside_timeout = root / "outside-timeout"
            for path in (outside_command, outside_metrics, outside_timeout):
                path.write_text("keep\n", encoding="utf-8")
            result = run_bash(
                self.base_script(root)
                + f"""
                ln -s {shlex.quote(str(outside_command))} \
                  "$SCFUZZBENCH_LOG_DIR/runner_commands.log"
                ln -s {shlex.quote(str(outside_metrics))} \
                  "$SCFUZZBENCH_LOG_DIR/runner_metrics.csv"
                ln -s {shlex.quote(str(outside_timeout))} \
                  "$SCFUZZBENCH_LOG_DIR/fuzzer.log"
                append_runner_command_log 2 1 printf trusted
                export SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS=1
                start_runner_metrics
                sleep 0.1
                stop_runner_metrics
                start_preliminary_snapshots() {{ :; }}
                stop_preliminary_snapshots() {{ :; }}
                export SCFUZZBENCH_TIMEOUT_SECONDS=2
                run_with_timeout "$SCFUZZBENCH_LOG_DIR/fuzzer.log" \
                  bash -c 'printf "trusted output\\n"'
                """
            )

            self.assertEqual(0, result.returncode, result.stderr)
            for path in (outside_command, outside_metrics, outside_timeout):
                self.assertEqual("keep\n", path.read_text(encoding="utf-8"))
            self.assertIn(
                "trusted output",
                (root / "logs" / "fuzzer.log").read_text(encoding="utf-8"),
            )
            self.assertFalse((root / "logs" / "fuzzer.log").is_symlink())
            self.assertFalse((root / "logs" / "runner_metrics.csv").is_symlink())


if __name__ == "__main__":
    unittest.main()
