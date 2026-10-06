# Chạy dưới dạng Function (HTTP) thay vì Batch Job

Project này vốn chạy dạng **Cloud Run Job** (`RUN_MODE=concat` / `sync`).
Giờ nó có thêm một HTTP entrypoint để chạy như một **function / API service**:
gọi POST với payload là một glob pattern, nó chạy đúng logic cũ trên glob đó
rồi trả kết quả về.

## Đã thêm gì
- `function_main.py` — HTTP handler (functions-framework), target = `handle`.
- `RUN_MODE=serve` trong `docker-entrypoint.sh` — chạy functions-framework.
- `functions-framework` vào `pyproject.toml` + `uv.lock` + `requirements.txt`.
- `Dockerfile` copy thêm `function_main.py`.

## API
`POST /` với JSON:
```json
{ "mode": "concat", "match_glob": "captures/aex-cqbn-hxi/**/*.webm" }
```
- `mode`: `"concat"` (mặc định) hoặc `"sync"`.
- `match_glob`: bắt buộc — glob pattern trên bucket. (alias: `glob`)

Trả về (mỗi file ghép được là một URI `gs://`):
```json
{
  "ok": true,
  "mode": "concat",
  "match_glob": "...",
  "count": 2,
  "files": [
    "gs://meet-captures-2026/captures/aex-cqbn-hxi/session-.../concatenated/b_from_c.webm",
    "gs://meet-captures-2026/captures/aex-cqbn-hxi/session-.../concatenated/direct_b.webm"
  ],
  "log": "<toàn bộ output>"
}
```
(`mode=sync` không tạo file ghép nên `files` = `[]`.)

## Deploy — Cách A (khuyến nghị): Cloud Run Service từ Docker image
Dùng chính image sẵn có nên **có ffmpeg** → chạy được cả `concat` lẫn `sync`.

```bash
gcloud run deploy concatenate-records-fn \
  --source . \
  --region asia-southeast1 \
  --memory 4Gi --cpu 2 --timeout 3600 --concurrency 1 \
  --no-allow-unauthenticated \
  --set-env-vars RUN_MODE=serve,ENVIRONMENT=container,PROJECT_ID=tcservices2027,BUCKET_NAME=meet-captures-2026,KEEP_ALL_WEBM_CHUNKS=false,SUPABASE_API_URL=https://ktbzdpbvthnejqjoueln.supabase.co \
  --set-secrets SUPABASE_API_KEY=SUPABASE_API_KEY:latest
```
Ghi chú:
- `--source .` sẽ build bằng `Dockerfile` có sẵn (đã cài ffmpeg).
- `.env` bị dockerignore, nên biến môi trường phải set khi deploy như trên.
  Nên để `SUPABASE_API_KEY` trong Secret Manager (tạo secret rồi dùng
  `--set-secrets`), đừng nhét thẳng vào `--set-env-vars`.
- Service account của service cần quyền đọc/ghi bucket (roles/storage.objectAdmin
  trên bucket `meet-captures-2026`).
- `concat` nặng (tải chunk về /tmp, chạy ffmpeg). `/tmp` trên Cloud Run tính vào
  RAM → nếu session lớn hãy tăng `--memory`. Giữ glob hẹp (1 session/lần) để nhanh
  và tránh chạm timeout 3600s.
- **Dọn temp**: batch job giữ temp (`KEEP_ALL_CONCATENATED_WEBM_FILES=true`) là ổn vì
  container thoát sau mỗi lần chạy. Server thì KHÔNG được giữ — `function_main.py` đã
  **ép dọn temp mỗi request** (bỏ qua các cờ KEEP_* của .env) để `/tmp` không tích tới
  OOM. Video vẫn được upload lên GCS trước khi xoá bản temp cục bộ.
