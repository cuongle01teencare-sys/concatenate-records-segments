import shutil
import subprocess
import tempfile

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from google.cloud.storage.blob import Blob
from google.cloud.storage.bucket import Bucket


LOCAL_OUTPUT_ROOT = Path("output")


@dataclass
class SessionProcessingResult:
    output_paths: list[str]
    failed_participants: int
    failed_session_outputs: int


def get_blob_name(blob: Blob) -> str:
    return blob.name or ""


def profile_data(bucket: Bucket) -> None:
    blobs = bucket.list_blobs(
        prefix="captures/",
        match_glob="captures/*/*/participants/*/video/**/*.webm",
        max_results=1,
    )

    first_blob = next(iter(blobs), None)

    if first_blob is None:
        print("Không tìm thấy blob WebM nào phù hợp.")
        return

    first_blob_name = get_blob_name(first_blob)
    parts = first_blob_name.split("/")

    print(f"Blob: {first_blob_name}")

    if len(parts) >= 5:
        print(f"segment_c: {parts[4]}")


def iter_participant_groups(
    bucket: Bucket,
    should_process_but_no: list[str],
    should_not_process_but_yes: list[str],
    match_glob: str = "captures/*/*/participants/*/video/**/*.webm",
) -> Iterator[tuple[str, list[Blob]]]:
    """
    Đọc blobs theo thứ tự từ GCS.

    Khi phát hiện đã chuyển sang participant khác,
    yield toàn bộ blobs của participant trước đó ngay lập tức.

    participant_path có dạng:

    captures/<segment_a>/<segment_b>/participants/<segment_c>
    """

    blobs = bucket.list_blobs(
        prefix="captures/",
        match_glob=match_glob,
    )

    current_participant_path: str | None = None
    current_blobs: list[Blob] = []

    for page_number, page in enumerate(blobs.pages, start=1):
        page_blobs = list(page)

        if not page_blobs:
            continue

        print(
            f"Fetched page {page_number}: "
            f"{len(page_blobs)} blob(s)"
        )

        for blob in page_blobs:
            blob_name = get_blob_name(blob)

            print(f"Reading: {blob_name}")

            parts = blob_name.split("/")

            # Expected:
            #
            # 0 captures
            # 1 <segment_a>
            # 2 <segment_b>
            # 3 participants
            # 4 <segment_c>
            # 5 video
            # 6+ blob path
            if len(parts) < 7:
                if "participants" in parts:
                    should_process_but_no.append(blob_name)

                continue

            if parts[3] != "participants":
                should_not_process_but_yes.append(blob_name)
                continue

            if parts[5] != "video":
                should_not_process_but_yes.append(blob_name)
                continue

            if not blob_name.lower().endswith(".webm"):
                should_not_process_but_yes.append(blob_name)
                continue

            participant_path = "/".join(parts[:5])

            # Blob hợp lệ đầu tiên.
            if current_participant_path is None:
                current_participant_path = participant_path

            # Đã chuyển sang participant tiếp theo.
            elif participant_path != current_participant_path:
                yield (
                    current_participant_path,
                    current_blobs,
                )

                current_participant_path = participant_path
                current_blobs = []

            current_blobs.append(blob)

    # Participant cuối cùng không có participant tiếp theo
    # để kích hoạt yield phía trên.
    if current_participant_path is not None and current_blobs:
        yield (
            current_participant_path,
            current_blobs,
        )


