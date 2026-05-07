import importlib.util
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


def load_generate_cost_transparency():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "generate_cost_transparency.py"
    spec = importlib.util.spec_from_file_location("generate_cost_transparency", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load spec for {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class GenerateCostTransparencyTests(unittest.TestCase):
    def test_normalize_service_groups_keeps_negative_nonzero_costs(self):
        module = load_generate_cost_transparency()
        groups = [
            {"Keys": ["Amazon EC2"], "Metrics": {module.METRIC: {"Amount": "120.00"}}},
            {"Keys": ["Credit"], "Metrics": {module.METRIC: {"Amount": "-20.00"}}},
            {"Keys": ["Unused"], "Metrics": {module.METRIC: {"Amount": "0.00"}}},
        ]

        normalized = module.normalize_service_groups(groups, total=Decimal("100.00"))

        self.assertEqual(["Amazon EC2", "Credit"], [item["service"] for item in normalized])
        self.assertEqual([120.0, -20.0], [item["cost_usd"] for item in normalized])
        self.assertEqual([120.0, -20.0], [item["share_of_total_pct"] for item in normalized])

    def test_make_unavailable_payload_keeps_public_schema(self):
        module = load_generate_cost_transparency()

        payload = module.make_unavailable_payload(
            "2026-05-07T00:00:00+00:00",
            "boom",
            history_months=7,
        )

        self.assertFalse(payload["available"])
        self.assertEqual(module.COST_EXPLORER_REGION, payload["cost_explorer_region"])
        self.assertEqual(7, payload["history_months"])
        self.assertEqual(module.MAX_STACKED_SERVICES, payload["max_stacked_services"])
        self.assertEqual([], payload["history"]["months"])
        self.assertEqual([], payload["current_month"]["daily"])

    def test_main_fails_when_lookup_breaks_by_default(self):
        module = load_generate_cost_transparency()

        with mock.patch.object(module, "build_payload", side_effect=RuntimeError("boom")):
            with mock.patch.object(module, "write_json") as write_json:
                with mock.patch.object(sys, "argv", ["generate_cost_transparency.py"]):
                    exit_code = module.main()

        self.assertEqual(1, exit_code)
        write_json.assert_not_called()

    def test_main_can_emit_unavailable_payload_when_allowed(self):
        module = load_generate_cost_transparency()

        with mock.patch.object(module, "build_payload", side_effect=RuntimeError("boom")):
            with mock.patch.object(module, "write_json") as write_json:
                with mock.patch.object(sys, "argv", ["generate_cost_transparency.py", "--allow-unavailable"]):
                    exit_code = module.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(2, write_json.call_count)
        payload = write_json.call_args_list[0].args[1]
        self.assertFalse(payload["available"])
        self.assertIn("cost_explorer_region", payload)
        self.assertIn("history_months", payload)
        self.assertIn("max_stacked_services", payload)


if __name__ == "__main__":
    unittest.main()
