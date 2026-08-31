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