def iter_session_groups(
    bucket: Bucket,
    should_process_but_no: list[str],
    should_not_process_but_yes: list[str],
    match_glob: str = "captures/*/*/participants/*/video/**/*.webm",
) -> Iterator[tuple[str, list[Blob]]]:
    """
    Đọc blobs theo thứ tự từ GCS và group theo buổi.

    session_path có dạng:

    captures/<segment_a>/<segment_b>
    """

    blobs = bucket.list_blobs(
        prefix="captures/",
        match_glob=match_glob,
    )

    current_session_path: str | None = None
    current_blobs: list[Blob] = []

    for page_number, page in enumerate(blobs.pages, start=1):
        page_blobs = list(page)

        if not page_blobs:
            continue

        print(
            f"Fetched page {page_number}: "
            f"{len(page_blobs)} blob(s)"
        )

        for blob in page_blobs:
            blob_name = get_blob_name(blob)

            print(f"Reading: {blob_name}")

            parts = blob_name.split("/")

            # Expected:
            #
            # 0 captures
            # 1 <segment_a>
            # 2 <segment_b>
            # 3 participants
            # 4 <segment_c>
            # 5 video
            # 6+ blob path
            if len(parts) < 7:
                if "participants" in parts:
                    should_process_but_no.append(blob_name)

                continue

            if parts[3] != "participants":
                should_not_process_but_yes.append(blob_name)
                continue

            if parts[5] != "video":
                should_not_process_but_yes.append(blob_name)
                continue

            if not blob_name.lower().endswith(".webm"):
                should_not_process_but_yes.append(blob_name)
                continue

            session_path = "/".join(parts[:3])

            if current_session_path is None:
                current_session_path = session_path
            elif session_path != current_session_path:
                yield (
                    current_session_path,
                    current_blobs,
                )

                current_session_path = session_path
                current_blobs = []

            current_blobs.append(blob)

    if current_session_path is not None and current_blobs:
        yield (
            current_session_path,
            current_blobs,
        )


def concatenate_participant_video(
    *,
    bucket: Bucket,
    participant_path: str,
    blobs: list[Blob],
    environment: str,
    keep_all_webm_chunks: bool,
    keep_all_concatenated_webm_files: bool,
    invalid_webm_backlog: list[str],
    keep_temp_on_failure: bool,
) -> str | None:
    """
    Download toàn bộ WebM của một participant,
    concatenate bằng FFmpeg,
    rồi lưu theo environment.

    Return path của output:
    - local: đường dẫn local
    - container: gs://...
    - None: output không được giữ lại
    """

    if not blobs:
        raise ValueError(
            f"Participant {participant_path} không có blob nào."
        )

    segment_c = participant_path.split("/")[-1]

    # Ghép từ trên xuống dưới theo tên object.
    ordered_blobs = sorted(
        blobs,
        key=lambda blob: blob.name or "",
    )

    print()
    print(f"=== Processing participant {segment_c} ===")
    print(f"Chunks: {len(ordered_blobs)}")

    # Tự quản lý temp dir để có thể giữ lại khi cần debug failure.
    temp_path = Path(
        tempfile.mkdtemp(
            prefix=f"participant-{segment_c}-"
        )
    )
    keep_temp = False

    try:
        downloaded_files = download_student_blobs(
            blobs=ordered_blobs,
            destination=temp_path,
            segment_c=segment_c,
        )

        if not downloaded_files:
            raise ValueError(
                f"Participant {participant_path} "
                "không download được WebM chunk nào để concatenate."
            )

        print(
            f"[{segment_c}] "
            f"Downloaded chunks: {len(downloaded_files)}/{len(ordered_blobs)}"
        )

        try:
            output_file = concatenate_webm_files(
                files=downloaded_files,
                output_dir=temp_path,
                output_name=f"{segment_c}.webm",
            )
        except RuntimeError as error:
            keep_temp = keep_temp_on_failure

            print(
                f"[{segment_c}] "
                f"FFmpeg error:\n{error}"
            )

            invalid_webm_backlog.append(
                "\n".join(
                    [
                        f"participant_path={participant_path}",
                        f"segment_c={segment_c}",
                        "reason=ffmpeg_concatenate_failed",
                        f"temp_path={temp_path}",
                        f"keep_temp_on_failure={keep_temp_on_failure}",
                        f"error={error}",
                        "downloaded_files=",
                        *[
                            str(file_path)
                            for file_path in downloaded_files
                        ],
                    ]
                )
            )

            print(
                f"[{segment_c}] "
                "Skipped whole student because FFmpeg failed."
            )

            return None

        if not keep_all_concatenated_webm_files:
            if not keep_all_webm_chunks:
                delete_downloaded_webm_chunks(
                    files=downloaded_files,
                    segment_c=segment_c,
                )

            print(
                f"[{segment_c}] "
                "Concatenated WebM was not saved "
                "because keep_all_concatenated_webm_files=false"
            )
            return None

        if environment == "local":
            output_path = save_video_local(
                participant_path=participant_path,
                source_file=output_file,
            )
        elif environment == "container":
            output_path = save_video_to_gcs(
                bucket=bucket,
                participant_path=participant_path,
                source_file=output_file,
            )
        else:
            raise ValueError(
                f"Environment không hợp lệ: {environment}"
            )

        if not keep_all_webm_chunks:
            delete_downloaded_webm_chunks(
                files=downloaded_files,
                segment_c=segment_c,
            )

        return output_path
    except Exception:
        keep_temp = keep_temp_on_failure
        raise
    finally:
        if keep_temp:
            print(
                f"[{segment_c}] "
                f"Kept temp directory for debugging: {temp_path}"
            )
        else:
            shutil.rmtree(
                temp_path,
                ignore_errors=True,
            )


