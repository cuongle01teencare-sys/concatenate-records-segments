import traceback

from datetime import datetime

from google.cloud import storage

from src.utils.logging_utils import write_log
from src.processing import (
    profile_data,
    iter_session_groups,
    session_has_non_empty_concatenated_webm,
    concatenate_session_video,
)

# CLI:
#   uv run python main.py --concat   -> traverse MATCH_GLOB blobs and concatenate
#   uv run python main.py --sync     -> traverse SUPABASE_SYNC_MATCH_GLOB blobs and
#                                        sync record_link into Supabase

# Sync feature logic:
# Với mỗi session đã có concatenated session-level .webm video.
# Nếu có nhiều hơn hai .webm được tạo ra từ source (thường là b_from_c và direct b)
# thì ưu tiên chọn streaming link của video có dung lượng lớn hơn.
# Sau khi chọn final streaming link, lấy segment_a (google meet code) để ghép thành
# một google meet url hợp lệ.
# Tìm trong hai bảng kns_class_sessions và sessions record nào có meeting_url bằng
# google meet url đó VÀ:
#   1. kns_class_sessions: session_date bằng session_date_from_segment_b
#   2. sessions: scheduled_start (theo ngày) bằng session_date_from_segment_b
# session_date_from_segment_b trích từ segment_b dạng
# "session-<start_date>T<start_time>Z<suffix>".
# Nếu đúng 1 record khớp và record_link đang trống -> ghi record_link.
# Nếu record_link đã có -> backlog (thư mục record_link_already_set).
# Nếu >= 2 record khớp -> backlog (thư mục multiple_matches).


def safe_write_log(
    *,
    environment: str,
    bucket,
    path: str,
    data: str,
) -> bool:
    try:
        write_log(
            environment=environment,
            bucket=bucket,
            path=path,
            data=data,
        )
    except Exception as error:
        print(
            "Failed to write log. "
            f"path={path}"
        )
        print(
            traceback.format_exception_only(
                type(error),
                error,
            )[-1].strip()
        )
        return False

    return True


def write_invalid_webm_backlog(
    *,
    environment: str,
    bucket,
    path: str,
    entries: list[str],
) -> None:
    if not entries:
        return

    safe_write_log(
        environment=environment,
        bucket=bucket,
        path=path,
        data=(
            "\n\n---\n\n".join(entries)
            + "\n"
        ),
    )


def print_output_paths(output_paths: list[str]) -> None:
    if output_paths:
        for output_path in output_paths:
            print(
                f"Output: {output_path}"
            )
        return

    print("Output: none created")


def main(
    *,
    project_id: str,
    bucket_name: str,
    environment: str,
    data_profiling: bool,
    match_glob: str,
    keep_all_webm_chunks: bool,
    keep_all_concatenated_webm_files: bool,
    keep_temp_on_failure: bool,
    return_existing_session_outputs: bool = False,
) -> list[str]:
    started_at = datetime.now()

    now_date_str = started_at.strftime("%Y-%m-%d")
    now_time_str = started_at.strftime("%H-%M-%S")

    client = storage.Client(
        project=project_id,
    )

    bucket = client.bucket(
        bucket_name,
    )

    if data_profiling:
        profile_data(bucket)
        return []

    should_process_but_no: list[str] = []
    should_not_process_but_yes: list[str] = []
    invalid_webm_backlog_entries: list[str] = []
    all_output_paths: list[str] = []

    processed_items = 0
    skipped_existing_sessions = 0
    skipped_invalid_items = 0
    failed_session_outputs = 0

    invalid_webm_backlog_path = (
        f"backlogs/invalid_webm_chunks/"
        f"{now_date_str}/"
        f"{now_time_str}/"
        f"skipped_sessions.log"
    )

    try:
        groups = iter_session_groups(
            bucket=bucket,
            should_process_but_no=should_process_but_no,
            should_not_process_but_yes=should_not_process_but_yes,
            match_glob=match_glob,
        )

        for session_path, blobs in groups:
            label = session_path.split("/")[-1]

            if session_has_non_empty_concatenated_webm(
                bucket=bucket,
                session_path=session_path,
            ):
                skipped_existing_sessions += 1
                if return_existing_session_outputs:
                    # API muốn nhận URI dù session đã ghép sẵn: liệt kê các
                    # video CẤP SESSION đang tồn tại tại <session>/concatenated/.
                    existing_prefix = (
                        f"{session_path.rstrip('/')}/concatenated/"
                    )
                    for existing in bucket.list_blobs(prefix=existing_prefix):
                        existing_name = existing.name or ""
                        if existing_name.lower().endswith(".webm"):
                            all_output_paths.append(
                                f"gs://{bucket.name}/{existing_name}"
                            )
                continue

            print()
            print(
                f"Collected all WebM chunks for "
                f"session {label}"
            )

            print(
                f"Total chunks: {len(blobs)}"
            )

            backlog_count_before = len(
                invalid_webm_backlog_entries
            )

            result = concatenate_session_video(
                bucket=bucket,
                session_path=session_path,
                blobs=blobs,
                environment=environment,
                keep_all_webm_chunks=keep_all_webm_chunks,
                keep_all_concatenated_webm_files=(
                    keep_all_concatenated_webm_files
                ),
                invalid_webm_backlog=invalid_webm_backlog_entries,
                keep_temp_on_failure=keep_temp_on_failure,
            )

            if len(invalid_webm_backlog_entries) > backlog_count_before:
                skipped_invalid_items += result.failed_participants
                failed_session_outputs += result.failed_session_outputs

                write_invalid_webm_backlog(
                    environment=environment,
                    bucket=bucket,
                    path=invalid_webm_backlog_path,
                    entries=invalid_webm_backlog_entries,
                )

                print(
                    f"Session {label} had "
                    f"{result.failed_participants} skipped student(s)"
                )

            processed_items += 1
            all_output_paths.extend(result.output_paths)

            print(f"Finished session {label}")
            print_output_paths(result.output_paths)

    except Exception:
        error_time = datetime.now()

        error_date_str = error_time.strftime(
            "%Y-%m-%d"
        )

        error_time_str = error_time.strftime(
            "%H-%M-%S"
        )

        error = (
            f"[At {error_time_str}]\n"
            f"{traceback.format_exc()}\n"
        )

        print(error)

        error_log_name = (
            f"logs/"
            f"{error_date_str}/"
            f"{error_time_str}"
            f"-records-concatenation-error.log"
        )

        safe_write_log(
            environment=environment,
            bucket=bucket,
            path=error_log_name,
            data=error,
        )

        write_invalid_webm_backlog(
            environment=environment,
            bucket=bucket,
            path=invalid_webm_backlog_path,
            entries=invalid_webm_backlog_entries,
        )

        raise

    write_invalid_webm_backlog(
        environment=environment,
        bucket=bucket,
        path=invalid_webm_backlog_path,
        entries=invalid_webm_backlog_entries,
    )

    # ---------------------------------------
    # Records leakage report
    # ---------------------------------------

    records_leakage_prefix = (
        f"records_leakage/"
        f"{now_date_str}/"
        f"{now_time_str}"
    )

    false_negatives = "\n".join(
        should_process_but_no
    )

    false_positives = "\n".join(
        should_not_process_but_yes
    )

    if len(should_process_but_no) > 0:
        safe_write_log(
            environment=environment,
            bucket=bucket,
            path=(
                f"{records_leakage_prefix}/"
                f"false_negatives.log"
            ),
            data=false_negatives,
        )

    if len(should_not_process_but_yes) > 0:
        safe_write_log(
            environment=environment,
            bucket=bucket,
            path=(
                f"{records_leakage_prefix}/"
                f"false_positives.log"
            ),
            data=false_positives,
        )

    print()
    print("==============================")
    print("Processing completed")
    print(
        "Sessions processed: "
        f"{processed_items}"
    )
    print(
        "Sessions skipped because concatenated WebM already exists: "
        f"{skipped_existing_sessions}"
    )
    print(
        "Sessions skipped because of invalid WebM chunks: "
        f"{skipped_invalid_items}"
    )
    print(
        "Session-level outputs failed: "
        f"{failed_session_outputs}"
    )
    print("==============================")

    return all_output_paths


