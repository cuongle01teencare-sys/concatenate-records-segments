import traceback

from datetime import datetime

from google.cloud import storage

from src.utils.logging_utils import write_log
from src.processing import (
    profile_data,
    iter_session_groups,
    concatenate_session_video,
)


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
) -> None:
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
        return

    should_process_but_no: list[str] = []
    should_not_process_but_yes: list[str] = []
    invalid_webm_backlog_entries: list[str] = []

    processed_items = 0
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
        "Sessions skipped because of invalid WebM chunks: "
        f"{skipped_invalid_items}"
    )
    print(
        "Session-level outputs failed: "
        f"{failed_session_outputs}"
    )
    print("==============================")


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
    )

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
