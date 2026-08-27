"""
Quick helper: glob all .webm audio chunks under a GCS prefix (e.g. a session's
mentor-audio/ folder), download them, and concatenate into a single local WebM
so you can play it back and compare.

Reuses the project's proven concatenation (byte-concat + FFmpeg remux, with a
concat-demuxer fallback), the same path used for MediaRecorder video chunks.

Usage (from the repo root):
    uv run python scripts/concat_mentor_audio.py
    uv run python scripts/concat_mentor_audio.py "captures/<a>/<b>/mentor-audio/"
    uv run python scripts/concat_mentor_audio.py "gs://meet-captures-2026/captures/<a>/<b>/mentor-audio/"

Output is written locally to ./mentor-audio-output/.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Make `src` importable no matter the current working directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.cloud import storage  # noqa: E402

from src.config import PROJECT_ID, BUCKET_NAME  # noqa: E402
from src.processing import concatenate_webm_files  # noqa: E402


DEFAULT_PREFIX = (
    "captures/ajd-tvou-jik/"
    "session-2026-08-24T12-25-20-663Z-tab-154424347/"
    "mentor-audio/"
)


def normalize_prefix(value: str, bucket_name: str) -> str:
    """Accept a bare prefix, a gs:// URI, or a bucket/prefix and return the
    object prefix ending with a single trailing slash."""
    prefix = value.strip()

    gs_prefix = "gs://"
    if prefix.startswith(gs_prefix):
        prefix = prefix[len(gs_prefix):]

    if prefix.startswith(f"{bucket_name}/"):
        prefix = prefix[len(bucket_name) + 1:]

    prefix = prefix.lstrip("/")

    if prefix and not prefix.endswith("/"):
        prefix += "/"

    return prefix


def glob_audio_blobs(bucket, prefix: str) -> list:
    """Glob every non-empty .webm object directly (and recursively) under the
    prefix, sorted by name so chunks concatenate in order."""
    blobs = [
        blob
        for blob in bucket.list_blobs(prefix=prefix)
        if (blob.name or "").lower().endswith(".webm")
        and not (blob.name or "").endswith("/")
    ]
    blobs.sort(key=lambda blob: blob.name or "")
    return blobs


def main(argv: list[str]) -> None:
    prefix_arg = argv[1] if len(argv) > 1 else DEFAULT_PREFIX

    client = storage.Client(project=PROJECT_ID)
    bucket = client.bucket(BUCKET_NAME)

    prefix = normalize_prefix(prefix_arg, BUCKET_NAME)
    print(f"Globbing gs://{BUCKET_NAME}/{prefix}")

    blobs = glob_audio_blobs(bucket, prefix)
    print(f"Found {len(blobs)} .webm audio file(s).")

    if not blobs:
        print("No audio files found under that prefix.")
        return

    # Derive a readable output name from the session folder (segment_b).
    parts = prefix.rstrip("/").split("/")
    segment_b = parts[2] if len(parts) >= 3 else "session"
    output_name = f"{segment_b}_mentor_audio.webm"

    out_dir = Path("mentor-audio-output").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    temp_dir = Path(tempfile.mkdtemp(prefix="mentor-audio-"))
    local_files: list[Path] = []

    total = len(blobs)
    for index, blob in enumerate(blobs, start=1):
        base_name = (blob.name or "").split("/")[-1]
        # Prefix with the index to guarantee stable ordering on disk.
        dest = temp_dir / f"{index:06d}_{base_name}"
        print(f"Downloading {index}/{total}: {blob.name} ({blob.size or 0} bytes)")
        blob.download_to_filename(str(dest))
        local_files.append(dest)

    print(f"\nConcatenating {len(local_files)} chunk(s) with byte-concat + remux...")
    output_file = concatenate_webm_files(
        files=local_files,
        output_dir=out_dir,
        output_name=output_name,
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
    main(sys.argv)
