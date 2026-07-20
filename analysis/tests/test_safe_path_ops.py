import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "fuzzers" / "_shared" / "safe_path_ops.py"


class SafePathOperationTests(unittest.TestCase):
    def root_args(self, root: Path, prefix: str = "") -> list[str]:
        option = f"{prefix}-" if prefix else ""
        resolved = root.resolve(strict=True)
        info = resolved.stat()
        return [
            f"--{option}root-path",
            str(root),
            f"--{option}root-anchor",
            str(resolved),
            f"--{option}root-identity",
            f"{info.st_dev}:{info.st_ino}",
        ]

    def run_helper(
        self,
        command: str,
        arguments: list[str],
        *,
        data: bytes | None = None,
        hook: Path | None = None,
        timeout: float = 3,
    ) -> subprocess.CompletedProcess[bytes]:
        env = os.environ.copy()
        if hook is not None:
            env["SCFUZZBENCH_SAFE_PATH_TEST_HOOK"] = str(hook)
        return subprocess.run(
            [sys.executable, str(HELPER), command, *arguments],
            input=data,
            capture_output=True,
            check=False,
            env=env,
            timeout=timeout,
        )

    def write_hook(self, root: Path, body: str) -> Path:
        hook = root / "race-hook.py"
        hook.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            f"{body}\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
        return hook

    def test_atomic_write_and_stable_read_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "nested" / "value.json"
            payload = b'{"stable":true}\n'

            written = self.run_helper(
                "write-file",
                [
                    *self.root_args(root),
                    "--path",
                    str(destination),
                    "--parents",
                ],
                data=payload,
            )
            read = self.run_helper(
                "read-file",
                [*self.root_args(root), "--path", str(destination)],
            )

            self.assertEqual(0, written.returncode, written.stderr)
            self.assertEqual(0, read.returncode, read.stderr)
            self.assertEqual(payload, read.stdout)
            self.assertEqual(0o600, destination.stat().st_mode & 0o777)
            self.assertEqual([], list(destination.parent.glob(".*.scfuzzbench-*")))

    def test_fifo_read_and_regular_move_fail_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fifo = root / "input.fifo"
            os.mkfifo(fifo)
            destination = root / "moved"

            started = time.monotonic()
            read = self.run_helper(
                "read-file",
                [*self.root_args(root), "--path", str(fifo)],
                timeout=1,
            )
            moved = self.run_helper(
                "move",
                [
                    *self.root_args(root, "source"),
                    *self.root_args(root, "destination"),
                    "--source",
                    str(fifo),
                    "--destination",
                    str(destination),
                    "--source-type",
                    "regular",
                ],
                timeout=1,
            )

            self.assertLess(time.monotonic() - started, 1.5)
            self.assertNotEqual(0, read.returncode)
            self.assertNotEqual(0, moved.returncode)
            self.assertIn(b"not a regular file", read.stderr)
            self.assertIn(b"not a regular file", moved.stderr)
            self.assertTrue(fifo.exists())
            self.assertFalse(destination.exists())

    def test_stream_operations_do_not_follow_symlinks_or_block_on_fifo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.log"
            outside.write_text("keep\n", encoding="utf-8")
            linked = root / "linked.log"
            linked.symlink_to(outside)

            append = self.run_helper(
                "append-file",
                [*self.root_args(root), "--path", str(linked)],
                data=b"attacker\n",
            )
            self.assertNotEqual(0, append.returncode)
            self.assertEqual("keep\n", outside.read_text(encoding="utf-8"))

            fifo = root / "metrics.fifo"
            os.mkfifo(fifo)
            started = time.monotonic()
            fifo_result = self.run_helper(
                "append-file",
                [*self.root_args(root), "--path", str(fifo)],
                data=b"sample\n",
                timeout=1,
            )
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertNotEqual(0, fifo_result.returncode)

            streamed = self.run_helper(
                "tee-file",
                [*self.root_args(root), "--path", str(linked)],
                data=b"trusted\n",
            )
            self.assertEqual(0, streamed.returncode, streamed.stderr)
            self.assertEqual(b"trusted\n", streamed.stdout)
            self.assertEqual("trusted\n", linked.read_text(encoding="utf-8"))
            self.assertFalse(linked.is_symlink())
            self.assertEqual("keep\n", outside.read_text(encoding="utf-8"))

    def test_stream_rejects_parent_swap_without_writing_outside(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "logs"
            parent.mkdir()
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "runner.log"
            sentinel.write_text("keep\n", encoding="utf-8")
            hook = self.write_hook(
                root,
                "\n".join(
                    [
                        f"parent = Path({str(parent)!r})",
                        f"saved = Path({str(root / 'logs-saved')!r})",
                        f"outside = Path({str(outside)!r})",
                        "parent.rename(saved)",
                        "parent.symlink_to(outside, target_is_directory=True)",
                    ]
                ),
            )

            result = self.run_helper(
                "tee-file",
                [
                    *self.root_args(root),
                    "--path",
                    str(parent / "runner.log"),
                ],
                data=b"trusted\n",
                hook=hook,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(b"", result.stdout)
            self.assertEqual("keep\n", sentinel.read_text(encoding="utf-8"))

    def test_exec_tee_streams_live_and_preserves_command_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "runner.log"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(HELPER),
                    "exec-tee",
                    *self.root_args(root),
                    "--path",
                    str(destination),
                    "--",
                    sys.executable,
                    "-c",
                    (
                        "import sys,time;"
                        "sys.stdout.write('first\\n');sys.stdout.flush();"
                        "time.sleep(0.5);"
                        "sys.stdout.write('second\\n');sys.stdout.flush();"
                        "raise SystemExit(7)"
                    ),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if process.stdout is None:
                self.fail("helper stdout was not captured")
            readable, _, _ = select.select([process.stdout], [], [], 0.3)
            self.assertTrue(readable, "first log line was buffered until EOF")
            first = process.stdout.readline()
            remainder, stderr = process.communicate(timeout=2)

            self.assertEqual(b"first\n", first)
            self.assertEqual(b"second\n", remainder)
            self.assertEqual(b"", stderr)
            self.assertEqual(7, process.returncode)
            self.assertEqual(
                b"first\nsecond\n",
                destination.read_bytes(),
            )

    def test_exec_tee_forwards_term_and_does_not_orphan_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "runner.log"
            pid_file = root / "child-pids"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(HELPER),
                    "exec-tee",
                    *self.root_args(root),
                    "--path",
                    str(destination),
                    "--",
                    sys.executable,
                    "-c",
                    (
                        "import os,pathlib,subprocess,time;"
                        "child=subprocess.Popen(['sleep','30']);"
                        f"pathlib.Path({str(pid_file)!r}).write_text("
                        "f'{os.getpid()} {child.pid}');"
                        "time.sleep(30)"
                    ),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 3
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pid_file.exists(), "command did not start")
            command_pid, descendant_pid = map(
                int, pid_file.read_text(encoding="utf-8").split()
            )

            process.terminate()
            stdout, stderr = process.communicate(timeout=5)

            def process_is_running(pid: int) -> bool:
                stat_path = Path(f"/proc/{pid}/stat")
                if not stat_path.exists():
                    return False
                try:
                    state = stat_path.read_text(encoding="utf-8").split()[2]
                except (FileNotFoundError, IndexError):
                    return False
                return state not in {"X", "Z"}

            deadline = time.monotonic() + 2
            while (
                process_is_running(command_pid)
                or process_is_running(descendant_pid)
            ) and time.monotonic() < deadline:
                time.sleep(0.02)

            self.assertEqual(b"", stdout)
            self.assertEqual(b"", stderr)
            self.assertEqual(128 + 15, process.returncode)
            self.assertFalse(process_is_running(command_pid))
            self.assertFalse(process_is_running(descendant_pid))

    def test_exec_tee_defers_signal_from_inside_popen_until_child_is_owned(self):
        probe = r"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time
from types import SimpleNamespace
from unittest import mock

helper = Path(sys.argv[1])
root = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("safe_path_ops_probe", helper)
if spec is None or spec.loader is None:
    raise SystemExit("could not load helper")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

resolved = root.resolve(strict=True)
info = resolved.stat()
destination = root / "race.log"
pid_file = root / "race-child.pid"
args = SimpleNamespace(
    root_path=str(root),
    root_anchor=str(resolved),
    root_identity=f"{info.st_dev}:{info.st_ino}",
    path=str(destination),
    parents=False,
    max_bytes=1024,
)
child_code = (
    "import os,pathlib,signal,time;"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
    f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));"
    "time.sleep(6)"
)
real_popen = module.subprocess.Popen