def concatenate_session_video(
    *,
    bucket: Bucket,
    session_path: str,
    blobs: list[Blob],
    environment: str,
    keep_all_webm_chunks: bool,
    keep_all_concatenated_webm_files: bool,
    invalid_webm_backlog: list[str],
    keep_temp_on_failure: bool,
) -> SessionProcessingResult:
    """
    Ghép WebM theo buổi.

    Luôn ghép từng participant/segment_c trước.

    Nếu có bất kỳ participant nào fail, tạo thêm 2 video cấp session:
    - <segment_b>_from_c.webm từ các video segment_c đã ghép thành công.
    - <segment_b>.webm từ toàn bộ chunks đã download trong session.
    """

    if not blobs:
        raise ValueError(
            f"Session {session_path} không có blob nào."
        )

    segment_b = session_path.split("/")[-1]
    ordered_blobs = sorted(
        blobs,
        key=lambda blob: blob.name or "",
    )

    print()
    print(f"=== Processing session {segment_b} ===")
    print(f"Chunks: {len(ordered_blobs)}")

    temp_path = Path(
        tempfile.mkdtemp(
            prefix=f"session-{segment_b}-"
        )
    )
    keep_temp = False
    output_paths: list[str] = []
    failed_participants = 0
    failed_session_outputs = 0

    try:
        participant_outputs: list[Path] = []
        downloaded_chunk_files: list[Path] = []

        for participant_path, participant_blobs in group_blobs_by_participant(
            ordered_blobs
        ):
            segment_c = participant_path.split("/")[-1]
            participant_temp_path = (
                temp_path /
                "participants" /
                segment_c
            )

            print(
                f"[{segment_b}] "
                f"Concatenating participant {segment_c}"
            )

            downloaded_files = download_student_blobs(
                blobs=participant_blobs,
                destination=participant_temp_path,
                segment_c=segment_c,
            )

            try:
                participant_output = concatenate_participant_from_files(
                    files=downloaded_files,
                    temp_path=temp_path,
                    segment_c=segment_c,
                )
            except RuntimeError as error:
                failed_participants += 1
                keep_temp = keep_temp_on_failure

                print(
                    f"[{segment_c}] "
                    f"FFmpeg error:\n{error}"
                )

                invalid_webm_backlog.append(
                    "\n".join(
                        [
                            f"participant_path={participant_path}",
                            f"session_path={session_path}",
                            f"segment_c={segment_c}",
                            f"segment_b={segment_b}",
                            "reason=participant_ffmpeg_concatenate_failed",
                            f"temp_path={temp_path}",
                            f"keep_temp_on_failure={keep_temp_on_failure}",
                            f"error={error}",
                            "downloaded_files=",
                            *[
                                str(file_path)
                                for file_path in downloaded_files
                            ],
                        ]
                    )
                )

                print(
                    f"[{segment_c}] "
                    "Skipped student because FFmpeg failed."
                )
            else:
                participant_outputs.append(participant_output)

                if keep_all_concatenated_webm_files:
                    output_paths.append(
                        save_video(
                            bucket=bucket,
                            environment=environment,
                            participant_path=participant_path,
                            source_file=participant_output,
                        )
                    )
                else:
                    print(
                        f"[{segment_c}] "
                        "Concatenated WebM was not saved "
                        "because keep_all_concatenated_webm_files=false"
                    )

            downloaded_chunk_files.extend(downloaded_files)

        if failed_participants > 0:
            session_outputs = create_session_outputs_after_participant_failure(
                bucket=bucket,
                session_path=session_path,
                segment_b=segment_b,
                temp_path=temp_path,
                participant_outputs=participant_outputs,
                downloaded_chunk_files=downloaded_chunk_files,
                environment=environment,
                keep_all_concatenated_webm_files=(
                    keep_all_concatenated_webm_files
                ),
                invalid_webm_backlog=invalid_webm_backlog,
                keep_temp_on_failure=keep_temp_on_failure,
            )

            output_paths.extend(session_outputs.output_paths)
            failed_session_outputs = session_outputs.failed_session_outputs

            if failed_session_outputs > 0:
                keep_temp = keep_temp_on_failure

        if (
            not keep_all_webm_chunks and
            not keep_temp
        ):
            delete_downloaded_webm_chunks(
                files=downloaded_chunk_files,
                segment_c=segment_b,
            )

        return SessionProcessingResult(
            output_paths=output_paths,
            failed_participants=failed_participants,
            failed_session_outputs=failed_session_outputs,
        )
    except Exception:
        keep_temp = keep_temp_on_failure
        raise
    finally:
        if keep_temp:
            print(
                f"[{segment_b}] "
                f"Kept temp directory for debugging: {temp_path}"
            )
        else:
            shutil.rmtree(
                temp_path,
                ignore_errors=True,
            )


