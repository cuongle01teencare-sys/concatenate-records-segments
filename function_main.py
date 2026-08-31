"""
HTTP entrypoint (functions-framework / Cloud Run Service, RUN_MODE=serve).

Nhận POST và chạy lại logic của main.py (concat / sync) rồi trả kết quả.

Payload (mode=concat):
    {
        "mode": "concat",                 # optional, mặc định "concat"
        "match_glob": "captures/.../**/*.webm",   # hoặc dùng meeting_url + date
        "meeting_url": "https://meet.google.com/aex-cqbn-hxi",  # để build glob
        "meet_code": "aex-cqbn-hxi",      # thay cho meeting_url nếu có sẵn
        "date": "2026-08-19",             # yyyy-mm-dd (KNS: session_date)
        "scheduled_start": "2026-08-19T19:45:00+07:00",  # 1:1: sẽ tự cắt ra ngày (UTC)
        "select": "largest",              # all|largest|direct|from_c (mặc định all)
        "async": true,                    # true -> trả 202 ngay, ghép ở nền
        "sync_after": true                # sau khi ghép, ghi record_link vào Supabase
    }

Build glob từ meet_code + date (khi không truyền match_glob):
    captures/<meet_code>/session-<yyyy-mm-dd>T*/participants/*/video/**/*.webm
`session-<date>T*` wildcard toàn bộ phần giờ + -tab-<num> nên khớp mọi session
của meet_code đó trong ngày (tên folder có giờ không khớp giờ thật cũng OK).

ASYNC: async=true trả 202 ngay rồi ghép + sync ở BACKGROUND THREAD. Điều này chỉ
chạy đúng trên Cloud Run nếu deploy với CPU luôn cấp phát:
    gcloud run deploy ... --no-cpu-throttling
Nếu không, CPU bị bóp sau khi trả response và task nền sẽ ngừng giữa chừng.
"""

import io
import os
import sys
import json
import time
import uuid
import hashlib
import threading
import traceback
import contextlib
from datetime import datetime, timezone

import functions_framework
from google.cloud import storage

from src.config import (
    PROJECT_ID,
    BUCKET_NAME,
    ENVIRONMENT,
    KEEP_ALL_WEBM_CHUNKS,
    SUPABASE_API_URL,
    SUPABASE_API_KEY,
)
from main import main as run_concat, run_sync

# Server chạy dài KHÔNG được giữ temp: /tmp trên Cloud Run là RAM-backed, mỗi
# request để lại chunk + video ghép sẽ tích dần tới OOM. Việc upload lên GCS
# vẫn diễn ra trước đó, nên bật dọn temp chỉ xoá bản cục bộ thừa.
_SERVE_KEEP_ALL_CONCATENATED_WEBM_FILES = False
_SERVE_KEEP_TEMP_ON_FAILURE = False

_ALLOWED_SELECT = {"all", "largest", "direct", "from_c"}
_LOCK_TTL_SECONDS = 1800  # 30 phút: lock cũ hơn ngần này coi như stale.
# Giới hạn số concat nền chạy đồng thời TRÊN MỘT INSTANCE (ffmpeg nặng).
# Các thread vượt hạn sẽ xếp hàng chờ. Chỉnh qua env MAX_CONCURRENT_CONCAT.
_MAX_CONCURRENT_BG = max(1, int(os.getenv("MAX_CONCURRENT_CONCAT", "1")))
_bg_semaphore = threading.BoundedSemaphore(_MAX_CONCURRENT_BG)

_storage_client = None


def _get_storage_client():
    global _storage_client
    if _storage_client is None:
        _storage_client = storage.Client(project=PROJECT_ID)
    return _storage_client


# ---------------------------------------------------------------------------
# Helpers: đường dẫn / chọn file
# ---------------------------------------------------------------------------

def _to_gs_uri(path: str) -> str:
    if path.startswith("gs://"):
        return path
    clean = path.lstrip("/")
    if clean.startswith("output/"):
        clean = clean[len("output/"):]
    return f"gs://{BUCKET_NAME}/{clean}"


def _blob_size(gs_uri: str) -> int:
    try:
        bucket_name, _, blob_name = gs_uri[len("gs://"):].partition("/")
        blob = _get_storage_client().bucket(bucket_name).get_blob(blob_name)
        return (blob.size or 0) if blob is not None else 0
    except Exception:
        return 0


