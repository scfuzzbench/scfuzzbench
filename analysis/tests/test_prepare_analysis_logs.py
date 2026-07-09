import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prepare_analysis_logs.py"


class PrepareAnalysisLogsTests(unittest.TestCase):
    def test_keeps_runner_metrics_basename_when_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            unzipped_dir = tmp_dir / "unzipped" / "i-aaa-foundry"
            (unzipped_dir / "logs").mkdir(parents=True, exist_ok=True)
            (unzipped_dir / "logs" / "fuzzer.log").write_text("hello\n", encoding="utf-8")
            (unzipped_dir / "logs" / "runner_metrics.csv").write_text(
                "timestamp,uptime_seconds,cpu_user_pct,cpu_system_pct,cpu_iowait_pct,mem_total_kb,mem_used_kb\n"
                "2026-02-23T00:00:00+00:00,1,1,1,1,1000,500\n",
                encoding="utf-8",
            )

            out_dir = tmp_dir / "prepared"
            subprocess.check_call(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--unzipped-dir",
                    str(tmp_dir / "unzipped"),
                    "--out-dir",
                    str(out_dir),
                ]
            )

            prepared_instance = out_dir / "i-aaa-foundry"
            self.assertTrue((prepared_instance / "fuzzer.log").exists())
            self.assertTrue((prepared_instance / "runner_metrics.csv").exists())
            self.assertFalse((prepared_instance / "logs__runner_metrics.csv").exists())

    def test_copies_showmap_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            showmap_dir = (
                tmp_dir
                / "unzipped"
                / "i-aaa-foundry-master"
                / "logs"
                / "showmap"
                / "foundry-master__Suite__invariant_ok"
            )
            showmap_dir.mkdir(parents=True, exist_ok=True)
            (showmap_dir / "trial-1.txt").write_text("edge_a:1\n", encoding="utf-8")

            out_dir = tmp_dir / "prepared"
            subprocess.check_call(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--unzipped-dir",
                    str(tmp_dir / "unzipped"),
                    "--out-dir",
                    str(out_dir),
                ]
            )

            prepared_showmap = (
                out_dir
                / "i-aaa-foundry-master"
                / "showmap"
                / "foundry-master__Suite__invariant_ok"
                / "trial-1.txt"
            )
            self.assertEqual(prepared_showmap.read_text(encoding="utf-8"), "edge_a:1\n")

    def test_disambiguates_colliding_showmap_trial_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            showmap_a = (
                tmp_dir
                / "unzipped"
                / "i-aaa-foundry-master"
                / "logs"
                / "a"
                / "showmap"
                / "foundry-master__Suite__invariant_ok"
            )
            showmap_b = (
                tmp_dir
                / "unzipped"
                / "i-aaa-foundry-master"
                / "logs"
                / "b"
                / "showmap"
                / "foundry-master__Suite__invariant_ok"
            )
            showmap_a.mkdir(parents=True, exist_ok=True)
            showmap_b.mkdir(parents=True, exist_ok=True)
            (showmap_a / "trial-1.txt").write_text("edge_a:1\n", encoding="utf-8")
            (showmap_b / "trial-1.txt").write_text("edge_b:1\n", encoding="utf-8")

            out_dir = tmp_dir / "prepared"
            cmd = [
                sys.executable,
                str(SCRIPT),
                "--unzipped-dir",
                str(tmp_dir / "unzipped"),
                "--out-dir",
                str(out_dir),
            ]
            subprocess.check_call(cmd)
            subprocess.check_call(cmd)

            prepared_showmap_dir = (
                out_dir
                / "i-aaa-foundry-master"
                / "showmap"
                / "foundry-master__Suite__invariant_ok"
            )
            files = sorted(prepared_showmap_dir.glob("*.txt"))
            self.assertEqual(len(files), 2)
            self.assertEqual(
                sorted(path.read_text(encoding="utf-8") for path in files),
                ["edge_a:1\n", "edge_b:1\n"],
            )


if __name__ == "__main__":
    unittest.main()