def save_video(
    *,
    bucket: Bucket,
    environment: str,
    participant_path: str,
    source_file: Path,
) -> str:
    if environment == "local":
        return save_video_local(
            participant_path=participant_path,
            source_file=source_file,
        )

    if environment == "container":
        return save_video_to_gcs(
            bucket=bucket,
            participant_path=participant_path,
            source_file=source_file,
        )

    raise ValueError(
        f"Environment không hợp lệ: {environment}"
    )


def save_session_video(
    *,
    bucket: Bucket,
    environment: str,
    session_path: str,
    source_file: Path,
    output_name: str,
) -> str:
    if environment == "local":
        return save_session_video_local(
            session_path=session_path,
            source_file=source_file,
            output_name=output_name,
        )

    if environment == "container":
        return save_session_video_to_gcs(
            bucket=bucket,
            session_path=session_path,
            source_file=source_file,
            output_name=output_name,
        )

    raise ValueError(
        f"Environment không hợp lệ: {environment}"
    )


def concatenate_participant_from_files(
    *,
    files: list[Path],
    temp_path: Path,
    segment_c: str,
) -> Path:
    if not files:
        raise RuntimeError(
            f"Participant {segment_c} không có chunk nào đã download."
        )

    participant_output_dir = (
        temp_path /
        "participant-concatenated"
    )
    participant_output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    return concatenate_webm_files(
        files=files,
        output_dir=participant_output_dir,
        output_name=f"{segment_c}.webm",
        prefer_byte_concat=True,
    )


