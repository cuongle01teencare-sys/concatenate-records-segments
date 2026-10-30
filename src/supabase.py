import json
import re

from datetime import datetime
from typing import Any, cast

from google.cloud.storage.blob import Blob
from google.cloud.storage.bucket import Bucket
from supabase import Client

from src.processing import is_session_audio_output
from src.utils.logging_utils import write_log
from src.utils.storage_utils import upload_from_string


# Idempotency marker dropped into a session's concatenated/ folder after its
# record_link has been synced, so later --sync runs skip it.
SYNC_MARKER_FILENAME = "sync.txt"


SESSION_SEGMENT_PATTERN = re.compile(
    r"^session-"
    r"(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:T(?P<time>\d{2}-\d{2}-\d{2}))?"
)


def get_blob_size(blob: Blob) -> int:
    if blob.size is None:
        blob.reload()

    return blob.size or 0


def get_session_date_from_segment_b(segment_b: str) -> str:
    match = SESSION_SEGMENT_PATTERN.search(segment_b)

    if match is None:
        raise ValueError(
            f"Cannot parse session date from segment_b={segment_b}"
        )

    return match.group("date")


def get_session_time_from_segment_b(segment_b: str) -> str | None:
    match = SESSION_SEGMENT_PATTERN.search(segment_b)

    if match is None or not match.group("time"):
        return None

    # "12-49-19" -> "12:49:19"
    return match.group("time").replace("-", ":")


def get_session_video_record_link(
    *,
    bucket: Bucket,
    blob_name: str,
) -> str:
    # Store the canonical gs:// URI (private, non-expiring, access-policy
    # agnostic). The viewing app mints a signed URL from this at view time.
    return f"gs://{bucket.name}/{blob_name}"


def get_concatenated_prefix(blob_name: str) -> str:
    # blob_name: captures/<segment_a>/<segment_b>/concatenated/<file>.webm
    # -> captures/<segment_a>/<segment_b>/concatenated/
    parts = blob_name.split("/")
    return "/".join(parts[:4]) + "/"


def get_sync_marker_name(blob_name: str) -> str:
    return f"{get_concatenated_prefix(blob_name)}{SYNC_MARKER_FILENAME}"


def sync_marker_exists(
    *,
    bucket: Bucket,
    blob_name: str,
) -> bool:
    return bucket.blob(get_sync_marker_name(blob_name)).exists()


def write_sync_marker(
    *,
    bucket: Bucket,
    blob_name: str,
    payload: str,
) -> str:
    marker_name = get_sync_marker_name(blob_name)
    upload_from_string(bucket, marker_name, payload)
    return marker_name


def scheduled_start_date_matches(
    *,
    scheduled_start: object,
    session_date: str,
) -> bool:
    if scheduled_start is None:
        return False

    normalized_scheduled_start = (
        str(scheduled_start)
        .replace("T", " ")
    )

    return normalized_scheduled_start.startswith(session_date)


def select_largest_session_videos(
    *,
    bucket: Bucket,
    single_match_glob: str,
) -> dict[str, tuple[Blob, int]]:
    session_videos = bucket.list_blobs(
        prefix="captures/",
        match_glob=single_match_glob,
    )
    selected_by_session: dict[str, tuple[Blob, int]] = {}

    for session_video in session_videos:
        blob_name = session_video.name or ""
        name_parts = blob_name.split("/")

        if (
            len(name_parts) < 5 or
            name_parts[0] != "captures" or
            name_parts[3] != "concatenated" or
            not blob_name.lower().endswith(".webm")
        ):
            print(
                "Skipped non session-level concatenated WebM: "
                f"{blob_name}"
            )
            continue

        if is_session_audio_output(blob_name):
            # File audio ghép (shared_audio / mentor_audio) không phải video
            # của session -> không được chọn làm record_link.
            continue

        blob_size = get_blob_size(session_video)

        if blob_size <= 0:
            print(
                "Skipped empty session-level concatenated WebM: "
                f"{blob_name}"
            )
            continue

        session_path = "/".join(name_parts[:3])
        selected = selected_by_session.get(session_path)

        if selected is None or blob_size > selected[1]:
            selected_by_session[session_path] = (
                session_video,
                blob_size,
            )

    return selected_by_session