def _is_from_c(gs_uri: str) -> bool:
    name = gs_uri.rsplit("/", 1)[-1]
    if name.lower().endswith(".webm"):
        name = name[: -len(".webm")]
    return name.endswith("_from_c")


def _select_files(files: list[str], select: str) -> list[str]:
    if select == "all" or len(files) <= 1:
        return files
    if select == "direct":
        picked = [f for f in files if not _is_from_c(f)]
        return picked or files
    if select == "from_c":
        picked = [f for f in files if _is_from_c(f)]
        return picked or files
    if select == "largest":
        return [max(files, key=_blob_size)]
    return files


def _session_files_from_outputs(output_paths: list[str]) -> list[str]:
    # Chỉ giữ video CẤP SESSION (bỏ mọi đường dẫn có "/participants/").
    return [
        _to_gs_uri(p)
        for p in output_paths
        if "/participants/" not in _to_gs_uri(p)
    ]


# ---------------------------------------------------------------------------
# Helpers: build glob từ meet_code + ngày
# ---------------------------------------------------------------------------

def _meet_code_from_url(meet_url: str) -> str:
    return meet_url.rstrip("/").split("/")[-1]


def _to_utc_date(value: str) -> str:
    """value có thể là 'yyyy-mm-dd' hoặc ISO timestamp (có/không timezone).
    Trả về yyyy-mm-dd theo UTC (tên folder dùng ...T<time>Z = UTC)."""
    value = value.strip()
    if len(value) == 10 and value[4] == "-" and value[7] == "-":
        return value  # đã là yyyy-mm-dd (KNS session_date)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return value[:10]
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.date().isoformat()


def _build_concat_glob(meet_code: str, date: str) -> str:
    return (
        f"captures/{meet_code}/session-{date}T*"
        f"/participants/*/video/**/*.webm"
    )


def _concat_glob_to_sync_glob(concat_glob: str) -> str | None:
    """captures/<a>/<b>/participants/*/video/**/*.webm
       -> captures/<a>/<b>/concatenated/*.webm"""
    idx = concat_glob.find("/participants/")
    if idx == -1:
        return None
    return concat_glob[:idx] + "/concatenated/*.webm"


def _resolve_match_glob(payload: dict) -> tuple[str | None, str | None]:
    """Trả (match_glob, error). Ưu tiên match_glob tường minh; nếu không có thì
    build từ meet_code/meeting_url + date/scheduled_start."""
    match_glob = payload.get("match_glob") or payload.get("glob")
    if match_glob:
        return str(match_glob), None

    meet_code = payload.get("meet_code")
    if not meet_code and payload.get("meeting_url"):
        meet_code = _meet_code_from_url(str(payload["meeting_url"]))

    raw_date = payload.get("date") or payload.get("scheduled_start")
    if not (meet_code and raw_date):
        return None, (
            "Cần 'match_glob', HOẶC ('meet_code'|'meeting_url') + "
            "('date'|'scheduled_start')."
        )
    return _build_concat_glob(str(meet_code), _to_utc_date(str(raw_date))), None


# ---------------------------------------------------------------------------
# Lock trên GCS: chống ghép trùng khi nhiều request cùng session
# ---------------------------------------------------------------------------

def _lock_blob(glob: str):
    digest = hashlib.sha1(glob.encode("utf-8")).hexdigest()[:16]
    return _get_storage_client().bucket(BUCKET_NAME).blob(
        f"locks/concat/{digest}.lock"
    )


def _acquire_lock(glob: str) -> bool:
    blob = _lock_blob(glob)
    payload = json.dumps({"glob": glob, "at": time.time()})
    try:
        blob.upload_from_string(payload, if_generation_match=0)
        return True
    except Exception:
        # Đã có lock -> kiểm tra tuổi, stale thì chiếm lại.
        try:
            blob.reload()
            updated = blob.updated
            if updated is not None:
                age = (datetime.now(timezone.utc) - updated).total_seconds()
                if age > _LOCK_TTL_SECONDS:
                    blob.delete()
                    blob.upload_from_string(payload, if_generation_match=0)
                    return True
        except Exception:
            pass
        return False