def spawn_then_signal(*popen_args, **popen_kwargs):
    process = real_popen(*popen_args, **popen_kwargs)
    deadline = time.monotonic() + 2
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not pid_file.exists():
        process.kill()
        process.wait()
        raise RuntimeError("race child did not become ready")
    os.kill(os.getpid(), signal.SIGTERM)
    return process

started = time.monotonic()
with mock.patch.object(
    module.subprocess, "Popen", side_effect=spawn_then_signal
):
    status = module._stream_file(
        args,
        append=False,
        copy_stdout=False,
        execute=[sys.executable, "-c", child_code],
    )
elapsed = time.monotonic() - started
child_pid = int(pid_file.read_text())
print(
    json.dumps(
        {
            "status": status,
            "elapsed": elapsed,
            "child_exists": Path(f"/proc/{child_pid}").exists(),
        }
    )
)
"""
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-c", probe, str(HELPER), tmp],
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(128 + signal.SIGTERM, payload["status"])
        self.assertLess(payload["elapsed"], 5.5)
        self.assertFalse(payload["child_exists"])

    def test_exec_tee_child_signal_state_and_parent_state_are_isolated(self):
        probe = r"""
import importlib.util
import json
from pathlib import Path
import signal
import sys
from types import SimpleNamespace

helper = Path(sys.argv[1])
root = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("safe_path_ops_probe", helper)
if spec is None or spec.loader is None:
    raise SystemExit("could not load helper")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

