#!/usr/bin/env python3
"""Conditionally create an S3 manifest from stable bytes supplied on stdin."""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError


MAX_MANIFEST_BYTES = 64 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024


class ManifestObjectError(RuntimeError):
    pass


def _read_manifest() -> tuple[tempfile.SpooledTemporaryFile[bytes], int, str]:
    manifest = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = sys.stdin.buffer.read(COPY_CHUNK_BYTES)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_MANIFEST_BYTES:
            manifest.close()
            raise ManifestObjectError(
                f"manifest exceeds {MAX_MANIFEST_BYTES} bytes"
            )
        manifest.write(chunk)
        digest.update(chunk)
    if size == 0:
        manifest.close()
        raise ManifestObjectError("manifest is empty")
    manifest.seek(0)
    return manifest, size, digest.hexdigest()


def _remote_digest(client, *, bucket: str, key: str) -> tuple[int, str] | None:
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        if code in {"NoSuchKey", "NotFound", "404"} or status == 404:
            return None
        raise
    body = response["Body"]
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = body.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_MANIFEST_BYTES:
                raise ManifestObjectError(
                    "existing manifest exceeds the local manifest byte cap"
                )
            digest.update(chunk)
    finally:
        body.close()
    return size, digest.hexdigest()


def put_once_or_verify(
    *,
    bucket: str,
    key: str,
    attempts: int,
    retry_base_seconds: float,
) -> None:
    manifest, size, digest = _read_manifest()
    try:
        client = boto3.client("s3")

        # Older botocore service models do not expose PutObject.IfNoneMatch as
        # a modeled argument even though S3 supports the HTTP header. Inject it
        # immediately before signing so the create-once condition is covered by
        # SigV4 and enforced atomically by S3.
        def add_if_none_match(model, params, **_kwargs):
            params.setdefault("headers", {})["If-None-Match"] = "*"

        client.meta.events.register("before-call.s3.PutObject", add_if_none_match)

        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            manifest.seek(0)
            try:
                client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=manifest,
                    ContentLength=size,
                    ContentType="application/json",
                    Metadata={"sha256": digest},
                )
                return
            except (BotoCoreError, ClientError, OSError) as exc:
                last_error = exc

            try:
                remote = _remote_digest(client, bucket=bucket, key=key)
            except (BotoCoreError, ClientError, OSError) as exc:
                last_error = exc
                remote = None
            if remote is not None:
                remote_size, remote_sha256 = remote
                if remote_size == size and remote_sha256 == digest:
                    return
                raise ManifestObjectError(
                    f"refusing to overwrite a different manifest at "
                    f"s3://{bucket}/{key}"
                )
            if attempt < attempts:
                time.sleep(retry_base_seconds * attempt)

        raise ManifestObjectError(
            f"could not create or verify s3://{bucket}/{key}: {last_error}"
        )
    finally:
        manifest.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--retry-base-seconds", type=float, default=2.0)
    args = parser.parse_args()
    if not 1 <= args.attempts <= 10:
        parser.error("--attempts must be between 1 and 10")
    if not 0 <= args.retry_base_seconds <= 60:
        parser.error("--retry-base-seconds must be between 0 and 60")
    try:
        put_once_or_verify(
            bucket=args.bucket,
            key=args.key,
            attempts=args.attempts,
            retry_base_seconds=args.retry_base_seconds,
        )
    except (ManifestObjectError, BotoCoreError, ClientError, OSError) as exc:
        print(f"manifest upload failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