def normalize_supabase_rows(data: object) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    if not isinstance(data, list):
        return rows

    for row in data:
        if isinstance(row, dict):
            rows.append(cast(dict[str, Any], row))

    return rows


def get_meet_code_from_url(meet_url: str) -> str:
    return meet_url.rstrip("/").split("/")[-1]


def summarize_supabase_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []

    for row in rows[:10]:
        summary.append(
            {
                "id": row.get("id"),
                "meeting_url": row.get("meeting_url"),
                "session_date": row.get("session_date"),
                "scheduled_start": row.get("scheduled_start"),
                "start_at": row.get("start_at"),
                "record_link": row.get("record_link"),
            }
        )

    return summary


def debug_fetch_supabase_rows_by_meet_code(
    *,
    supabase_client: Client,
    table_name: str,
    meet_code: str,
) -> list[dict[str, Any]]:
    print(
        "[supabase] Exact meeting_url returned 0 rows; "
        "debug fetching rows by meeting_url ilike: "
        f"table={table_name}; meet_code={meet_code}"
    )

    response = (
        supabase_client
        .table(table_name)
        .select("*")
        .ilike("meeting_url", f"%{meet_code}%")
        .limit(10)
        .execute()
    )

    rows = normalize_supabase_rows(response.data)

    print(
        "[supabase] Debug ilike candidates: "
        f"table={table_name}; count={len(rows)}; "
        f"rows={json.dumps(summarize_supabase_rows(rows), default=str)}"
    )

    return rows


def fetch_supabase_rows_by_meeting_url(
    *,
    supabase_client: Client,
    table_name: str,
    meet_url: str,
) -> list[dict[str, Any]]:
    print(
        "[supabase] Fetching rows by meeting_url: "
        f"table={table_name}; meeting_url={meet_url}"
    )

    response = (
        supabase_client
        .table(table_name)
        .select("*")
        .eq("meeting_url", meet_url)
        .execute()
    )

    rows = normalize_supabase_rows(response.data)

    print(
        "[supabase] Fetched candidates: "
        f"table={table_name}; count={len(rows)}"
    )

    if not rows:
        debug_fetch_supabase_rows_by_meet_code(
            supabase_client=supabase_client,
            table_name=table_name,
            meet_code=get_meet_code_from_url(meet_url),
        )

    return rows


def update_record_link(
    *,
    supabase_client: Client,
    table_name: str,
    row_id: Any,
    record_link: str,
) -> None:
    (
        supabase_client
        .table(table_name)
        .update({"record_link": record_link})
        .eq("id", row_id)
        .execute()
    )