- **`--concurrency 1`**: mỗi instance chỉ xử lý 1 request concat tại một thời điểm.
  ffmpeg + video lớn trong RAM nên chạy song song nhiều request trên cùng instance dễ
  OOM. Cần thông lượng cao thì tăng số instance (`--max-instances`), đừng tăng concurrency.

## Deploy — Cách B: Cloud Functions gen2 (chỉ `sync`)
Runtime Cloud Functions **không có ffmpeg** → chỉ hợp cho `mode=sync`.
```bash
gcloud functions deploy concatenate-records-fn \
  --gen2 --runtime python313 --region asia-southeast1 \
  --source . --entry-point handle --trigger-http --no-allow-unauthenticated \
  --set-env-vars ENVIRONMENT=container,PROJECT_ID=tcservices2027,BUCKET_NAME=meet-captures-2026 \
  --set-secrets SUPABASE_API_KEY=SUPABASE_API_KEY:latest,SUPABASE_API_URL=SUPABASE_API_URL:latest
```
(Lưu ý: `requires-python` đang là 3.14, còn Cloud Functions tối đa python313 —
đây là lý do nữa nên ưu tiên Cách A.)

## Gọi từ Node.js
Service đặt `--no-allow-unauthenticated` nên cần ID token.
```bash
npm install google-auth-library
```
```js
const { GoogleAuth } = require('google-auth-library');

const url = 'https://concatenate-records-fn-xxxx.a.run.app'; // URL sau khi deploy

async function triggerConcat(matchGlob) {
  const auth = new GoogleAuth();
  const client = await auth.getIdTokenClient(url); // tự lấy ID token đúng audience
  const res = await client.request({
    url,
    method: 'POST',
    data: { mode: 'concat', match_glob: matchGlob },
    timeout: 3600_000,
  });
  return res.data; // { ok, mode, match_glob, log }
}

triggerConcat('captures/aex-cqbn-hxi/**/*.webm')
  .then((r) => console.log(r))
  .catch((e) => console.error(e.response?.data || e.message));
```
Chạy local cần `GOOGLE_APPLICATION_CREDENTIALS` trỏ tới service-account key có
quyền `roles/run.invoker` trên service. Trên GCP thì dùng ADC tự động.

## Lưu ý về "job chạy lâu"
Handler chạy **đồng bộ**: request treo tới khi ghép xong mới trả `log`. Nếu glob
rộng, thời gian có thể vượt timeout HTTP. Hai lựa chọn: (1) gọi với glob hẹp từng
session; hoặc (2) đổi sang mô hình async — trả `202` ngay và chạy nền — mình có
thể bổ sung nếu bạn cần.

## Test local
```bash
uv run functions-framework --target=handle --source=function_main.py --port=8080
# terminal khác:
curl -X POST localhost:8080 -H 'Content-Type: application/json' \
  -d '{"mode":"sync","match_glob":"captures/aex-cqbn-hxi/.../concatenated/*.webm"}'
```

---

# ASYNC + tự ghi record_link (cho KNS)

