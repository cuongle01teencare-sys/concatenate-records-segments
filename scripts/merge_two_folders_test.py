"""
Script test nhanh: ghép chunk từ 2 folder participants cụ thể để xem output có
xem được không.

Sort key theo yêu cầu:
    primary   = seq6      (số thứ tự chunk, 6 chữ số ở đầu tên file)
    secondary = epoch_ms  (unix timestamp ms, trường thứ 2)

Cách chạy (từ repo root):
    uv run python scripts/merge_two_folders_test.py

Output: ./test-output/nga__2800407418_merged.webm

Lưu ý: script này CỐ Ý dùng sort (seq6, epoch_ms) đúng như bạn chỉ định. Nếu
trong 2 folder có chunk lệch ngày (như chunk anomaly epoch ~2026-08-26 trong
folder ngày 2026-08-13), thứ tự byte có thể không trùng thứ tự thời gian —
xem log "Sort order preview" để tự kiểm tra trước khi mở file output.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Cho phép import src.* dù đứng ở đâu chạy.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.cloud import storage  # noqa: E402

from src.config import PROJECT_ID, BUCKET_NAME  # noqa: E402
from src.processing import concatenate_webm_files  # noqa: E402


SESSION_PREFIX = (
    "captures/amh-gxoi-asq/"
    "session-2026-08-13T12-59-59-273Z-tab-780602203/"
)

PREFIXES = [
    # Bỏ comment dòng dưới để gộp cả folder id-only.
    # f"{SESSION_PREFIX}participants/2800407418/video/",
    f"{SESSION_PREFIX}participants/nga__2800407418/video/",
]

OUTPUT_NAME = "nga__2800407418_only.webm"
OUTPUT_DIR = Path("test-output").resolve()

# chunk-<seq6>-<epoch_ms>-<stream_id>-<seq4>.webm
CHUNK_NAME_PATTERN = re.compile(
    r"^chunk-(?P<seq6>\d+)-(?P<epoch_ms>\d+)-(?P<stream_id>[^-]+)-\d+\.webm$"
)


def sort_key(blob) -> tuple[int, int, str]:
    """primary = seq6, secondary = epoch_ms, tertiary = full name (deterministic)."""
    name = (blob.name or "").rsplit("/", 1)[-1]
    match = CHUNK_NAME_PATTERN.match(name)
    if match is None:
        # Chunk không đúng format -> đẩy xuống cuối, không chen vào giữa.
        return (2**63 - 1, 2**63 - 1, blob.name or "")
    return (
        int(match.group("seq6")),
        int(match.group("epoch_ms")),
        blob.name or "",
    )


def format_epoch(epoch_ms: int) -> str:
    return (
        datetime
        .fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
        .isoformat()
    )


def describe_blob(blob) -> str:
    name = (blob.name or "").rsplit("/", 1)[-1]
    match = CHUNK_NAME_PATTERN.match(name)
    if match is None:
        return f"[unparsed] {blob.name}"
    epoch_ms = int(match.group("epoch_ms"))
    return (
        f"seq6={match.group('seq6'):>6}  "
        f"epoch={format_epoch(epoch_ms)}  "
        f"stream_id={match.group('stream_id')}  "
        f"{blob.name}"
    )


def main() -> None:
    client = storage.Client(project=PROJECT_ID)
    bucket = client.bucket(BUCKET_NAME)

    all_blobs = []
    for prefix in PREFIXES:
        print(f"Globbing gs://{BUCKET_NAME}/{prefix}")
        count_before = len(all_blobs)
        for blob in bucket.list_blobs(prefix=prefix):
            name = blob.name or ""
            if not name.lower().endswith(".webm"):
                continue
            if name.endswith("/"):
                continue
            all_blobs.append(blob)
        print(f"  -> {len(all_blobs) - count_before} chunk")

    print(f"\nTổng chunk gom được: {len(all_blobs)}")
    if not all_blobs:
        print("Không có chunk nào để ghép.")
        return

    all_blobs.sort(key=sort_key)

    print("\n=== Sort order (toàn bộ) ===")
    for rank, blob in enumerate(all_blobs, start=1):
        print(f"  {rank:>4}. {describe_blob(blob)}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="merge-two-folders-"))
    print(f"\nTemp download dir: {temp_dir}")

    local_files: list[Path] = []
    total = len(all_blobs)
    for rank, blob in enumerate(all_blobs, start=1):
        base_name = (blob.name or "").rsplit("/", 1)[-1]
        # Prefix rank để giữ thứ tự đã sort ngay trên disk
        # (concat demuxer fallback đọc theo thứ tự file).
        dest = temp_dir / f"{rank:06d}_{base_name}"
        print(
            f"Downloading {rank}/{total}: "
            f"{blob.name} ({blob.size or 0} bytes)"
        )
        blob.download_to_filename(str(dest))
        local_files.append(dest)

    print(f"\nGhép {len(local_files)} chunk (byte-concat + FFmpeg remux)...")
    output_file = concatenate_webm_files(
        files=local_files,
        output_dir=OUTPUT_DIR,
        output_name=OUTPUT_NAME,
        prefer_byte_concat=True,
    )

    total_bytes = sum(f.stat().st_size for f in local_files)
    print("\n==============================")
    print("DONE")
    print(f"Chunks merged   : {len(local_files)}")
    print(f"Source total    : {total_bytes} bytes")
    print(f"Output (local)  : {output_file}")
    print(f"Output size     : {output_file.stat().st_size} bytes")
    print(f"Temp downloads  : {temp_dir}")
    print("==============================")


if __name__ == "__main__":
    main()