def create_session_outputs_after_participant_failure(
    *,
    bucket: Bucket,
    session_path: str,
    segment_b: str,
    temp_path: Path,
    participant_outputs: list[Path],
    downloaded_chunk_files: list[Path],
    environment: str,
    keep_all_concatenated_webm_files: bool,
    invalid_webm_backlog: list[str],
    keep_temp_on_failure: bool,
) -> SessionProcessingResult:
    output_paths: list[str] = []
    failed_session_outputs = 0

    if participant_outputs:
        try:
            from_c_output = concatenate_webm_files(
                files=participant_outputs,
                output_dir=temp_path,
                output_name=f"{segment_b}_from_c.webm",
            )
        except RuntimeError as error:
            failed_session_outputs += 1
            record_session_output_failure(
                invalid_webm_backlog=invalid_webm_backlog,
                session_path=session_path,
                segment_b=segment_b,
                output_name=f"{segment_b}_from_c.webm",
                reason="session_from_c_concatenate_failed",
                temp_path=temp_path,
                keep_temp_on_failure=keep_temp_on_failure,
                error=error,
                files=participant_outputs,
            )
        else:
            if keep_all_concatenated_webm_files:
                output_paths.append(
                    save_session_video(
                        bucket=bucket,
                        environment=environment,
                        session_path=session_path,
                        source_file=from_c_output,
                        output_name=f"{segment_b}_from_c.webm",
                    )
                )
            else:
                print(
                    f"[{segment_b}] "
                    "Session WebM from segment_c was not saved "
                    "because keep_all_concatenated_webm_files=false"
                )
    else:
        failed_session_outputs += 1
        record_session_output_failure(
            invalid_webm_backlog=invalid_webm_backlog,
            session_path=session_path,
            segment_b=segment_b,
            output_name=f"{segment_b}_from_c.webm",
            reason="session_from_c_has_no_successful_participants",
            temp_path=temp_path,
            keep_temp_on_failure=keep_temp_on_failure,
            error=RuntimeError(
                "Không có video segment_c nào ghép thành công."
            ),
            files=[],
        )

    try:
        direct_output = concatenate_webm_files(
            files=downloaded_chunk_files,
            output_dir=temp_path,
            output_name=f"{segment_b}.webm",
            prefer_byte_concat=True,
        )
    except RuntimeError as error:
        failed_session_outputs += 1
        record_session_output_failure(
            invalid_webm_backlog=invalid_webm_backlog,
            session_path=session_path,
            segment_b=segment_b,
            output_name=f"{segment_b}.webm",
            reason="session_direct_concatenate_failed",
            temp_path=temp_path,
            keep_temp_on_failure=keep_temp_on_failure,
            error=error,
            files=downloaded_chunk_files,
        )
    else:
        if keep_all_concatenated_webm_files:
            output_paths.append(
                save_session_video(
                    bucket=bucket,
                    environment=environment,
                    session_path=session_path,
                    source_file=direct_output,
                    output_name=f"{segment_b}.webm",
                )
            )
        else:
            print(
                f"[{segment_b}] "
                "Direct session WebM was not saved "
                "because keep_all_concatenated_webm_files=false"
            )

    return SessionProcessingResult(
        output_paths=output_paths,
        failed_participants=0,
        failed_session_outputs=failed_session_outputs,
    )


def record_session_output_failure(
    *,
    invalid_webm_backlog: list[str],
    session_path: str,
    segment_b: str,
    output_name: str,
    reason: str,
    temp_path: Path,
    keep_temp_on_failure: bool,
    error: RuntimeError,
    files: list[Path],
) -> None:
    print(
        f"[{segment_b}] "
        f"Failed to create {output_name}:\n{error}"
    )

    invalid_webm_backlog.append(
        "\n".join(
            [
                f"session_path={session_path}",
                f"segment_b={segment_b}",
                f"output_name={output_name}",
                f"reason={reason}",
                f"temp_path={temp_path}",
                f"keep_temp_on_failure={keep_temp_on_failure}",
                f"error={error}",
                "files=",
                *[
                    str(file_path)
                    for file_path in files
                ],
            ]
        )
    )


def group_blobs_by_participant(
    blobs: list[Blob],
) -> Iterator[tuple[str, list[Blob]]]:
    current_participant_path: str | None = None
    current_blobs: list[Blob] = []

    for blob in sorted(blobs, key=lambda item: item.name or ""):
        parts = get_blob_name(blob).split("/")
        participant_path = "/".join(parts[:5])

        if current_participant_path is None:
            current_participant_path = participant_path
        elif participant_path != current_participant_path:
            yield (
                current_participant_path,
                current_blobs,
            )

            current_participant_path = participant_path
            current_blobs = []

        current_blobs.append(blob)

    if current_participant_path is not None and current_blobs:
        yield (
            current_participant_path,
            current_blobs,
        )