def run_sync(
    *,
    project_id: str,
    bucket_name: str,
    environment: str,
    supabase_api_url: str,
    supabase_api_key: str,
    supabase_sync_match_glob: str,
) -> None:
    print("Running Supabase record sync")

    if not (supabase_api_url and supabase_api_key):
        print(
            "Skipped Supabase record sync because Supabase config is missing "
            "(SUPABASE_API_URL / SUPABASE_API_KEY)."
        )
        return

    from supabase import create_client
    from src.supabase import supabase_record_sync

    client = storage.Client(
        project=project_id,
    )

    bucket = client.bucket(
        bucket_name,
    )

    supabase_client = create_client(
        supabase_api_url,
        supabase_api_key,
    )

    supabase_record_sync(
        bucket,
        supabase_client,
        supabase_sync_match_glob,
        environment,
    )


def build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="concatenate-records-segments",
        description=(
            "Concatenate session recordings (--concat) or sync record links "
            "into Supabase (--sync)."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--concat",
        action="store_true",
        help="Traverse blobs by MATCH_GLOB and concatenate session videos.",
    )
    group.add_argument(
        "--sync",
        action="store_true",
        help=(
            "Traverse blobs by SUPABASE_SYNC_MATCH_GLOB and sync record_link "
            "into Supabase."
        ),
    )
    return parser


if __name__ == "__main__":
    from src.config import (
        PROJECT_ID,
        BUCKET_NAME,
        ENVIRONMENT,
        MATCH_GLOB,
        DATA_PROFILING,
        KEEP_ALL_WEBM_CHUNKS,
        KEEP_ALL_CONCATENATED_WEBM_FILES,
        KEEP_TEMP_ON_FAILURE,
        SUPABASE_API_URL,
        SUPABASE_API_KEY,
        SUPABASE_SYNC_MATCH_GLOB,
    )

    args = build_arg_parser().parse_args()

    if args.sync:
        run_sync(
            project_id=PROJECT_ID,
            bucket_name=BUCKET_NAME,
            environment=ENVIRONMENT,
            supabase_api_url=SUPABASE_API_URL,
            supabase_api_key=SUPABASE_API_KEY,
            supabase_sync_match_glob=SUPABASE_SYNC_MATCH_GLOB,
        )
    else:
        print("Running concatenation")
        main(
            project_id=PROJECT_ID,
            bucket_name=BUCKET_NAME,
            environment=ENVIRONMENT,
            data_profiling=DATA_PROFILING,
            match_glob=MATCH_GLOB,
            keep_all_webm_chunks=KEEP_ALL_WEBM_CHUNKS,
            keep_all_concatenated_webm_files=(
                KEEP_ALL_CONCATENATED_WEBM_FILES
            ),
            keep_temp_on_failure=KEEP_TEMP_ON_FAILURE,
        )