## Payload async (mode=concat)
```json
{
  "mode": "concat",
  "async": true,
  "sync_after": true,
  "select": "largest",
  "meeting_url": "https://meet.google.com/aex-cqbn-hxi",
  "date": "2026-08-19"
}
```
- `meeting_url` + `date` → service tự build glob `captures/<meet_code>/session-<date>T*/participants/*/video/**/*.webm` (wildcard giờ; KNS `session_date` chỉ có ngày nên khỏi cắt giờ). Có thể truyền thẳng `match_glob` thay cho meeting_url+date.
- `async: true` → trả **202 ngay**, ghép + sync chạy ở **background thread**.
- `sync_after: true` → sau khi ghép, chạy `run_sync` trên `.../concatenated/*.webm` để ghi `record_link` (gs:// của video lớn nhất) vào Supabase.
- Có **lock trên GCS** (`locks/concat/<hash>.lock`, TTL 30') chống ghép trùng khi nhiều request cùng session.

## Bảo mật: shared secret
Service check header nếu env `INVOKE_SHARED_SECRET` được set:
`Authorization: Bearer <secret>` (hoặc header `X-Service-Secret`). Không set thì bỏ qua (dùng khi vẫn để IAM).

## Deploy cho async (BẮT BUỘC --no-cpu-throttling)
Async chạy nền SAU khi trả response → phải để CPU luôn cấp phát, nếu không task nền bị bóp CPU và ngừng giữa chừng:
```bash
export IMAGE="asia-east1-docker.pkg.dev/tcservices2027/cloud-run-source-deploy/concatenate-records-segments:$(git rev-parse --short HEAD)"
docker buildx build --platform linux/amd64 -t "$IMAGE" --push .
gcloud run deploy concatenate-records-segments --image "$IMAGE" \
  --region asia-east1 --project tcservices2027 \
  --memory 4Gi --cpu 2 --timeout 3600 --concurrency 1 \
  --no-cpu-throttling \
  --allow-unauthenticated \
  --set-env-vars RUN_MODE=serve,ENVIRONMENT=container,PROJECT_ID=tcservices2027,BUCKET_NAME=meet-captures-2026,SUPABASE_API_URL=https://ktbzdpbvthnejqjoueln.supabase.co,INVOKE_SHARED_SECRET=<đặt-secret-mạnh> \
  --set-secrets SUPABASE_API_KEY=SUPABASE_API_KEY:latest
```
(`--allow-unauthenticated` + `INVOKE_SHARED_SECRET` = pattern shared-secret giống các service khác trong repo. Muốn chặt hơn thì giữ IAM `--no-allow-unauthenticated` và bỏ secret, nhưng backend Netlify/Vercel sẽ phải tự ký ID token.)

## Env phía ParentApp (Netlify/Vercel)
```
CONCAT_SERVICE_URL=https://concatenate-records-segments-588651614395.asia-east1.run.app
CONCAT_SERVICE_SECRET=<đúng bằng INVOKE_SHARED_SECRET>
```

## Test async
```bash
curl -sS -X POST "$URL" -H "Authorization: Bearer <secret>" -H "Content-Type: application/json" \
  -d '{"mode":"concat","async":true,"sync_after":true,"select":"largest","meeting_url":"https://meet.google.com/aex-cqbn-hxi","date":"2026-08-19"}'
# -> 202 {"status":"processing","job_id":"..."} ngay lập tức.
# Xem tiến trình nền trong Cloud Logging: [async <job_id>] ...
```

---

# Ghép audio cấp session (`--concat-audio`)

Với mỗi segment_b, ghép chunk trong `shared_audio/` và `mentor_audio/`
(chấp nhận cả `shared-audio` / `mentor-audio`) rồi lưu vào `concatenated/` của session:

```
captures/<a>/<b>/shared_audio/**/*.webm -> captures/<a>/<b>/concatenated/<b>_shared_audio.webm
captures/<a>/<b>/mentor_audio/**/*.webm -> captures/<a>/<b>/concatenated/<b>_mentor_audio.webm
```

```bash
uv run python main.py --concat-audio --glob "captures/aex-cqbn-hxi/session-2026-08-19T*/*audio/**/*.webm"
# không truyền --glob -> AUDIO_MATCH_GLOB hoặc mặc định captures/*/*/*audio/**/*.webm (toàn bucket)
# Docker/Cloud Run Job: RUN_MODE=concat-audio
```
- Chunk sort theo `epoch_ms` trong tên (`chunk-<seq>-<epoch_ms>-...`), fallback theo tên blob.
- Output đã tồn tại (không rỗng) -> bỏ qua. Muốn ghép lại thì xoá file output trước.
- File `*_shared_audio.webm` / `*_mentor_audio.webm` được loại khỏi: điều kiện skip của
  `--concat` video và việc chọn video lớn nhất của `--sync` (không bao giờ thành `record_link`).