def download_student_blobs(
    *,
    blobs: list[Blob],
    destination: Path,
    segment_c: str,
) -> list[Path]:
    downloaded_files: list[Path] = []

    destination.mkdir(
        parents=True,
        exist_ok=True,
    )

    for index, blob in enumerate(blobs):
        blob_name = get_blob_name(blob)
        original_file_name = blob_name.rsplit("/", 1)[-1]

        # Giữ tên chunk gốc để debug lỗi FFmpeg.
        destination_file = (
            destination /
            original_file_name
        )

        print(
            f"[{segment_c}] "
            f"Downloading {index + 1}/{len(blobs)}: "
            f"{blob_name}"
        )

        blob.download_to_filename(
            str(destination_file)
        )

        downloaded_files.append(
            destination_file
        )

    return downloaded_files


def delete_downloaded_webm_chunks(
    *,
    files: list[Path],
    segment_c: str,
) -> None:
    deleted_files = 0

    for file_path in files:
        if not delete_file_if_exists(file_path):
            continue

        deleted_files += 1

    print(
        f"[{segment_c}] "
        f"Deleted downloaded WebM chunks from temp: {deleted_files}"
    )


def delete_file_if_exists(file_path: Path) -> bool:
    try:
        file_path.unlink()
    except FileNotFoundError:
        return False

    return True


def concatenate_webm_files(
    *,
    files: list[Path],
    output_dir: Path,
    output_name: str,
    prefer_byte_concat: bool = False,
) -> Path:
    if not files:
        raise ValueError(
            "Không có WebM file nào để concatenate."
        )

    output_file = output_dir / output_name

    if prefer_byte_concat:
        try:
            return concatenate_webm_files_by_bytes_and_remux(
                files=files,
                output_file=output_file,
            )
        except RuntimeError as byte_concat_error:
            print(
                "Byte concat failed. "
                "Falling back to FFmpeg concat demuxer..."
            )

            try:
                return concatenate_webm_files_with_demuxer(
                    files=files,
                    output_dir=output_dir,
                    output_file=output_file,
                )
            except RuntimeError as concat_demuxer_error:
                raise RuntimeError(
                    "FFmpeg concatenate failed.\n\n"
                    "Byte-concat remux stderr:\n"
                    f"{byte_concat_error}\n\n"
                    "Concat demuxer stderr:\n"
                    f"{concat_demuxer_error}"
                ) from concat_demuxer_error

    try:
        return concatenate_webm_files_with_demuxer(
            files=files,
            output_dir=output_dir,
            output_file=output_file,
        )
    except RuntimeError as concat_demuxer_error:
        print(
            "FFmpeg concat demuxer failed. "
            "Falling back to byte concat for MediaRecorder chunks..."
        )

        try:
            return concatenate_webm_files_by_bytes_and_remux(
                files=files,
                output_file=output_file,
            )
        except RuntimeError as byte_concat_error:
            raise RuntimeError(
                "FFmpeg concatenate failed.\n\n"
                "Concat demuxer stderr:\n"
                f"{concat_demuxer_error}\n\n"
                "Byte-concat remux stderr:\n"
                f"{byte_concat_error}"
            ) from byte_concat_error


def concatenate_webm_files_with_demuxer(
    *,
    files: list[Path],
    output_dir: Path,
    output_file: Path,
) -> Path:
    concat_manifest = output_dir / "concat.txt"

    with concat_manifest.open(
        "w",
        encoding="utf-8",
    ) as manifest:
        for file_path in files:
            # as_posix() giúp FFmpeg làm việc tốt hơn,
            # đặc biệt khi chạy local trên Windows.
            resolved_path = (
                file_path
                .resolve()
                .as_posix()
                .replace("'", "'\\''")
            )

            manifest.write(
                f"file '{resolved_path}'\n"
            )

    command = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_manifest),
        "-c",
        "copy",
        str(output_file),
    ]

    print("Running FFmpeg concat demuxer...")

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    if result.returncode == 0 and output_file.exists():
        return output_file

    raise RuntimeError(result.stderr)