resolved = root.resolve(strict=True)
info = resolved.stat()
destination = root / "signal-state.json"
args = SimpleNamespace(
    root_path=str(root),
    root_anchor=str(resolved),
    root_identity=f"{info.st_dev}:{info.st_ino}",
    path=str(destination),
    parents=False,
    max_bytes=4096,
)
managed = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
child_code = r'''
import json
import signal
managed = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
print(json.dumps({
    "default": (
        signal.getsignal(signal.SIGHUP) == signal.SIG_DFL
        and signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    ),
    "unblocked": all(item not in blocked for item in managed),
}))
'''
original_hup = signal.getsignal(signal.SIGHUP)
original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
try:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    status = module._stream_file(
        args,
        append=False,
        copy_stdout=False,
        execute=[sys.executable, "-c", child_code],
    )
    configured_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    parent_restored = (
        signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
        and signal.SIGTERM in configured_mask
    )
finally:
    signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)
    signal.signal(signal.SIGHUP, original_hup)

payload = json.loads(destination.read_text())
payload.update({"status": status, "parent_restored": parent_restored})
print(json.dumps(payload))
"""
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-c", probe, str(HELPER), tmp],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(0, payload["status"])
        self.assertTrue(payload["default"])
        self.assertTrue(payload["unblocked"])
        self.assertTrue(payload["parent_restored"])

    def test_exec_tee_post_wait_signal_is_caught_and_restores_parent_state(self):
        probe = r"""
import importlib.util
import inspect
import json
import os
from pathlib import Path
import signal
import sys
from types import SimpleNamespace

helper = Path(sys.argv[1])
root = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("safe_path_ops_probe", helper)
if spec is None or spec.loader is None:
    raise SystemExit("could not load helper")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

resolved = root.resolve(strict=True)
info = resolved.stat()
destination = root / "post-wait.log"
args = SimpleNamespace(
    root_path=str(root),
    root_anchor=str(resolved),
    root_identity=f"{info.st_dev}:{info.st_ino}",
    path=str(destination),
    parents=False,
    max_bytes=4096,
)
source, first_line = inspect.getsourcelines(module._stream_file)
marker_index = next(
    index
    for index, line in enumerate(source)
    if "SCFUZZBENCH_POST_WAIT_SIGNAL_BARRIER" in line
)
barrier_line = first_line + marker_index + 1
triggered = {"value": False}

def trace_barrier(frame, event, _arg):
    if (
        event == "line"
        and frame.f_code is module._stream_file.__code__
        and frame.f_lineno == barrier_line
    ):
        triggered["value"] = True
        sys.settrace(None)
        os.kill(os.getpid(), signal.SIGTERM)
    return trace_barrier

original_hup = signal.getsignal(signal.SIGHUP)
original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
try:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    sys.settrace(trace_barrier)
    status = module._stream_file(
        args,
        append=False,
        copy_stdout=False,
        execute=[sys.executable, "-c", "print('complete')"],
    )
    sys.settrace(None)
    configured_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    parent_restored = (
        signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
        and signal.SIGTERM in configured_mask
    )