def _release_lock(glob: str) -> None:
    try:
        _lock_blob(glob).delete()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Core: chạy concat (+ sync) — dùng cho cả sync path lẫn background thread
# ---------------------------------------------------------------------------

def _do_concat(match_glob: str) -> list[str]:
    return run_concat(
        project_id=PROJECT_ID,
        bucket_name=BUCKET_NAME,
        environment=ENVIRONMENT,
        data_profiling=False,
        match_glob=match_glob,
        keep_all_webm_chunks=KEEP_ALL_WEBM_CHUNKS,
        keep_all_concatenated_webm_files=_SERVE_KEEP_ALL_CONCATENATED_WEBM_FILES,
        keep_temp_on_failure=_SERVE_KEEP_TEMP_ON_FAILURE,
        return_existing_session_outputs=True,
    )


def _do_sync(sync_glob: str) -> None:
    run_sync(
        project_id=PROJECT_ID,
        bucket_name=BUCKET_NAME,
        environment=ENVIRONMENT,
        supabase_api_url=SUPABASE_API_URL,
        supabase_api_key=SUPABASE_API_KEY,
        supabase_sync_match_glob=sync_glob,
    )


def _run_locked(
    job_id: str,
    match_glob: str,
    sync_glob: str | None,
    select: str,
    sync_after: bool,
    started: float,
) -> None:
    print(f"[async {job_id}] BẮT ĐẦU concat | glob={match_glob}")
    outputs = _do_concat(match_glob)
    files = _select_files(_session_files_from_outputs(outputs), select)
    print(
        f"[async {job_id}] concat XONG sau {time.time() - started:.1f}s "
        f"| session_files={files}"
    )

    if sync_after and sync_glob:
        print(f"[async {job_id}] BẮT ĐẦU sync record_link | glob={sync_glob}")
        _do_sync(sync_glob)
        print(f"[async {job_id}] sync XONG -> record_link đã ghi vào Supabase")
    elif sync_after and not sync_glob:
        print(
            f"[async {job_id}] BỎ QUA sync: không suy ra được sync_glob "
            f"từ match_glob (thiếu '/participants/')."
        )

    print(
        f"[async {job_id}] HOÀN TẤT | tổng {time.time() - started:.1f}s "
        f"| files={files}"
    )


def _background_concat_and_sync(
    *,
    job_id: str,
    match_glob: str,
    sync_glob: str | None,
    select: str,
    sync_after: bool,
) -> None:
    """Chạy trong background thread (async). Log dày để debug trên Cloud Logging."""
    started = time.time()
    print(
        f"[async {job_id}] xếp hàng (đang chờ slot; "
        f"tối đa {_MAX_CONCURRENT_BG}/instance) | glob={match_glob}"
    )
    try:
        with _bg_semaphore:
            _run_locked(job_id, match_glob, sync_glob, select, sync_after, started)
    except Exception as error:
        print(
            f"[async {job_id}] LỖI {type(error).__name__}: {error}\n"
            f"{traceback.format_exc()}"
        )
    finally:
        _release_lock(match_glob)
        print(f"[async {job_id}] đã nhả lock cho glob={match_glob}")


# ---------------------------------------------------------------------------
# stdout tee (cho sync path: trả log trong response + hiện live trên Logging)
# ---------------------------------------------------------------------------

class _Tee(io.TextIOBase):
    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, data):
        for stream in self._streams:
            try:
                stream.write(data)
                stream.flush()
            except Exception:
                pass
        return len(data)

    def flush(self):
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:
                pass


def _json_response(body: dict, status: int):
    return (
        json.dumps(body, ensure_ascii=False),
        status,
        {"Content-Type": "application/json; charset=utf-8"},
    )


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