def concatenate_webm_files_by_bytes_and_remux(
    *,
    files: list[Path],
    output_file: Path,
) -> Path:
    delete_file_if_exists(output_file)
    raw_output_file = (
        output_file.parent /
        f"{output_file.stem}.raw{output_file.suffix}"
    )

    concatenate_files_by_bytes(
        files=files,
        output_file=raw_output_file,
    )

    remux_command = [
        "ffmpeg",
        "-y",
        "-i",
        str(raw_output_file),
        "-c",
        "copy",
        str(output_file),
    ]

    print("Running FFmpeg remux on byte-concatenated WebM...")

    remux_result = subprocess.run(
        remux_command,
        capture_output=True,
        text=True,
    )

    if remux_result.returncode == 0 and output_file.exists():
        delete_file_if_exists(raw_output_file)
        return output_file

    raise RuntimeError(remux_result.stderr)


def concatenate_files_by_bytes(
    *,
    files: list[Path],
    output_file: Path,
) -> None:
    with output_file.open("wb") as output:
        for file_path in files:
            if not file_path.exists():
                raise RuntimeError(
                    f"Downloaded chunk không tồn tại: {file_path}"
                )

            with file_path.open("rb") as chunk:
                shutil.copyfileobj(
                    chunk,
                    output,
                    length=1024 * 1024,
                )

    if not output_file.exists():
        raise RuntimeError(
            f"Không tạo được byte-concat output: {output_file}"
        )


def save_video_local(
    *,
    participant_path: str,
    source_file: Path,
) -> str:
    """
    Local:

    output/
      captures/
        <a>/
          <b>/
            participants/
              <c>/
                concatenated/
                  <c>.webm
    """

    segment_c = participant_path.split("/")[-1]

    output_directory = (
        LOCAL_OUTPUT_ROOT
        / participant_path
        / "concatenated"
    )

    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_file = (
        output_directory /
        f"{segment_c}.webm"
    )

    shutil.copy2(
        source_file,
        output_file,
    )

    print(
        f"[{segment_c}] "
        f"Saved locally: {output_file}"
    )

    return str(output_file)


def save_video_to_gcs(
    *,
    bucket: Bucket,
    participant_path: str,
    source_file: Path,
) -> str:
    """
    Container:

    captures/
      [segment_a]/
        [segment_b]/
          participants/
            [segment_c]/
              video/
              concatenated/
                [segment_c].webm
    """

    segment_c = participant_path.split("/")[-1]

    output_blob_name = (
        f"{participant_path}/"
        f"concatenated/"
        f"{segment_c}.webm"
    )

    output_blob = bucket.blob(
        output_blob_name
    )

    output_blob.upload_from_filename(
        str(source_file),
        content_type="video/webm",
    )

    gcs_uri = (
        f"gs://{bucket.name}/"
        f"{output_blob_name}"
    )

    print(
        f"[{segment_c}] "
        f"Uploaded: {gcs_uri}"
    )

    return gcs_uri


def save_session_video_local(
    *,
    session_path: str,
    source_file: Path,
    output_name: str,
) -> str:
    """
    Local:

    output/
      captures/
        <segment_a>/
          <segment_b>/
            concatenated/
              <output_name>
    """

    segment_b = session_path.split("/")[-1]

    output_directory = (
        LOCAL_OUTPUT_ROOT /
        session_path /
        "concatenated"
    )

    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_file = (
        output_directory /
        output_name
    )

    shutil.copy2(
        source_file,
        output_file,
    )

    print(
        f"[{segment_b}] "
        f"Saved session locally: {output_file}"
    )

    return str(output_file)


def save_session_video_to_gcs(
    *,
    bucket: Bucket,
    session_path: str,
    source_file: Path,
    output_name: str,
) -> str:
    """
    Container:

    captures/
      <segment_a>/
        <segment_b>/
          concatenated/
            <output_name>
    """

    segment_b = session_path.split("/")[-1]

    output_blob_name = (
        f"{session_path}/"
        f"concatenated/"
        f"{output_name}"
    )

    output_blob = bucket.blob(
        output_blob_name
    )

    output_blob.upload_from_filename(
        str(source_file),
        content_type="video/webm",
    )

    gcs_uri = (
        f"gs://{bucket.name}/"
        f"{output_blob_name}"
    )

    print(
        f"[{segment_b}] "
        f"Uploaded session: {gcs_uri}"
    )

    return gcs_uri