finally:
    sys.settrace(None)
    signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)
    signal.signal(signal.SIGHUP, original_hup)

print(
    json.dumps(
        {
            "status": status,
            "triggered": triggered["value"],
            "parent_restored": parent_restored,
            "output": destination.read_text(),
        }
    )
)
"""
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-c", probe, str(HELPER), tmp],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(128 + signal.SIGTERM, payload["status"])
        self.assertTrue(payload["triggered"])
        self.assertTrue(payload["parent_restored"])
        self.assertEqual("complete\n", payload["output"])

    def test_move_rejects_parent_swap_to_outside_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_parent = root / "source"
            source_parent.mkdir()
            source = source_parent / "staged"
            source.mkdir()
            destination = root / "destination"
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel"
            sentinel.write_text("keep", encoding="utf-8")
            hook = self.write_hook(
                root,
                "\n".join(
                    [
                        f"source = Path({str(source_parent)!r})",
                        f"saved = Path({str(root / 'source-saved')!r})",
                        f"outside = Path({str(outside)!r})",
                        "source.rename(saved)",
                        "source.symlink_to(outside, target_is_directory=True)",
                    ]
                ),
            )

            result = self.run_helper(
                "move",
                [
                    *self.root_args(root, "source"),
                    *self.root_args(root, "destination"),
                    "--source",
                    str(source),
                    "--destination",
                    str(destination),
                    "--source-type",
                    "directory",
                ],
                hook=hook,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn(b"safe path operation failed", result.stderr)
            self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))
            self.assertFalse(destination.exists())

    def test_remove_rejects_parent_swap_without_touching_outside(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "parent"
            victim = parent / "victim"
            victim.mkdir(parents=True)
            outside = root / "outside"
            outside_victim = outside / "victim"
            outside_victim.mkdir(parents=True)
            sentinel = outside_victim / "sentinel"
            sentinel.write_text("keep", encoding="utf-8")
            hook = self.write_hook(
                root,
                "\n".join(
                    [
                        f"parent = Path({str(parent)!r})",
                        f"saved = Path({str(root / 'parent-saved')!r})",
                        f"outside = Path({str(outside)!r})",
                        "parent.rename(saved)",
                        "parent.symlink_to(outside, target_is_directory=True)",
                    ]
                ),
            )

            result = self.run_helper(
                "remove-tree",
                [*self.root_args(root), "--path", str(victim)],
                hook=hook,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn(b"safe path operation failed", result.stderr)
            self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))
            self.assertTrue((root / "parent-saved" / "victim").is_dir())

    def test_remove_missing_parent_is_noop_but_symlink_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_parent = root / "corpus"

            for command in ("remove-file", "remove-tree"):
                with self.subTest(command=command):
                    result = self.run_helper(
                        command,
                        [
                            *self.root_args(root),
                            "--path",
                            str(missing_parent / "echidna" / "stale"),
                        ],
                    )
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual(b"", result.stderr)
                    self.assertFalse(missing_parent.exists())

            outside = root / "outside"
            outside_victim = outside / "echidna" / "stale"
            outside_victim.mkdir(parents=True)
            sentinel = outside_victim / "sentinel"
            sentinel.write_text("keep", encoding="utf-8")
            missing_parent.symlink_to(outside, target_is_directory=True)

            rejected = self.run_helper(
                "remove-tree",
                [
                    *self.root_args(root),
                    "--path",
                    str(missing_parent / "echidna" / "stale"),
                ],
            )

            self.assertNotEqual(0, rejected.returncode)
            self.assertIn(b"safe path operation failed", rejected.stderr)
            self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))

    def test_read_rejects_name_swap_and_emits_no_unverified_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "artifact.log"
            source.write_text("trusted\n", encoding="utf-8")
            outside = root / "outside.log"
            outside.write_text("secret\n", encoding="utf-8")
            hook = self.write_hook(
                root,
                "\n".join(
                    [
                        f"source = Path({str(source)!r})",
                        f"saved = Path({str(root / 'artifact-saved.log')!r})",
                        f"outside = Path({str(outside)!r})",
                        "source.rename(saved)",
                        "source.symlink_to(outside)",
                    ]
                ),
            )

            result = self.run_helper(
                "read-file",
                [*self.root_args(root), "--path", str(source)],
                hook=hook,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(b"", result.stdout)
            self.assertIn(b"file changed", result.stderr)
            self.assertEqual("secret\n", outside.read_text(encoding="utf-8"))

    def test_archive_is_compressed_and_contains_the_pinned_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "logs"
            source.mkdir()
            payload = b"A" * (256 * 1024)
            runner_log = source / "runner.log"
            runner_log.write_bytes(payload)
            os.utime(runner_log, (7258118400, 7258118400))
            destination = root / "output" / "logs.zip"
            source_identity = self.root_args(source)[-1]

            result = self.run_helper(
                "archive",
                [
                    *self.root_args(source, "source"),
                    *self.root_args(root, "destination"),
                    "--source",
                    str(source),
                    "--source-identity",
                    source_identity,
                    "--destination",
                    str(destination),
                    "--parents",
                ],
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertLess(destination.stat().st_size, len(payload) // 10)
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(payload, archive.read("logs/runner.log"))
                self.assertEqual(
                    zipfile.ZIP_DEFLATED,
                    archive.getinfo("logs/runner.log").compress_type,
                )
                self.assertEqual(
                    2107, archive.getinfo("logs/runner.log").date_time[0]
                )

    def test_archive_rejects_symlinks_hardlinks_and_special_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "corpus"
            source.mkdir()
            regular = source / "seed"
            regular.write_text("seed", encoding="utf-8")
            destination = root / "corpus.zip"
            source_identity = self.root_args(source)[-1]
            base_arguments = [
                *self.root_args(source, "source"),
                *self.root_args(root, "destination"),
                "--source",
                str(source),
                "--source-identity",
                source_identity,
                "--destination",
                str(destination),
            ]

            linked = source / "linked"
            linked.symlink_to(regular)
            symlink_result = self.run_helper("archive", base_arguments)
            linked.unlink()

            hardlink = source / "hardlink"
            os.link(regular, hardlink)
            hardlink_result = self.run_helper("archive", base_arguments)
            hardlink.unlink()
            regular.unlink()

            fifo = source / "fifo"
            os.mkfifo(fifo)
            fifo_result = self.run_helper("archive", base_arguments)

            self.assertNotEqual(0, symlink_result.returncode)
            self.assertIn(b"is a symlink", symlink_result.stderr)
            self.assertNotEqual(0, hardlink_result.returncode)
            self.assertIn(b"multiple hard links", hardlink_result.stderr)
            self.assertNotEqual(0, fifo_result.returncode)
            self.assertIn(b"not a regular file", fifo_result.stderr)
            self.assertFalse(destination.exists())

    def test_archive_destination_insertion_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "logs"
            source.mkdir()
            (source / "runner.log").write_text("log\n", encoding="utf-8")
            destination = root / "snapshot.zip"
            hook = self.write_hook(
                root,
                f"Path({str(destination)!r}).write_bytes(b'attacker')",
            )

            result = self.run_helper(
                "archive",
                [
                    *self.root_args(source, "source"),
                    *self.root_args(root, "destination"),
                    "--source",
                    str(source),
                    "--source-identity",
                    self.root_args(source)[-1],
                    "--destination",
                    str(destination),
                ],
                hook=hook,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(b"attacker", destination.read_bytes())
            self.assertEqual([], list(root.glob(".*.scfuzzbench-*")))

    def test_archive_bounds_empty_directory_depth_and_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "logs"
            source.mkdir()
            current = source
            for _ in range(140):
                current /= "d"
                current.mkdir()
            destination = root / "deep.zip"
            arguments = [
                *self.root_args(source, "source"),
                *self.root_args(root, "destination"),
                "--source",
                str(source),
                "--source-identity",
                self.root_args(source)[-1],
                "--destination",
                str(destination),
            ]

            deep = self.run_helper("archive", arguments)

            self.assertNotEqual(0, deep.returncode)
            self.assertIn(b"nesting exceeds", deep.stderr)
            self.assertNotIn(b"Traceback", deep.stderr)
            self.assertFalse(destination.exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "logs"
            source.mkdir()
            for index in range(11):
                (source / f"d{index}").mkdir()
            destination = root / "wide.zip"

            wide = self.run_helper(
                "archive",
                [
                    *self.root_args(source, "source"),
                    *self.root_args(root, "destination"),
                    "--source",
                    str(source),
                    "--source-identity",
                    self.root_args(source)[-1],
                    "--destination",
                    str(destination),
                    "--max-entries",
                    "10",
                ],
            )

            self.assertNotEqual(0, wide.returncode)
            self.assertIn(b"more than 10 entries", wide.stderr)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
