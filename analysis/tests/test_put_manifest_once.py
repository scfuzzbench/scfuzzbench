import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

from botocore.exceptions import ClientError, EndpointConnectionError


def load_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "fuzzers"
        / "_shared"
        / "put_manifest_once.py"
    )
    spec = importlib.util.spec_from_file_location("put_manifest_once_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeStdin:
    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)


class FakeEvents:
    def __init__(self):
        self.handler = None

    def register(self, event_name, handler):
        if event_name != "before-call.s3.PutObject":
            raise AssertionError(event_name)
        self.handler = handler


class FakeClient:
    def __init__(self, *, put_succeeds: bool, existing: bytes | None):
        self.meta = type("Meta", (), {"events": FakeEvents()})()
        self.put_succeeds = put_succeeds
        self.existing = existing
        self.put_bytes = b""
        self.put_headers: dict[str, str] = {}

    def put_object(self, **kwargs):
        request = {"headers": {}}
        if self.meta.events.handler is None:
            raise AssertionError("conditional header hook was not registered")
        self.meta.events.handler(None, request)
        self.put_headers = request["headers"]
        self.put_bytes = kwargs["Body"].read()
        if kwargs["ContentLength"] != len(self.put_bytes):
            raise AssertionError("incorrect ContentLength")
        if self.put_succeeds:
            self.existing = self.put_bytes
            return {}
        raise EndpointConnectionError(endpoint_url="https://s3.invalid")

    def get_object(self, **_kwargs):
        if self.existing is None:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "GetObject",
            )
        return {"Body": io.BytesIO(self.existing)}


class PutManifestOnceTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def invoke(self, data: bytes, client: FakeClient) -> None:
        with (
            mock.patch.object(self.module.sys, "stdin", FakeStdin(data)),
            mock.patch.object(self.module.boto3, "client", return_value=client),
            mock.patch.object(self.module.time, "sleep"),
        ):
            self.module.put_once_or_verify(
                bucket="bucket",
                key="logs/run/manifest.json",
                attempts=2,
                retry_base_seconds=0,
            )

    def test_create_success_uses_signed_create_once_header(self):
        data = b'{"commit":"abc"}\n'
        client = FakeClient(put_succeeds=True, existing=None)

        self.invoke(data, client)

        self.assertEqual(data, client.put_bytes)
        self.assertEqual("*", client.put_headers["If-None-Match"])
        self.assertEqual(data, client.existing)

    def test_timeout_after_success_accepts_byte_identical_existing_object(self):
        data = b'{"commit":"abc"}\n'
        client = FakeClient(put_succeeds=False, existing=data)

        self.invoke(data, client)

        self.assertEqual(data, client.put_bytes)
        self.assertEqual("*", client.put_headers["If-None-Match"])

    def test_divergent_existing_object_is_rejected(self):
        client = FakeClient(
            put_succeeds=False,
            existing=b'{"commit":"different"}\n',
        )

        with self.assertRaisesRegex(
            self.module.ManifestObjectError, "different manifest"
        ):
            self.invoke(b'{"commit":"abc"}\n', client)

    def test_empty_and_oversize_inputs_are_rejected_before_network(self):
        for data, expected in ((b"", "empty"), (b"12345", "exceeds")):
            with self.subTest(expected=expected):
                client = FakeClient(put_succeeds=True, existing=None)
                with (
                    mock.patch.object(self.module.sys, "stdin", FakeStdin(data)),
                    mock.patch.object(self.module, "MAX_MANIFEST_BYTES", 4),
                    mock.patch.object(
                        self.module.boto3, "client", return_value=client
                    ) as boto_client,
                ):
                    with self.assertRaisesRegex(
                        self.module.ManifestObjectError, expected
                    ):
                        self.module.put_once_or_verify(
                            bucket="bucket",
                            key="manifest.json",
                            attempts=1,
                            retry_base_seconds=0,
                        )
                    boto_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