def write_sync_backlog(
    *,
    environment: str,
    bucket: Bucket,
    category: str,
    reason: str,
    table_name: str,
    segment_a: str,
    segment_b: str,
    session_date: str,
    session_time: str | None,
    meet_url: str,
    selected_video: dict[str, Any],
    matches: list[dict[str, Any]],
) -> None:
    now = datetime.now()
    path = (
        "backlogs/"
        "supabase_sync/"
        f"{category}/"
        f"{now.strftime('%Y-%m-%d')}/"
        f"{now.strftime('%H-%M-%S')}/"
        f"meet_code={segment_a}/"
        f"session_date={session_date}/"
        f"segment_b={segment_b}/"
        f"{table_name}.json"
    )
    data = json.dumps(
        {
            "reason": reason,
            "category": category,
            "table_name": table_name,
            "segment_a": segment_a,
            "segment_b": segment_b,
            "session_date": session_date,
            "session_time": session_time,
            "meet_url": meet_url,
            "concatenated_blob_name": selected_video.get("blob_name"),
            "concatenated_gcs_uri": selected_video.get("gcs_uri"),
            "record_link": selected_video.get("record_link"),
            "selected_video": selected_video,
            "match_count": len(matches),
            "matches": matches,
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    )

    try:
        write_log(
            environment=environment,
            bucket=bucket,
            path=path,
            data=f"{data}\n",
        )
    except Exception as error:
        print(
            "Failed to write Supabase sync backlog. "
            f"category={category}; path={path}; error={error}"
        )
        return

    print(
        f"Wrote Supabase sync backlog [{category}]: {path}"
    )


def sync_table_record_link(
    *,
    environment: str,
    bucket: Bucket,
    supabase_client: Client,
    table_name: str,
    candidates: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    segment_a: str,
    segment_b: str,
    session_date: str,
    session_time: str | None,
    meet_url: str,
    selected_video: dict[str, Any],
) -> dict[str, Any]:
    record_link = selected_video["record_link"]
    result: dict[str, Any] = {
        "candidates_by_meeting_url": len(candidates),
        "match_count": len(matches),
        "matches": matches,
        "action": None,
        "updated_row_id": None,
    }

    if not matches:
        result["action"] = "no_match"
        print(
            f"[supabase] No date match for record_link write: table={table_name}"
        )
        return result

    # Same day but (likely) different time. We cannot reliably match the time
    # from segment_b against the DB, so treat 2+ matches as ambiguous.
    if len(matches) >= 2:
        write_sync_backlog(
            environment=environment,
            bucket=bucket,
            category="multiple_matches",
            reason="multiple_records_matched_same_meet_url_and_date",
            table_name=table_name,
            segment_a=segment_a,
            segment_b=segment_b,
            session_date=session_date,
            session_time=session_time,
            meet_url=meet_url,
            selected_video=selected_video,
            matches=matches,
        )
        result["action"] = "backlog_multiple_matches"
        return result

    row = matches[0]
    row_id = row.get("id")
    existing_record_link = row.get("record_link")

    if existing_record_link:
        write_sync_backlog(
            environment=environment,
            bucket=bucket,
            category="record_link_already_set",
            reason="record_link_already_present_not_overwritten",
            table_name=table_name,
            segment_a=segment_a,
            segment_b=segment_b,
            session_date=session_date,
            session_time=session_time,
            meet_url=meet_url,
            selected_video=selected_video,
            matches=matches,
        )
        result["action"] = "backlog_already_set"
        return result

    update_record_link(
        supabase_client=supabase_client,
        table_name=table_name,
        row_id=row_id,
        record_link=record_link,
    )
    result["action"] = "updated"
    result["updated_row_id"] = row_id

    print(
        "[supabase] Updated record_link: "
        f"table={table_name}; id={row_id}; record_link={record_link}"
    )

    return result


def supabase_record_sync(
    bucket: Bucket,
    supabase_client: Client,
    single_match_glob: str,
    environment: str,
) -> None:
    selected_by_session = select_largest_session_videos(
        bucket=bucket,
        single_match_glob=single_match_glob,
    )

    print(
        "Supabase record sync session videos found: "
        f"{len(selected_by_session)}"
    )

    total_sessions = len(selected_by_session)

    for index, (
        session_path,
        (session_video, blob_size),
    ) in enumerate(selected_by_session.items(), start=1):
        blob_name = session_video.name or ""
        name_parts = blob_name.split("/")
        segment_a = name_parts[1]
        segment_b = name_parts[2]

        print(
            "[supabase] Processing session video "
            f"{index}/{total_sessions}: {blob_name} ({blob_size} bytes)"
        )

        # Idempotency: skip sessions already synced (sync.txt present in the
        # session's concatenated/ folder).
        if sync_marker_exists(bucket=bucket, blob_name=blob_name):
            print(
                "[supabase] Skipping already-synced session "
                f"(found {get_sync_marker_name(blob_name)})"
            )
            continue

        try:
            session_date = get_session_date_from_segment_b(
                segment_b
            )
        except ValueError as error:
            print(f"Skipped Supabase sync for {blob_name}: {error}")
            continue

        session_time = get_session_time_from_segment_b(segment_b)

        meet_url = f"https://meet.google.com/{segment_a}"
        record_link = get_session_video_record_link(
            bucket=bucket,
            blob_name=blob_name,
        )
        selected_video = {
            "blob_name": blob_name,
            "gcs_uri": f"gs://{bucket.name}/{blob_name}",
            "record_link": record_link,
            "size": blob_size,
        }

        print(
            "[supabase] Parsed session: "
            f"segment_a={segment_a}; segment_b={segment_b}; "
            f"session_date={session_date}; session_time={session_time}; "
            f"meet_url={meet_url}"
        )

        kns_class_session_candidates = fetch_supabase_rows_by_meeting_url(
            supabase_client=supabase_client,
            table_name="kns_class_sessions",
            meet_url=meet_url,
        )
        session_candidates = fetch_supabase_rows_by_meeting_url(
            supabase_client=supabase_client,
            table_name="sessions",
            meet_url=meet_url,
        )

        kns_class_session_matches = [
            row
            for row in kns_class_session_candidates
            if str(row.get("session_date")) == session_date
        ]
        session_matches = [
            row
            for row in session_candidates
            if scheduled_start_date_matches(
                scheduled_start=row.get("scheduled_start"),
                session_date=session_date,
            )
        ]

        print(
            "[supabase] Matched rows: "
            f"kns_class_sessions={len(kns_class_session_matches)}; "
            f"sessions={len(session_matches)}"
        )

        if kns_class_session_candidates and not kns_class_session_matches:
            print(
                "[supabase] kns_class_sessions rows matched meeting_url "
                "but not session_date. Candidate session_dates: "
                f"{[
                    row.get('session_date')
                    for row in kns_class_session_candidates
                ]}"
            )

        if session_candidates and not session_matches:
            print(
                "[supabase] sessions rows matched meeting_url "
                "but not scheduled_start date. Candidate scheduled_starts: "
                f"{[
                    row.get('scheduled_start')
                    for row in session_candidates
                ]}"
            )

        kns_result = sync_table_record_link(
            environment=environment,
            bucket=bucket,
            supabase_client=supabase_client,
            table_name="kns_class_sessions",
            candidates=kns_class_session_candidates,
            matches=kns_class_session_matches,
            segment_a=segment_a,
            segment_b=segment_b,
            session_date=session_date,
            session_time=session_time,
            meet_url=meet_url,
            selected_video=selected_video,
        )
        sessions_result = sync_table_record_link(
            environment=environment,
            bucket=bucket,
            supabase_client=supabase_client,
            table_name="sessions",
            candidates=session_candidates,
            matches=session_matches,
            segment_a=segment_a,
            segment_b=segment_b,
            session_date=session_date,
            session_time=session_time,
            meet_url=meet_url,
            selected_video=selected_video,
        )

        # Drop the idempotency marker only after a real record_link write.
        updated_row_ids = {
            "kns_class_sessions": (
                kns_result.get("updated_row_id")
                if kns_result.get("action") == "updated"
                else None
            ),
            "sessions": (
                sessions_result.get("updated_row_id")
                if sessions_result.get("action") == "updated"
                else None
            ),
        }
        did_write = (
            kns_result.get("action") == "updated"
            or sessions_result.get("action") == "updated"
        )
        marker_name = None

        if did_write:
            marker_payload = json.dumps(
                {
                    "synced_at": datetime.now().isoformat(),
                    "segment_a": segment_a,
                    "segment_b": segment_b,
                    "session_date": session_date,
                    "session_time": session_time,
                    "meet_url": meet_url,
                    "record_link": record_link,
                    "concatenated_blob_name": blob_name,
                    "updated_row_ids": updated_row_ids,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            try:
                marker_name = write_sync_marker(
                    bucket=bucket,
                    blob_name=blob_name,
                    payload=f"{marker_payload}\n",
                )
                print(f"[supabase] Wrote sync marker: {marker_name}")
            except Exception as error:
                print(
                    "Failed to write sync marker for "
                    f"{blob_name}: {error}"
                )

        print(
            json.dumps(
                {
                    "session_path": session_path,
                    "segment_a": segment_a,
                    "segment_b": segment_b,
                    "meet_url": meet_url,
                    "session_date": session_date,
                    "session_time": session_time,
                    "selected_video": selected_video,
                    "kns_class_sessions": kns_result,
                    "sessions": sessions_result,
                    "record_link_written": did_write,
                    "sync_marker": marker_name,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )


def find_kns_session_for_composite(
    *,
    supabase_client: Client,
    meet_code: str,
    vn_date: str,
) -> tuple[dict[str, Any] | None, str]:
    """Buổi KNS (chưa huỷ, chưa gộp) của Meet code trong ngày VN đó.

    Trả (row, "") khi có đúng một buổi, ngược lại (None, "no_match" | "multiple_matches").
    Khớp theo meet code nằm trong meeting_url vì cột này có nhiều dạng (http://, thiếu
    scheme, đuôi ?hs=..., tel.meet/...), so bằng nguyên URL https:// sẽ sót buổi.
    """
    response = (
        supabase_client
        .table("kns_class_sessions")
        .select("id,class_name,session_date,status,record_link,merged_into_session_id")
        .eq("session_date", vn_date)
        .ilike("meeting_url", f"*{meet_code}*")
        .execute()
    )
    rows = [
        row
        for row in normalize_supabase_rows(response.data)
        if not str(row.get("status") or "").lower().startswith("cancel")
        and not row.get("merged_into_session_id")
    ]
    if len(rows) != 1:
        print(
            "[supabase] composite: "
            f"{len(rows)} kns_class_sessions row(s) for {meet_code} on {vn_date}"
        )
        return None, "no_match" if not rows else "multiple_matches"
    return rows[0], ""


def composite_skip_reason(
    *,
    supabase_client: Client,
    meet_code: str,
    vn_date: str,
) -> str | None:
    """Lý do không cần dựng video cả buổi, None nếu cần dựng.

    Buổi đã có link Drive bản ghi Google Meet (Apps Script ghi) hay link CS dán tay thì
    không dựng: bản Meet có đủ mentor, slide và tiếng nên luôn tốt hơn. Chỉ dựng khi
    record_link đang trống hoặc đang là gs:// (video ghép cũ _from_c.webm / bản dựng trước).
    Không tìm được đúng một buổi thì cũng không dựng vì không có chỗ ghi link.
    """
    row, reason = find_kns_session_for_composite(
        supabase_client=supabase_client, meet_code=meet_code, vn_date=vn_date
    )
    if row is None:
        return reason
    current = row.get("record_link") or ""
    if current and not current.startswith("gs://"):
        print(
            "[supabase] composite: already has record_link, skip "
            f"id={row.get('id')} record_link={current}"
        )
        return "has_record_link"
    return None


def sync_composite_record_link(
    *,
    supabase_client: Client,
    meet_code: str,
    vn_date: str,
    record_link: str,
) -> str:
    """Ghi video dựng lại của cả buổi (src/composite.py) vào kns_class_sessions.

    Chỉ ghi khi record_link đang trống hoặc đang là gs:// (xem composite_skip_reason).
    Điều kiện trống/gs:// đi kèm câu UPDATE để không đè link Drive mà Apps Script vừa
    ghi trong lúc đang dựng.
    """
    row, reason = find_kns_session_for_composite(
        supabase_client=supabase_client, meet_code=meet_code, vn_date=vn_date
    )
    if row is None:
        return reason

    current = row.get("record_link") or ""
    if current == record_link:
        return "already"
    if current and not current.startswith("gs://"):
        print(
            "[supabase] composite: keep existing record_link "
            f"id={row.get('id')} record_link={current}"
        )
        return "kept_existing"

    response = (
        supabase_client
        .table("kns_class_sessions")
        .update({"record_link": record_link})
        .eq("id", row.get("id"))
        .or_("record_link.is.null,record_link.like.gs*")
        .execute()
    )
    if not normalize_supabase_rows(response.data):
        print(
            "[supabase] composite: record_link changed concurrently, not written "
            f"id={row.get('id')}"
        )
        return "changed_concurrently"

    print(
        "[supabase] composite: Updated record_link "
        f"id={row.get('id')} record_link={record_link} (was {current or 'empty'})"
    )
    return "updated"
