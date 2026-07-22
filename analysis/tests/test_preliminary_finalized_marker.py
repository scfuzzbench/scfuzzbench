import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


def load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "preliminary_results.py"
    spec = importlib.util.spec_from_file_location("preliminary_results", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FinalizedMarkerTests(unittest.TestCase):
    RUN_ID = "gh-29816663987-1"
    UUID = "f" * 32

    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def base_marker(self, **overrides):
        payload = {
            "schema": self.module.FINALIZED_SCHEMA,
            "run_id": self.RUN_ID,
            "benchmark_uuid": self.UUID,
            "canonical_release_tag": f"scfuzzbench-{self.UUID}-{self.RUN_ID}",
            "preliminary_stream_closed": True,
        }
        payload.update(overrides)
        return payload

    def test_canonical_marker_still_validates(self):
        marker = self.base_marker()
        self.assertIs(
            marker,
            self.module.validate_finalized_marker(
                marker, run_id=self.RUN_ID, benchmark_uuid=self.UUID
            ),
        )

    def test_superseded_marker_validates_without_canonical_release(self):
        marker = self.base_marker(
            canonical_release_tag=None,
            superseded=True,
            superseded_reference="superseded by #258",
        )
        self.assertIs(
            marker,
            self.module.validate_finalized_marker(
                marker, run_id=self.RUN_ID, benchmark_uuid=self.UUID
            ),
        )

    def test_superseded_marker_must_not_claim_a_canonical_release(self):
        marker = self.base_marker(
            superseded=True,
            superseded_reference="superseded by #258",
        )
        with self.assertRaises(ValueError):
            self.module.validate_finalized_marker(
                marker, run_id=self.RUN_ID, benchmark_uuid=self.UUID
            )

    def test_superseded_marker_requires_bounded_reference(self):
        for reference in ("", "   ", "x" * 201, 258, None):
            with self.subTest(reference=reference):
                marker = self.base_marker(
                    canonical_release_tag=None,
                    superseded=True,
                    superseded_reference=reference,
                )
                with self.assertRaises(ValueError):
                    self.module.validate_finalized_marker(
                        marker, run_id=self.RUN_ID, benchmark_uuid=self.UUID
                    )

    def test_non_superseded_marker_still_requires_exact_canonical_tag(self):
        marker = self.base_marker(canonical_release_tag="forged")
        with self.assertRaises(ValueError):
            self.module.validate_finalized_marker(
                marker, run_id=self.RUN_ID, benchmark_uuid=self.UUID
            )

    def test_mark_finalized_requires_exactly_one_mode(self):
        for kwargs in (
            {},
            {
                "canonical_tag": f"scfuzzbench-{self.UUID}-{self.RUN_ID}",
                "superseded_reference": "superseded by #258",
            },
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.module.mark_finalized(
                        bucket="bucket",
                        run_id=self.RUN_ID,
                        benchmark_uuid=self.UUID,
                        **kwargs,
                    )

    def test_mark_finalized_writes_superseded_payload(self):
        module = self.module
        captured = {}

        def capture(*, bucket, key, source, **_kwargs):
            captured["bucket"] = bucket
            captured["key"] = key
            captured["payload"] = json.loads(source.read_text(encoding="utf-8"))
            return "digest"

        with mock.patch.object(module, "put_immutable", side_effect=capture):
            key = module.mark_finalized(
                bucket="bucket",
                run_id=self.RUN_ID,
                benchmark_uuid=self.UUID,
                superseded_reference="superseded by #258",
            )
        self.assertEqual(
            f"preliminary/{self.RUN_ID}/{self.UUID}/finalized.json", key
        )
        payload = captured["payload"]
        self.assertIsNone(payload["canonical_release_tag"])
        self.assertIs(True, payload["superseded"])
        self.assertEqual("superseded by #258", payload["superseded_reference"])
        self.assertIs(True, payload["preliminary_stream_closed"])

    def test_mark_finalized_converges_on_existing_valid_marker(self):
        # A superseded-form marker written before a restore must not wedge
        # the later canonical closure: any valid marker closes the stream.
        module = self.module
        existing = self.base_marker(
            canonical_release_tag=None,
            superseded=True,
            superseded_reference="superseded by #258",
        )
        with mock.patch.object(
            module, "put_immutable", side_effect=RuntimeError("divergent")
        ), mock.patch.object(
            module.subprocess,
            "check_output",
            return_value=json.dumps(existing),
        ):
            key = module.mark_finalized(
                bucket="bucket",
                run_id=self.RUN_ID,
                benchmark_uuid=self.UUID,
                canonical_tag=f"scfuzzbench-{self.UUID}-{self.RUN_ID}",
            )
        self.assertEqual(
            f"preliminary/{self.RUN_ID}/{self.UUID}/finalized.json", key
        )

        forged = self.base_marker(canonical_release_tag="forged")
        with mock.patch.object(
            module, "put_immutable", side_effect=RuntimeError("divergent")
        ), mock.patch.object(
            module.subprocess,
            "check_output",
            return_value=json.dumps(forged),
        ):
            with self.assertRaises(ValueError):
                module.mark_finalized(
                    bucket="bucket",
                    run_id=self.RUN_ID,
                    benchmark_uuid=self.UUID,
                    canonical_tag=f"scfuzzbench-{self.UUID}-{self.RUN_ID}",
                )

    def test_mark_finalized_writes_canonical_payload(self):
        module = self.module
        captured = {}

        def capture(*, bucket, key, source, **_kwargs):
            captured["payload"] = json.loads(source.read_text(encoding="utf-8"))
            return "digest"

        with mock.patch.object(module, "put_immutable", side_effect=capture):
            module.mark_finalized(
                bucket="bucket",
                run_id=self.RUN_ID,
                benchmark_uuid=self.UUID,
                canonical_tag=f"scfuzzbench-{self.UUID}-{self.RUN_ID}",
            )
        payload = captured["payload"]
        self.assertEqual(
            f"scfuzzbench-{self.UUID}-{self.RUN_ID}",
            payload["canonical_release_tag"],
        )
        self.assertNotIn("superseded", payload)


if __name__ == "__main__":
    unittest.main()
