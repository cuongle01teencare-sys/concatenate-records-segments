"""
Ghép audio cấp session.

Với mỗi session (segment_b), lấy chunk .webm trong 2 folder audio nằm ngay
dưới segment_b rồi ghép từng folder thành 1 file, lưu vào concatenated/ của
chính segment_b đó:

    captures/<a>/<b>/shared_audio/**/*.webm  -> captures/<a>/<b>/concatenated/<b>_shared_audio.webm
    captures/<a>/<b>/mentor_audio/**/*.webm  -> captures/<a>/<b>/concatenated/<b>_mentor_audio.webm

Chấp nhận cả tên folder dạng gạch ngang (shared-audio / mentor-audio); tên
output luôn chuẩn hoá về gạch dưới.

Idempotent: nếu output của folder đó đã tồn tại và không rỗng thì bỏ qua.
"""

import re
import shutil
import tempfile
import traceback

from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path

from google.cloud.storage.blob import Blob
from google.cloud.storage.bucket import Bucket

from src.processing import (
    AUDIO_FOLDER_KINDS,
    concatenate_webm_files,
    download_student_blobs,
    get_blob_name,
    save_session_video,
)


DEFAULT_AUDIO_MATCH_GLOB = "captures/*/*/*audio/**/*.webm"

# Chunk audio thường có dạng chunk-<seq>-<epoch_ms>-....webm. Nếu tên không
# theo dạng này thì fallback sort theo tên blob.
_AUDIO_CHUNK_PATTERN = re.compile(
    r"^chunk-(?P<seq>\d+)-(?P<epoch_ms>\d+)"
)


@dataclass
class AudioConcatSummary:
    output_paths: list[str] = field(default_factory=list)
    processed: int = 0
    skipped_existing: int = 0
    failed: list[str] = field(default_factory=list)


def audio_chunk_sort_key(blob: Blob) -> tuple[int, int, int, str]:
    name = get_blob_name(blob)
    match = _AUDIO_CHUNK_PATTERN.match(name.rsplit("/", 1)[-1])
    if match is None:
        return (1, 0, 0, name)
    return (
        0,
        int(match.group("epoch_ms")),
        int(match.group("seq")),
        name,
    )


def audio_output_name(segment_b: str, kind: str) -> str:
    return f"{segment_b}_{kind}.webm"


def iter_session_audio_groups(
    *,
    bucket: Bucket,
    match_glob: str,
) -> Iterator[tuple[str, str, list[Blob]]]:
    """
    Yield (session_path, kind, blobs) cho từng cặp (segment_b, folder audio).
    kind là 'shared_audio' | 'mentor_audio'.
    GCS trả blob theo thứ tự từ điển nên các chunk cùng folder nằm liền nhau.
    """
    blobs = bucket.list_blobs(
        prefix="captures/",
        match_glob=match_glob,
    )

    def keyed() -> Iterator[tuple[tuple[str, str], Blob]]:
        for blob in blobs:
            blob_name = get_blob_name(blob)
            parts = blob_name.split("/")
            # 0 captures / 1 <a> / 2 <b> / 3 <audio folder> / 4+ chunk
            if len(parts) < 5 or parts[0] != "captures":
                continue
            kind = AUDIO_FOLDER_KINDS.get(parts[3])
            if kind is None:
                continue
            if not blob_name.lower().endswith(".webm"):
                continue
            if (blob.size or 0) <= 0:
                print(f"Skipped empty audio chunk: {blob_name}")
                continue
            yield ("/".join(parts[:3]), kind), blob

    for (session_path, kind), items in groupby(keyed(), key=lambda x: x[0]):
        yield session_path, kind, [blob for _, blob in items]


def session_audio_output_exists(
    *,
    bucket: Bucket,
    environment: str,
    session_path: str,
    output_name: str,
) -> bool:
    if environment == "local":
        from src.processing import LOCAL_OUTPUT_ROOT

        local_file = (
            LOCAL_OUTPUT_ROOT / session_path / "concatenated" / output_name
        )
        return local_file.exists() and local_file.stat().st_size > 0

    blob = bucket.get_blob(f"{session_path}/concatenated/{output_name}")
    return blob is not None and (blob.size or 0) > 0


def concatenate_session_audio(
    *,
    bucket: Bucket,
    session_path: str,
    kind: str,
    blobs: list[Blob],
    environment: str,
) -> str:
    """Ghép 1 folder audio của 1 session. Trả output path (gs:// hoặc local)."""
    segment_b = session_path.split("/")[-1]
    label = f"{segment_b}/{kind}"
    output_name = audio_output_name(segment_b, kind)
    ordered = sorted(blobs, key=audio_chunk_sort_key)

    temp_path = Path(tempfile.mkdtemp(prefix=f"audio-{segment_b}-{kind}-"))
    try:
        downloaded = download_student_blobs(
            blobs=ordered,
            destination=temp_path / "chunks",
            segment_c=label,
        )
        print(f"[{label}] Concatenating {len(downloaded)} audio chunk(s)...")
        output_file = concatenate_webm_files(
            files=downloaded,
            output_dir=temp_path,
            output_name=output_name,
            prefer_byte_concat=True,
        )
        return save_session_video(
            bucket=bucket,
            environment=environment,
            session_path=session_path,
            source_file=output_file,
            output_name=output_name,
            content_type="audio/webm",
        )
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)


def run_audio_concat(
    *,
    bucket: Bucket,
    environment: str,
    match_glob: str,
) -> AudioConcatSummary:
    summary = AudioConcatSummary()

    for session_path, kind, blobs in iter_session_audio_groups(
        bucket=bucket,
        match_glob=match_glob,
    ):
        segment_b = session_path.split("/")[-1]
        label = f"{segment_b}/{kind}"
        output_name = audio_output_name(segment_b, kind)

        if session_audio_output_exists(
            bucket=bucket,
            environment=environment,
            session_path=session_path,
            output_name=output_name,
        ):
            print(f"[{label}] Skipped: {output_name} already exists")
            summary.skipped_existing += 1
            continue

        print()
        print(f"=== Audio {label}: {len(blobs)} chunk(s) ===")

        try:
            output_path = concatenate_session_audio(
                bucket=bucket,
                session_path=session_path,
                kind=kind,
                blobs=blobs,
                environment=environment,
            )
        except Exception as error:
            print(f"[{label}] FAILED: {type(error).__name__}")
            print(traceback.format_exc()[-4000:])
            summary.failed.append(f"{session_path}/{kind}")
            continue

        summary.processed += 1
        summary.output_paths.append(output_path)
        print(f"[{label}] Output: {output_path}")

    print()
    print("==============================")
    print("Audio concatenation completed")
    print(f"Audio folders concatenated: {summary.processed}")
    print(f"Skipped (output already exists): {summary.skipped_existing}")
    print(f"Failed: {len(summary.failed)}")
    for item in summary.failed:
        print(f"  - {item}")
    print("==============================")

    return summary
