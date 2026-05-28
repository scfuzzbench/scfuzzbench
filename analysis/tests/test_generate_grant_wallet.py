import importlib.util
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


def load_generate_grant_wallet():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "generate_grant_wallet.py"
    spec = importlib.util.spec_from_file_location("generate_grant_wallet", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load spec for {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class GenerateGrantWalletTests(unittest.TestCase):
    def test_portfolio_from_zerion_reads_total_and_distributions(self):
        module = load_generate_grant_wallet()

        payload = module.portfolio_from_zerion(
            {
                "data": {
                    "attributes": {
                        "positions_distribution_by_type": {"wallet": 12.5, "staked": 1.25},
                        "positions_distribution_by_chain": {"ethereum": 10, "base": 3.75},
                        "total": {"positions": 13.75},
                        "changes": {"absolute_1d": 0.5, "percent_1d": 3.75},
                    }
                }
            },
            "2026-05-28T00:00:00+00:00",
        )

        self.assertTrue(payload["available"])
        self.assertEqual("zerion", payload["source"])
        self.assertEqual(13.75, payload["portfolio"]["total_usd"])
        self.assertEqual(0.5, payload["portfolio"]["absolute_1d_usd"])
        self.assertEqual({"ethereum": 10.0, "base": 3.75}, payload["portfolio"]["positions_distribution_by_chain"])
        self.assertEqual(module.GRANT_ADDRESS, payload["address"])

    def test_public_fallback_builds_eth_only_payload(self):
        module = load_generate_grant_wallet()

        with mock.patch.object(module, "fetch_eth_balance", return_value=Decimal("1.5")):
            with mock.patch.object(module, "fetch_eth_price_usd", return_value=Decimal("2000")):
                payload = module.portfolio_from_eth_balance("2026-05-28T00:00:00+00:00", timeout=1)

        self.assertTrue(payload["available"])
        self.assertEqual("ethereum_rpc_coingecko", payload["source"])
        self.assertEqual(3000.0, payload["portfolio"]["total_usd"])
        self.assertEqual(1.5, payload["portfolio"]["eth_balance"])
        self.assertEqual(2000.0, payload["portfolio"]["eth_price_usd"])

    def test_main_uses_unavailable_payload_when_lookup_fails_and_allowed(self):
        module = load_generate_grant_wallet()

        with mock.patch.dict(os.environ, {"ZERION_API_KEY": ""}, clear=False):
            with mock.patch.object(module, "portfolio_from_eth_balance", side_effect=RuntimeError("boom")):
                with mock.patch.object(module, "write_json") as write_json:
                    with mock.patch.object(
                        sys,
                        "argv",
                        ["generate_grant_wallet.py", "--allow-unavailable"],
                    ):
                        exit_code = module.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(2, write_json.call_count)
        payload = write_json.call_args_list[0].args[1]
        self.assertFalse(payload["available"])
        self.assertEqual("unavailable", payload["source"])
        self.assertIn("boom", payload["error"])


if __name__ == "__main__":
    unittest.main()
