"""
HTTP entrypoint (functions-framework / Cloud Functions gen2 compatible).

Nhận một API call POST với payload JSON chứa glob pattern, chạy lại đúng
logic của main.py (concat hoặc sync) trên glob đó, rồi trả kết quả về.

Payload:
    {
        "mode": "concat" | "sync",   # optional, mặc định "concat"
        "match_glob": "captures/.../**/*.webm"   # bắt buộc
    }
    (chấp nhận "glob" như alias của "match_glob")

Deploy khuyến nghị: build Docker image sẵn có (đã cài ffmpeg) rồi chạy
Cloud Run Service với RUN_MODE=serve. Xem README-cloud-function.md.
"""

import io
import json
import contextlib

import functions_framework

from src.config import (
    PROJECT_ID,
    BUCKET_NAME,
    ENVIRONMENT,
    KEEP_ALL_WEBM_CHUNKS,
    SUPABASE_API_URL,
    SUPABASE_API_KEY,
)

# Server chạy dài KHÔNG được giữ temp: /tmp trên Cloud Run là RAM-backed, mỗi
# request để lại chunk + video ghép sẽ tích dần tới OOM. Việc upload lên GCS
# vẫn diễn ra trước đó, nên bật dọn temp chỉ xoá bản cục bộ thừa. (Batch job
# vẫn dùng KEEP_* trong .env như cũ, không bị đổi.)
_SERVE_KEEP_ALL_CONCATENATED_WEBM_FILES = False
_SERVE_KEEP_TEMP_ON_FAILURE = False
from main import main as run_concat, run_sync


def _to_gs_uri(path: str) -> str:
    """Đảm bảo đường dẫn có tiền tố gs://. Container mode vốn đã trả gs://;
    local mode trả 'output/...' -> quy về gs://<bucket>/..."""
    if path.startswith("gs://"):
        return path
    clean = path.lstrip("/")
    if clean.startswith("output/"):
        clean = clean[len("output/"):]
    return f"gs://{BUCKET_NAME}/{clean}"


def _json_response(body: dict, status: int):
    return (
        json.dumps(body, ensure_ascii=False),
        status,
        {"Content-Type": "application/json; charset=utf-8"},
    )


@functions_framework.http
def handle(request):
    if request.method != "POST":
        return _json_response(
            {"ok": False, "error": "Chỉ chấp nhận method POST."},
            405,
        )

    payload = request.get_json(silent=True) or {}

    mode = str(payload.get("mode") or "concat").lower().strip()
    match_glob = payload.get("match_glob") or payload.get("glob")

    if mode not in {"concat", "sync"}:
        return _json_response(
            {"ok": False, "error": "mode phải là 'concat' hoặc 'sync'."},
            400,
        )

    if not match_glob or not isinstance(match_glob, str):
        return _json_response(
            {
                "ok": False,
                "error": "Thiếu 'match_glob' (glob pattern) trong payload.",
            },
            400,
        )

    # Bắt toàn bộ output in ra stdout của main()/run_sync() để trả về làm log.
    buffer = io.StringIO()
    output_paths: list[str] = []

    try:
        with contextlib.redirect_stdout(buffer):
            if mode == "concat":
                output_paths = run_concat(
                    project_id=PROJECT_ID,
                    bucket_name=BUCKET_NAME,
                    environment=ENVIRONMENT,
                    data_profiling=False,
                    match_glob=match_glob,
                    keep_all_webm_chunks=KEEP_ALL_WEBM_CHUNKS,
                    keep_all_concatenated_webm_files=(
                        _SERVE_KEEP_ALL_CONCATENATED_WEBM_FILES
                    ),
                    keep_temp_on_failure=_SERVE_KEEP_TEMP_ON_FAILURE,
                )
            else:
                run_sync(
                    project_id=PROJECT_ID,
                    bucket_name=BUCKET_NAME,
                    environment=ENVIRONMENT,
                    supabase_api_url=SUPABASE_API_URL,
                    supabase_api_key=SUPABASE_API_KEY,
                    supabase_sync_match_glob=match_glob,
                )
    except Exception as error:
        return _json_response(
            {
                "ok": False,
                "mode": mode,
                "match_glob": match_glob,
                "error": f"{type(error).__name__}: {error}",
                "log": buffer.getvalue(),
            },
            500,
        )

    files = [_to_gs_uri(p) for p in output_paths]

    return _json_response(
        {
            "ok": True,
            "mode": mode,
            "match_glob": match_glob,
            "count": len(files),
            "files": files,
            "log": buffer.getvalue(),
        },
        200,
    )
