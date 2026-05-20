#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Sequence, Set, Tuple


TARGET_ROOTS: Tuple[str, ...] = ("main", "catalogues", "explosion", "videos")
ALLOWED_SUFFIXES: Set[str] = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".avif",
    ".svg",
    ".txt",
    ".csv",
    ".pdf",
    ".mp4",
    ".webm",
    ".mov",
}
SYNC_TAG_ENV = "R2_SYNC_TAG"
DEFAULT_SYNC_TAG = "r2-last-synced"


class SyncError(RuntimeError):
    pass


def run(cmd: Sequence[str], *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd),
        check=check,
        capture_output=capture,
        text=False,
    )


def run_text(cmd: Sequence[str], *, check: bool = True) -> str:
    result = run(cmd, capture=True, check=check)
    return result.stdout.decode("utf-8", errors="replace").strip()


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SyncError(f"Missing required environment variable: {name}")
    return value


def allowed_path(path: str) -> bool:
    lower = path.lower()
    return any(lower.endswith(ext) for ext in ALLOWED_SUFFIXES)


def ensure_in_roots(path: str) -> bool:
    normalized = path.lstrip("./")
    return any(normalized == root or normalized.startswith(f"{root}/") for root in TARGET_ROOTS)


def aws_cp(local_path: str, bucket: str, endpoint: str) -> None:
    destination = f"s3://{bucket}/{local_path}"
    run(
        [
            "aws",
            "s3",
            "cp",
            local_path,
            destination,
            "--endpoint-url",
            endpoint,
            "--no-progress",
            "--only-show-errors",
        ]
    )


def aws_rm(remote_path: str, bucket: str, endpoint: str) -> None:
    target = f"s3://{bucket}/{remote_path}"
    run(
        [
            "aws",
            "s3",
            "rm",
            target,
            "--endpoint-url",
            endpoint,
            "--only-show-errors",
        ]
    )


def full_sync(bucket: str, endpoint: str) -> Tuple[int, int]:
    include_args: List[str] = []
    for ext in sorted(ALLOWED_SUFFIXES):
        include_args.extend(["--include", f"*{ext}"])

    synced_roots = 0
    for root in TARGET_ROOTS:
        if not Path(root).exists():
            continue
        run(
            [
                "aws",
                "s3",
                "sync",
                root,
                f"s3://{bucket}/{root}",
                "--endpoint-url",
                endpoint,
                "--delete",
                "--exclude",
                "*",
                *include_args,
                "--no-progress",
                "--only-show-errors",
            ]
        )
        synced_roots += 1

    print(f"[r2-sync] initial full sync completed for {synced_roots} roots")
    return (0, 0)


def parse_name_status_z(data: bytes) -> Tuple[Set[str], Set[str]]:
    uploads: Set[str] = set()
    deletions: Set[str] = set()

    if not data:
        return uploads, deletions

    fields = data.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()

    idx = 0
    while idx < len(fields):
        status = fields[idx].decode("utf-8", errors="replace")
        idx += 1
        code = status[0] if status else ""

        if code in {"R", "C"}:
            if idx + 1 >= len(fields):
                raise SyncError("Unexpected diff format for rename/copy record")
            old_path = fields[idx].decode("utf-8", errors="surrogateescape")
            new_path = fields[idx + 1].decode("utf-8", errors="surrogateescape")
            idx += 2
            if code == "R":
                deletions.add(old_path)
            uploads.add(new_path)
            continue

        if idx >= len(fields):
            raise SyncError("Unexpected diff format for single-path record")
        path = fields[idx].decode("utf-8", errors="surrogateescape")
        idx += 1

        if code == "D":
            deletions.add(path)
        else:
            uploads.add(path)

    return uploads, deletions


def main() -> int:
    require_env("AWS_ACCESS_KEY_ID")
    require_env("AWS_SECRET_ACCESS_KEY")
    bucket = require_env("R2_BUCKET")
    endpoint = require_env("R2_ENDPOINT_URL")

    sync_tag = os.getenv(SYNC_TAG_ENV, DEFAULT_SYNC_TAG).strip() or DEFAULT_SYNC_TAG
    sync_ref = f"refs/tags/{sync_tag}"
    head_commit = run_text(["git", "rev-parse", "HEAD"])

    base_check = run(["git", "rev-parse", "-q", "--verify", f"{sync_ref}^{{commit}}"], capture=True, check=False)
    if base_check.returncode != 0:
        print(f"[r2-sync] no sync marker tag '{sync_tag}' found; running one-time full sync")
        full_sync(bucket, endpoint)
        run(["git", "tag", "-f", sync_tag, head_commit])
        print(f"[r2-sync] marker updated to {head_commit[:12]}")
        return 0

    base_commit = base_check.stdout.decode("utf-8", errors="replace").strip()
    diff = run(
        [
            "git",
            "diff",
            "--name-status",
            "--find-renames=90%",
            "-z",
            base_commit,
            head_commit,
            "--",
            *TARGET_ROOTS,
        ],
        capture=True,
    )
    uploads, deletions = parse_name_status_z(diff.stdout)

    filtered_uploads = sorted(
        {
            p.lstrip("./")
            for p in uploads
            if ensure_in_roots(p) and allowed_path(p) and Path(p).is_file()
        }
    )
    filtered_deletions = sorted(
        {
            p.lstrip("./")
            for p in deletions
            if ensure_in_roots(p) and allowed_path(p)
        }
    )

    if not filtered_uploads and not filtered_deletions:
        print(f"[r2-sync] no eligible delta files since marker {base_commit[:12]}")
        run(["git", "tag", "-f", sync_tag, head_commit])
        print(f"[r2-sync] marker advanced to {head_commit[:12]}")
        return 0

    for path in filtered_uploads:
        aws_cp(path, bucket, endpoint)

    for path in filtered_deletions:
        aws_rm(path, bucket, endpoint)

    print(
        "[r2-sync] delta applied: "
        f"uploads={len(filtered_uploads)} deletions={len(filtered_deletions)} "
        f"from={base_commit[:12]} to={head_commit[:12]}"
    )

    run(["git", "tag", "-f", sync_tag, head_commit])
    print(f"[r2-sync] marker updated to {head_commit[:12]}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as error:
        print(f"[r2-sync] ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