@functions_framework.http
def handle(request):
    if request.method != "POST":
        return _json_response(
            {"ok": False, "error": "Chỉ chấp nhận method POST."}, 405
        )

    # Shared-secret (nhất quán với cách repo gọi các Cloud Run service khác).
    # Chỉ bật khi INVOKE_SHARED_SECRET được set; nếu không set thì bỏ qua
    # (dùng khi service vẫn để IAM --no-allow-unauthenticated).
    secret = os.getenv("INVOKE_SHARED_SECRET")
    if secret:
        # Đọc secret từ header RIÊNG X-Service-Secret để KHÔNG đụng Authorization
        # (Authorization dành cho Cloud Run IAM = id token). Chỉ khi service để
        # public không IAM thì mới fallback đọc secret từ Authorization: Bearer.
        provided = request.headers.get("X-Service-Secret", "")
        if not provided:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                provided = auth_header[len("Bearer "):]
        if provided != secret:
            return _json_response({"ok": False, "error": "Unauthorized."}, 401)

    payload = request.get_json(silent=True) or {}

    mode = str(payload.get("mode") or "concat").lower().strip()
    if mode not in {"concat", "sync"}:
        return _json_response(
            {"ok": False, "error": "mode phải là 'concat' hoặc 'sync'."}, 400
        )

    select = str(payload.get("select") or "all").lower().strip()
    if select not in _ALLOWED_SELECT:
        return _json_response(
            {"ok": False, "error": "select phải là 'all'|'largest'|'direct'|'from_c'."},
            400,
        )

    match_glob, glob_error = _resolve_match_glob(payload)
    if not match_glob:
        return _json_response({"ok": False, "error": glob_error}, 400)

    # -------- mode=sync: chỉ chạy sync (đồng bộ) --------
    if mode == "sync":
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(_Tee(sys.__stdout__, buffer)):
                _do_sync(match_glob)
        except Exception as error:
            return _json_response(
                {
                    "ok": False, "mode": "sync", "match_glob": match_glob,
                    "error": f"{type(error).__name__}: {error}",
                    "log": buffer.getvalue()[-40000:],
                },
                500,
            )
        return _json_response(
            {"ok": True, "mode": "sync", "match_glob": match_glob,
             "log": buffer.getvalue()[-40000:]},
            200,
        )

    # -------- mode=concat --------
    sync_after = _as_bool(payload.get("sync_after")) if "sync_after" in payload else True
    sync_glob = _concat_glob_to_sync_glob(match_glob)

    # ===== ASYNC: trả 202 ngay, ghép + sync ở background =====
    if _as_bool(payload.get("async")):
        if not _acquire_lock(match_glob):
            print(f"[async] BỎ QUA: đã có job đang chạy cho glob={match_glob}")
            return _json_response(
                {
                    "ok": True, "mode": "concat", "status": "already_processing",
                    "match_glob": match_glob,
                    "message": "Session này đang được ghép bởi một job khác.",
                },
                202,
            )

        job_id = uuid.uuid4().hex[:12]
        print(
            f"[async {job_id}] NHẬN request | glob={match_glob} "
            f"| sync_glob={sync_glob} | select={select} | sync_after={sync_after}"
        )
        thread = threading.Thread(
            target=_background_concat_and_sync,
            kwargs={
                "job_id": job_id,
                "match_glob": match_glob,
                "sync_glob": sync_glob,
                "select": select,
                "sync_after": sync_after,
            },
            daemon=True,
        )
        thread.start()
        return _json_response(
            {
                "ok": True, "mode": "concat", "status": "processing",
                "job_id": job_id, "match_glob": match_glob,
                "sync_glob": sync_glob, "select": select,
                "sync_after": sync_after,
                "message": "Đã nhận. Ghép chạy ở nền; record_link sẽ được ghi vào "
                           "Supabase khi xong. Parent app chỉ cần fetch lại.",
            },
            202,
        )

    # ===== SYNC (đồng bộ, chủ yếu để test) =====
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(_Tee(sys.__stdout__, buffer)):
            outputs = _do_concat(match_glob)
            files = _select_files(_session_files_from_outputs(outputs), select)
            if sync_after and sync_glob:
                print(f"[sync-after] ghi record_link | glob={sync_glob}")
                _do_sync(sync_glob)
    except Exception as error:
        return _json_response(
            {
                "ok": False, "mode": "concat", "match_glob": match_glob,
                "error": f"{type(error).__name__}: {error}",
                "log": buffer.getvalue()[-40000:],
            },
            500,
        )

    return _json_response(
        {
            "ok": True, "mode": "concat", "match_glob": match_glob,
            "select": select, "count": len(files), "files": files,
            "sync_after": bool(sync_after and sync_glob),
            "log": buffer.getvalue()[-40000:],
        },
        200,
    )
