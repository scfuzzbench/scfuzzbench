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


if __name__ == "__main__":
    unittest.main()
