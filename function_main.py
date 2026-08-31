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
import sys
import json
import contextlib

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


_ALLOWED_SELECT = {"all", "largest", "direct", "from_c"}
_size_client = None


def _get_size_client():
    global _size_client
    if _size_client is None:
        _size_client = storage.Client(project=PROJECT_ID)
    return _size_client


def _blob_size(gs_uri: str) -> int:
    try:
        bucket_name, _, blob_name = gs_uri[len("gs://"):].partition("/")
        blob = _get_size_client().bucket(bucket_name).get_blob(blob_name)
        return (blob.size or 0) if blob is not None else 0
    except Exception:
        return 0


def _is_from_c(gs_uri: str) -> bool:
    name = gs_uri.rsplit("/", 1)[-1]
    if name.lower().endswith(".webm"):
        name = name[: -len(".webm")]
    return name.endswith("_from_c")


def _select_files(files: list[str], select: str) -> list[str]:
    """Lọc danh sách video session theo yêu cầu:
      all     -> giữ nguyên (cả direct lẫn _from_c)
      direct  -> chỉ bản ghép trực tiếp <b>.webm
      from_c  -> chỉ bản <b>_from_c.webm
      largest -> chỉ cái dung lượng lớn nhất (giống logic sync)
    Nếu bộ lọc không khớp gì thì fallback trả nguyên danh sách."""
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


def _json_response(body: dict, status: int):
    return (
        json.dumps(body, ensure_ascii=False),
        status,
        {"Content-Type": "application/json; charset=utf-8"},
    )


class _Tee(io.TextIOBase):
    """Ghi song song ra nhiều stream. Dùng để vừa in ra stdout thật (Cloud
    Logging thấy tiến trình ngay lập tức) vừa gom vào buffer để trả trong
    field "log" của response."""

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

    select = str(payload.get("select") or "all").lower().strip()
    if select not in _ALLOWED_SELECT:
        return _json_response(
            {
                "ok": False,
                "error": "select phải là 'all', 'largest', 'direct' hoặc 'from_c'.",
            },
            400,
        )

    # Tee: vẫn in ra stdout THẬT (Cloud Logging thấy tiến trình live) đồng thời
    # gom vào buffer để trả trong field "log".
    buffer = io.StringIO()
    tee = _Tee(sys.__stdout__, buffer)
    output_paths: list[str] = []

    try:
        with contextlib.redirect_stdout(tee):
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
                    return_existing_session_outputs=True,
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
                "log": buffer.getvalue()[-40000:],
            },
            500,
        )

    all_files = [_to_gs_uri(p) for p in output_paths]

    # Chỉ trả video CẤP SESSION (segment_b), bỏ video cấp participant (segment_c).
    #   session-level:     captures/<a>/<b>/concatenated/<b>.webm (và <b>_from_c.webm)
    #   participant-level: captures/<a>/<b>/participants/<c>/concatenated/<c>.webm
    # -> loại mọi đường dẫn có "/participants/".
    session_files = [f for f in all_files if "/participants/" not in f]
    files = _select_files(session_files, select)

    return _json_response(
        {
            "ok": True,
            "mode": mode,
            "match_glob": match_glob,
            "select": select,
            "count": len(files),
            "files": files,
            "log": buffer.getvalue()[-40000:],
        },
        200,
    )
