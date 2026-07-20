import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

from botocore.exceptions import ClientError, EndpointConnectionError


REPO_ROOT = Path(__file__).resolve().parents[2]
SHARED_DIR = REPO_ROOT / "fuzzers" / "_shared"


def load_module():
    sys.path.insert(0, str(SHARED_DIR))
    path = SHARED_DIR / "upload_pinned_file.py"
    spec = importlib.util.spec_from_file_location("upload_pinned_file_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeEvents:
    def __init__(self):
        self.handler = None

    def register(self, event_name, handler):
        if event_name != "before-call.s3.PutObject":
            raise AssertionError(event_name)
        self.handler = handler


class FakeImmutableClient:
    def __init__(self, *, put_succeeds: bool, existing: bytes | None):
        self.meta = type("Meta", (), {"events": FakeEvents()})()
        self.put_succeeds = put_succeeds
        self.existing = existing
        self.put_headers: dict[str, str] = {}
        self.put_bytes = b""

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


class FakeReplaceableClient:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = 0
        self.uploaded = b""

    def upload_fileobj(self, spool, _bucket, _key, **_kwargs):
        self.calls += 1
        self.uploaded = spool.read()
        if self.failure is not None:
            raise self.failure


class UploadPinnedFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def root_values(self, root: Path) -> tuple[str, str, str]:
        resolved = root.resolve(strict=True)
        info = resolved.stat()
        return str(root), str(resolved), f"{info.st_dev}:{info.st_ino}"

    def spool(self, root: Path, path: Path):
        root_path, root_anchor, root_identity = self.root_values(root)
        return self.module.spool_pinned_file(
            root_path=root_path,
            root_anchor=root_anchor,
            root_identity=root_identity,
            path=str(path),
            max_bytes=1024 * 1024,
        )

    def test_spools_exact_stable_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "nested" / "artifact.zip"
            source.parent.mkdir()
            source.write_bytes(b"artifact bytes")

            spool, size, digest = self.spool(root, source)
            try:
                self.assertEqual(b"artifact bytes", spool.read())
                self.assertEqual(14, size)
                self.assertEqual(
                    "4659fc0570122b0e0aa14f4ff7c261b1fe51795a01ba79963f462ebf40d7520d",
                    digest,
                )
            finally:
                spool.close()

    def test_symlink_hardlink_and_fifo_are_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            regular = root / "regular"
            regular.write_text("data", encoding="utf-8")
            linked = root / "linked"
            linked.symlink_to(regular)
            hardlink = root / "hardlink"
            os.link(regular, hardlink)
            fifo = root / "fifo"
            os.mkfifo(fifo)

            started = time.monotonic()
            for source in (linked, hardlink, fifo):
                with self.subTest(source=source.name):
                    with self.assertRaises(
                        (OSError, self.module.safe_path_ops.SafePathError)
                    ):
                        self.spool(root, source)
            self.assertLess(time.monotonic() - started, 1.0)

    def test_parent_swap_is_rejected_without_reading_outside_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "artifacts"
            parent.mkdir()
            source = parent / "result.zip"
            source.write_text("inside", encoding="utf-8")
            outside = root / "outside"
            outside.mkdir()
            outside_source = outside / "result.zip"
            outside_source.write_text("secret", encoding="utf-8")
            hook = root / "hook.py"
            hook.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                f"parent = Path({str(parent)!r})\n"
                f"saved = Path({str(root / 'artifacts-saved')!r})\n"
                f"outside = Path({str(outside)!r})\n"
                "parent.rename(saved)\n"
                "parent.symlink_to(outside, target_is_directory=True)\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)

            with mock.patch.dict(
                os.environ,
                {"SCFUZZBENCH_SAFE_PATH_TEST_HOOK": str(hook)},
            ):
                with self.assertRaises(
                    (OSError, self.module.safe_path_ops.SafePathError)
                ):
                    self.spool(root, source)
            self.assertEqual("secret", outside_source.read_text(encoding="utf-8"))

    def test_immutable_upload_uses_create_once_and_verifies_ambiguous_write(self):
        data = b"immutable artifact"
        for put_succeeds in (True, False):
            with self.subTest(put_succeeds=put_succeeds):
                client = FakeImmutableClient(
                    put_succeeds=put_succeeds,
                    existing=None if put_succeeds else data,
                )
                self.module._put_immutable(
                    client,
                    io.BytesIO(data),
                    size=len(data),
                    digest=self.module.hashlib.sha256(data).hexdigest(),
                    bucket="bucket",
                    key="artifact.zip",
                    content_type="application/zip",
                    max_bytes=1024,
                    attempts=2,
                    retry_base_seconds=0,
                )
                self.assertEqual("*", client.put_headers["If-None-Match"])
                self.assertEqual(data, client.put_bytes)

    def test_immutable_upload_rejects_divergent_existing_object(self):
        client = FakeImmutableClient(
            put_succeeds=False,
            existing=b"different",
        )
        with self.assertRaisesRegex(
            self.module.PinnedUploadError, "different object"
        ):
            self.module._put_immutable(
                client,
                io.BytesIO(b"expected"),
                size=8,
                digest=self.module.hashlib.sha256(b"expected").hexdigest(),
                bucket="bucket",
                key="artifact.zip",
                content_type="application/zip",
                max_bytes=1024,
                attempts=1,
                retry_base_seconds=0,
            )

    def test_replaceable_upload_retries_boto3_transfer_failures(self):
        client = FakeReplaceableClient(
            self.module.boto3.exceptions.S3UploadFailedError("transfer failed")
        )
        with (
            mock.patch.object(self.module.time, "sleep") as sleep,
            self.assertRaisesRegex(
                self.module.PinnedUploadError, "could not upload"
            ),
        ):
            self.module._upload_replaceable(
                client,
                io.BytesIO(b"retry me"),
                digest=self.module.hashlib.sha256(b"retry me").hexdigest(),
                bucket="bucket",
                key="artifact.zip",
                content_type="application/zip",
                attempts=3,
                retry_base_seconds=0,
            )
        self.assertEqual(3, client.calls)
        self.assertEqual(b"retry me", client.uploaded)
        self.assertEqual(2, sleep.call_count)


if __name__ == "__main__":
    unittest.main()
