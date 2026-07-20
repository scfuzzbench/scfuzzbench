#!/usr/bin/env python3
"""Upload only bytes copied from an fd-pinned, stable regular file."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import time

import boto3
from boto3.exceptions import Boto3Error
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import BotoCoreError, ClientError

import safe_path_ops


COPY_CHUNK_BYTES = 1024 * 1024
DEFAULT_MAX_BYTES = 8 * 1024 * 1024 * 1024


class PinnedUploadError(RuntimeError):
    pass


def spool_pinned_file(
    *,
    root_path: str,
    root_anchor: str,
    root_identity: str,
    path: str,
    max_bytes: int,
) -> tuple[tempfile.SpooledTemporaryFile[bytes], int, str]:
    parts = safe_path_ops._relative_parts(path, root_path, root_anchor)
    spool = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    try:
        with safe_path_ops._open_anchor(
            root_path, root_anchor, root_identity
        ) as root_fd:
            with safe_path_ops._open_parent(root_fd, parts) as (parent_fd, name):
                source_fd = os.open(
                    name, safe_path_ops.FILE_READ_FLAGS, dir_fd=parent_fd
                )
                try:
                    before = os.fstat(source_fd)
                    safe_path_ops._require_regular(before, f"upload source {path}")
                    if before.st_size > max_bytes:
                        raise PinnedUploadError(
                            f"upload source exceeds {max_bytes} bytes"
                        )
                    safe_path_ops._run_test_hook()
                    safe_path_ops._verify_anchor_path(root_anchor, root_fd)
                    safe_path_ops._verify_directory_path(
                        root_fd,
                        parts[:-1],
                        parent_fd,
                        "upload source parent",
                    )
                    digest = hashlib.sha256()
                    copied = 0
                    while True:
                        chunk = os.read(source_fd, COPY_CHUNK_BYTES)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > max_bytes:
                            raise PinnedUploadError(
                                f"upload source exceeds {max_bytes} bytes"
                            )
                        spool.write(chunk)
                        digest.update(chunk)
                    after = os.fstat(source_fd)
                    if copied != before.st_size or not safe_path_ops._same_entry(
                        before, after
                    ):
                        raise PinnedUploadError(
                            "upload source changed while being copied"
                        )
                    current = os.stat(
                        name, dir_fd=parent_fd, follow_symlinks=False
                    )
                    if not safe_path_ops._same_entry(before, current):
                        raise PinnedUploadError(
                            "upload source path changed while being copied"
                        )
                    safe_path_ops._verify_anchor_path(root_anchor, root_fd)
                    safe_path_ops._verify_directory_path(
                        root_fd,
                        parts[:-1],
                        parent_fd,
                        "upload source parent",
                    )
                finally:
                    os.close(source_fd)
        spool.seek(0)
        return spool, copied, digest.hexdigest()
    except BaseException:
        spool.close()
        raise


def _remote_digest(client, *, bucket: str, key: str, max_bytes: int):
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
            if size > max_bytes:
                raise PinnedUploadError("existing object exceeds the upload byte cap")
            digest.update(chunk)
    finally:
        body.close()
    return size, digest.hexdigest()


def _put_immutable(
    client,
    spool,
    *,
    size: int,
    digest: str,
    bucket: str,
    key: str,
    content_type: str,
    max_bytes: int,
    attempts: int,
    retry_base_seconds: float,
) -> None:
    def add_if_none_match(model, params, **_kwargs):
        params.setdefault("headers", {})["If-None-Match"] = "*"

    client.meta.events.register("before-call.s3.PutObject", add_if_none_match)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        spool.seek(0)
        try:
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=spool,
                ContentLength=size,
                ContentType=content_type,
                Metadata={"sha256": digest},
            )
            return
        except (BotoCoreError, ClientError, OSError) as exc:
            last_error = exc
        try:
            remote = _remote_digest(
                client, bucket=bucket, key=key, max_bytes=max_bytes
            )
        except (BotoCoreError, ClientError, OSError) as exc:
            last_error = exc
            remote = None
        if remote is not None:
            if remote == (size, digest):
                return
            raise PinnedUploadError(
                f"refusing to overwrite a different object at s3://{bucket}/{key}"
            )
        if attempt < attempts:
            time.sleep(attempt * retry_base_seconds)
    raise PinnedUploadError(
        f"could not create or verify s3://{bucket}/{key}: {last_error}"
    )


def _upload_replaceable(
    client,
    spool,
    *,
    digest: str,
    bucket: str,
    key: str,
    content_type: str,
    attempts: int,
    retry_base_seconds: float,
) -> None:
    transfer = TransferConfig(
        multipart_threshold=64 * 1024 * 1024,
        multipart_chunksize=64 * 1024 * 1024,
        max_concurrency=4,
        use_threads=True,
    )
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        spool.seek(0)
        try:
            client.upload_fileobj(
                spool,
                bucket,
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "Metadata": {"sha256": digest},
                },
                Config=transfer,
            )
            return
        except (Boto3Error, BotoCoreError, ClientError, OSError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt * retry_base_seconds)
    raise PinnedUploadError(f"could not upload s3://{bucket}/{key}: {last_error}")


def upload_pinned(
    *,
    root_path: str,
    root_anchor: str,
    root_identity: str,
    path: str,
    max_bytes: int,
    bucket: str,
    key: str,
    content_type: str,
    immutable: bool,
    attempts: int,
    retry_base_seconds: float,
) -> tuple[int, str]:
    spool, size, digest = spool_pinned_file(
        root_path=root_path,
        root_anchor=root_anchor,
        root_identity=root_identity,
        path=path,
        max_bytes=max_bytes,
    )
    try:
        client = boto3.client("s3")
        if immutable:
            _put_immutable(
                client,
                spool,
                size=size,
                digest=digest,
                bucket=bucket,
                key=key,
                content_type=content_type,
                max_bytes=max_bytes,
                attempts=attempts,
                retry_base_seconds=retry_base_seconds,
            )
        else:
            _upload_replaceable(
                client,
                spool,
                digest=digest,
                bucket=bucket,
                key=key,
                content_type=content_type,
                attempts=attempts,
                retry_base_seconds=retry_base_seconds,
            )
        return size, digest
    finally:
        spool.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-path", required=True)
    parser.add_argument("--root-anchor", required=True)
    parser.add_argument("--root-identity", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument(
        "--content-type", default="application/octet-stream"
    )
    parser.add_argument("--immutable", action="store_true")
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--retry-base-seconds", type=float, default=2.0)
    args = parser.parse_args()
    if args.max_bytes < 1:
        parser.error("--max-bytes must be positive")
    if not 1 <= args.attempts <= 10:
        parser.error("--attempts must be between 1 and 10")
    if not 0 <= args.retry_base_seconds <= 60:
        parser.error("--retry-base-seconds must be between 0 and 60")
    try:
        size, digest = upload_pinned(
            root_path=args.root_path,
            root_anchor=args.root_anchor,
            root_identity=args.root_identity,
            path=args.path,
            max_bytes=args.max_bytes,
            bucket=args.bucket,
            key=args.key,
            content_type=args.content_type,
            immutable=args.immutable,
            attempts=args.attempts,
            retry_base_seconds=args.retry_base_seconds,
        )
    except (
        Boto3Error,
        BotoCoreError,
        ClientError,
        OSError,
        PinnedUploadError,
        safe_path_ops.SafePathError,
    ) as exc:
        print(f"pinned upload failed: {exc}", file=sys.stderr)
        return 1
    print(f"{digest} {size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
