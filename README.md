# AI Travel Video Service

FastAPI nhận job từ hệ thống chính. PostgreSQL giữ job và outbox, Celery
Beat/worker gửi và nhận task qua Redis. Worker dùng FFmpeg để dựng media local,
có thể dùng Groq để viết storyboard, WaveSpeedAI cho tối đa hai clip HYBRID và
VieNeu CPU cho voiceover tiếng Việt. API và worker dùng chung volume media,
PostgreSQL và Redis có volume riêng.

- [Hợp đồng API, chín payload JSON và Java Spring Boot](API_AND_SPRING_BOOT.md)
- [Postman Collection](postman/AI-Travel-Video-Service.postman_collection.json) và [Local Environment](postman/Local-Environment.postman_environment.json)
- [Báo cáo bàn giao và kết quả kiểm chứng](AI_TRAVEL_VIDEO_FINAL_REPORT.md)

## Docker preflight và kiểm thử cô lập

Cài Docker Desktop và dùng Linux containers trên Windows. Kiểm tra daemon và
cấu hình mà không in nội dung env đã nội suy.

```powershell
docker version
docker info
docker compose version
docker compose config --quiet
python setup_tts_model.py models/tts --check
docker compose build
docker compose up -d --wait
docker compose ps
```

Trên Windows có thể chạy `python setup_tts_model.py "D:/AI/models/vieneu-nano"`
và đặt `TTS_MODEL_HOST_DIR=D:/AI/models/vieneu-nano`. Giữ
`TTS_MODEL_DIR=/data/models/tts` trong container.

Trước khi khởi động một worker trên stack đã có dữ liệu, kiểm tra các job
outbox đang chờ, cờ provider và phạm vi key. Mặc định tắt paid submit.
Để thử trên DB riêng, dùng project Compose riêng qua `-p` và env riêng
qua `--env-file`. Env đó đặt `SERVICE_ENV_FILE` trỏ đúng file env cô lập,
cổng khác, DB/password/key thử nghiệm và tắt cả Groq, WaveSpeed, Wan. Compose
test profile dùng PostgreSQL tmpfs riêng. Các thao tác khởi động và dừng thông
thường giữ dữ liệu PostgreSQL, Redis và output.

Worker healthcheck ping chính worker qua Celery và kiểm file model cùng SDK
khi TTS bật. Readiness API kiểm PostgreSQL/Redis. Sự cố ở Groq hoặc WaveSpeed
không chặn API nhận job LOCAL. Worker mặc định 4 CPU/4 GB có thể điều chỉnh
qua `VIDEO_WORKER_CPUS` và `VIDEO_WORKER_MEMORY`. Với TTS, bắt đầu từ
`WORKER_CONCURRENCY=1` rồi đo tải thực tế.

## Postman và mẫu E2E

Import collection và environment. Điền key tenant trong Postman và dùng
`baseUrl` của stack tương ứng. Gửi Health, chọn file trong Body → binary
trước khi gửi Assets. Khi đã xác nhận worker tắt Director và video providers,
đặt `offlineWorkerConfirmed=true` để gửi các request LOCAL. Đổi `requestId`
cho lần tạo mới và giữ nguyên ID khi retry cùng payload. Mục Jobs chỉ poll
từng request theo yêu cầu. Khi COMPLETED dùng Send and Download.

Mặc định Postman bỏ qua mọi request có thể tạo job thành công, gồm các
request LOCAL và mục provider. Chỉ mở `allowProviderRequests` sau khi được
duyệt quyền dùng quota/chi phí và gửi riêng đúng canary. Với canary WaveSpeed
720p/5 giây/audio off, ngân sách tham chiếu là 0,1250 USD cho một clip. Giá
thực phải đối soát theo provider. [Bảng giá WaveSpeedAI](https://wavespeed.ai/models/alibaba/wan-2.6/image-to-video-flash)

Script `scripts/phase3_smoke.py` nhận service key từ
`PHASE3_SMOKE_API_KEY`. Chạy trên stack kiểm thử đã bật TTS với các provider
tắt. Script dùng ảnh minh họa tự tạo, kiểm API, idempotency, download, FFprobe
và trích frame. TTS inference là offline; dùng UUID upload khi muốn toàn bộ
job tránh tải media bên ngoài.

## Troubleshooting

| Hiện tượng | Kiểm tra |
|---|---|
| Docker daemon unavailable | Mở Docker Desktop, chọn Linux containers, kiểm context và chạy lại `docker version` |
| Groq HTTP 401 hoặc 429 | Kiểm key worker hoặc quota, giữ retry giới hạn và xem mã fallback |
| Groq model_not_found | Kiểm model tài khoản được cấp qua GET /models khi có quyền và đặt lại GROQ_MODEL |
| Invalid storyboard | Xem mã GROQ_INVALID_STORYBOARD, length/timeline/asset index/AI budget và cờ fallback |
| WaveSpeed insufficient balance hoặc HTTP 403 | Kiểm quota, quyền model, trạng thái tài khoản và key worker. Tránh gửi lại prediction khi POST mơ hồ |
| Prediction failed | Kiểm errorCode, deadline và kết quả provider. Phân biệt LOCAL fallback bằng executionMode |
| TTS model unavailable | Chạy setup script với --check, kiểm đúng target tts-worker và mount model read-only |
| TTS inference timeout | Kiểm slot TTS, CPU/RAM và độ dài lời dẫn. Đo trước khi tăng timeout Celery |
| FFmpeg failure | Kiểm MIME, format, stream và log media_command_failed; xác nhận FFmpeg/FFprobe trong worker |
| PostgreSQL/Redis không kết nối | Kiểm service health, DB/Redis URL trong Docker và quyền Alembic |
| Worker không nhận job | Kiểm worker health, kết nối Redis, Celery Beat và lịch redispatch của outbox |
| Video freeze | Kiểm clip nguồn và metadata FFprobe, kiểm job mã QUALITY_CHECK_FAILED |
| Phụ đề sai font tiếng Việt | Kiểm DejaVu Sans, fontconfig, libass, encoding UTF-8 và frame preview |

Microservice độc lập tạo video quảng cáo tour bằng FFmpeg trên CPU. Backend AI-Travel-Reseller-Network-Platform gọi REST API bằng `X-Service-API-Key` của tenant. Service dùng PostgreSQL và Redis riêng.

## Docker Compose

Tạo `.env` từ `.env.example`. Đặt `POSTGRES_PASSWORD` và phần mật khẩu trong `DATABASE_URL` bằng cùng giá trị. Dùng mật khẩu hex ngẫu nhiên hoặc URL-encode mật khẩu. Thay `SERVICE_API_KEYS` bằng JSON ánh xạ UUID tenant sang key riêng, ngẫu nhiên, ít nhất 32 ký tự. Backend nền tảng chọn key dựa trên tenant đã xác thực.

```sh
docker compose up --build -d
```

API tự áp dụng Alembic khi khởi động. Mở `http://127.0.0.1:8000/docs` và dùng **Authorize**. `/health/live` kiểm tra API. `/health/ready` kiểm tra PostgreSQL và Redis. Cổng API bind loopback mặc định. Khi tích hợp qua mạng khác, bảo vệ API bằng reverse proxy HTTPS hoặc mạng riêng. PostgreSQL và Redis dùng mạng Compose.

API và worker dùng chung volume `media-data` để lưu asset upload và MP4. `MEDIA_ROOT` mặc định là `/data/media` trong Docker. Volume giữ file khi container được tạo lại. `MAX_ASSET_BYTES` mặc định 32 MiB mỗi file, `MAX_JOB_ASSET_BYTES` 160 MiB tổng asset duy nhất và `MAX_ASSET_DURATION` 180 giây cho clip/audio. Cân đối `WORKER_CONCURRENCY`, `FFMPEG_THREADS` và timeout với CPU của máy chạy.

## Thử bằng Postman

1. Tạo biến `baseUrl` bằng `http://127.0.0.1:8000` và `serviceKey` bằng key đã cấu hình. Thêm header `X-Service-API-Key: {{serviceKey}}` vào các request dưới đây.
2. Gửi `POST {{baseUrl}}/api/v1/videos/assets`. Chọn **Body → binary**, chọn file và đặt `Content-Type` phù hợp như `image/jpeg`, `video/quicktime` hoặc `audio/wav`. Request trả `201` cùng `assetId`. Lặp lại cho các file cần dùng. Mỗi ID chỉ truy cập được trong tenant đã upload.
3. Gửi `POST {{baseUrl}}/api/v1/videos/generate` với **Body → raw → JSON**. Thay giá trị minh họa trong `images` bằng UUID upload hoặc URL HTTPS công khai trỏ thẳng đến ảnh. Thêm `videos` cho clip, `logo` cho logo, `backgroundMusic` và `voiceover` cho file âm thanh. Các trường media đều nhận UUID hoặc URL.

```json
{
  "requestId": "da-nang-mua-he-1",
  "productId": "tour-da-nang-3n2d",
  "tour": {
    "title": "Đà Nẵng 3 ngày 2 đêm",
    "destination": "Đà Nẵng",
    "highlights": ["Bán đảo Sơn Trà", "Ngũ Hành Sơn"],
    "experience": "Dạo biển Mỹ Khê",
    "benefits": "Đã gồm xe đưa đón",
    "priceVnd": 2990000,
    "promotion": "Ưu đãi cho nhóm từ 4 khách",
    "callToAction": "Liên hệ để đặt tour"
  },
  "images": ["11111111-1111-4111-8111-111111111111"],
  "config": {"mode": "LOCAL", "durationSeconds": 40, "aspectRatio": "9:16"}
}
```

4. API trả `202` cùng `jobId`. Gọi `GET {{baseUrl}}/api/v1/videos/{jobId}` để đọc `status`, `progress`, chi phí và mã lỗi. Có thể phân trang bằng `GET {{baseUrl}}/api/v1/videos?page=1&pageSize=20`.
5. Khi job đạt `COMPLETED`, ghép `{{baseUrl}}` với đường dẫn tương đối trong `outputUrl`, giữ header API key và dùng **Send and Download** để tải MP4.

Cùng tenant, `requestId` và payload đã chuẩn hóa trả lại job cũ. Dùng lại `requestId` với payload khác trả `409`. Thiếu key hoặc key sai trả `401`. Job ngoài tenant trả `404`. Tiền USD dạng Decimal được trả dưới dạng chuỗi JSON.

## Render và kiểm tra chất lượng

- `LOCAL` nhận tổng 1–30 ảnh JPG/JPEG, PNG, WebP hoặc clip MP4/MOV. Logo dùng định dạng ảnh. Audio hỗ trợ WAV, MP3, Ogg hoặc M4A/MP4 có audio stream. Cung cấp nhạc và voiceover từ file có quyền sử dụng. File video nguồn chỉ cung cấp hình ảnh. Khi có voiceover, nhạc nền tự giảm âm lượng, mix được trim theo video và fade ở cuối. Với input không có audio, MP4 chứa AAC im lặng.
- Storyboard lấy tiêu đề, điểm đến, điểm tham quan, trải nghiệm, giá VND, ưu đãi và CTA từ payload. Nội dung thiếu được bỏ qua. Ảnh dùng hiệu ứng Ken Burns. Các cảnh ghép crossfade và có fade đầu/cuối. Subtitles tiếng Việt được ghi trực tiếp vào video bằng DejaVu Sans, tự xuống dòng và đặt trong vùng an toàn của khung hình dọc.
- MP4 output có H.264, AAC, YUV420P, faststart, 30 FPS và một trong hai độ phân giải 1080×1920 hoặc 1920×1080. Chọn 30, 35 hoặc 40 giây. Worker kiểm tra codec, stream, kích thước, thời lượng trong sai số 0,2 giây, cảnh đen kéo dài và freeze trong cảnh clip. Các cảnh từ ảnh tĩnh được miễn kiểm tra freeze.
- Khi tải HTTPS, worker xác thực mọi IP DNS, kết nối trực tiếp tới IP đã kiểm tra, giữ xác thực TLS theo hostname, từ chối redirect và giới hạn số byte. Sau khi nhận asset, worker đối chiếu MIME/định dạng, kiểm tra ảnh hoặc probe clip/audio trước khi render. URL và tên file từ client không được đưa vào câu lệnh FFmpeg.
## Lồng tiếng Việt offline và phụ đề

TTS mặc định tắt. Cài đúng một bộ weights VieNeu v3 Nano từ thư mục project
và bật target worker dành cho TTS trong `.env`.

```powershell
python setup_tts_model.py models/tts
```

```dotenv
VIDEO_WORKER_TARGET=tts-worker
TTS_ENABLED=true
TTS_MODEL_HOST_DIR=./models/tts
TTS_MODEL_DIR=/data/models/tts
```

Build lại worker bằng `docker compose up -d --build video-worker`. Target
TTS cài SDK CPU `vieneu==3.5.4` và mount weights read-only. Giữ
`TTS_VOICE=default` để dùng preset mặc định của SDK. Các tên giọng khác phải
khớp preset của bản SDK đang cài. Worker chỉ đọc model từ disk. Khi thiếu
weights, chạy lại script setup trước khi gửi job. [Giấy phép model và preset](https://huggingface.co/pnnbao-ump/VieNeu-TTS-v3-Nano/blob/a239fbfe19ca572f1d5f4316521806ddacc0b279/README.md)

Request cũ dùng mặc định hiện hữu khi TTS tắt. Request có thể bật/tắt
voiceover tổng hợp bằng `narration.enabled`. Khi cung cấp `voiceover`, worker
dùng file đó. Nếu cần nội dung riêng, đặt `narration.text` hoặc
`narration.sceneTexts`. Nếu không, worker dùng lời dẫn của storyboard đã lưu
hoặc template LOCAL. `sceneTexts` theo thứ tự cảnh, chuỗi rỗng tạo khoảng
im lặng. `narration.allowFallback=true` cho phép dựng thiếu lời đọc khi TTS lỗi
và ghi mã nguyên nhân. Giọng tổng hợp có phụ đề ASS mặc định, dùng timing
theo segment đo được. Tắt phụ đề bằng `narration.subtitles=false`.

LOCAL offline dùng `AI_DIRECTOR_ENABLED=false` và asset UUID đã upload.
LOCAL bật Groq vẫn gọi Groq để viết kịch bản. HYBRID tiếp tục dùng budget,
polling và clip cache hiện có. TTS dùng CPU local trong cả ba cách vận hành.
Worker hiện có deadline Celery cho cả phần TTS và render. Nếu job thường
xuyên chạm giới hạn, đo từng bước trước khi chỉnh `TTS_TIMEOUT_SECONDS` và
`TASK_SOFT_TIMEOUT`, `TASK_HARD_TIMEOUT`, `REDISPATCH_AFTER`.

## AI Director với Groq

`AI_DIRECTOR_ENABLED=false` giữ storyboard Python hiện tại. Với LOCAL, cờ
này tắt đồng nghĩa worker không gọi Groq hay WaveSpeedAI. Worker vẫn có thể
tải media HTTPS mà client cung cấp. Bật `AI_DIRECTOR_ENABLED=true` và đặt
`GROQ_API_KEY` để Groq lên kịch bản cho cả LOCAL và HYBRID. `tour.targetAudience`
là trường tùy chọn. Client cũ có thể tiếp tục gửi payload như trước.

Mặc định `GROQ_MODEL=openai/gpt-oss-120b`. Worker kiểm tra model qua
`GET /openai/v1/models` của tài khoản trước completion. Nếu model không khả
dụng, chọn một model từ danh sách bằng `GROQ_MODEL`. Model hỗ trợ strict dùng
JSON Schema. Các model khác dùng JSON mode và qua cùng bước kiểm Pydantic.
Worker không tự chuyển model hoặc gói thanh toán. [Danh sách model](https://console.groq.com/docs/models) · [Structured Outputs](https://console.groq.com/docs/structured-outputs) · [API Groq](https://console.groq.com/docs/api-reference)

Storyboard JSON gồm tiêu đề, ý tưởng và danh sách cảnh theo giây. Ví dụ,
mỗi cảnh có `sceneId`, `startSeconds`, `durationSeconds`, `type`,
`assetIndex`, `videoPrompt`, `narration`, `caption` và `transition`.
Groq nhận metadata tour đã giới hạn và chỉ số media đã kiểm tra. Worker
quyết định giới hạn AI từ ngân sách và đơn giá provider, sau đó kiểm lại trước
submit. AI scene dùng prompt riêng tiếng Anh và ảnh JPEG chuẩn hóa. Các clip
WaveSpeed do Director điều phối tắt generated audio.

Worker lưu storyboard nội bộ trong metadata PostgreSQL của job. Storyboard
đã lưu được dùng lại khi render retry hoặc poll WaveSpeed. Bản ghi lưu prompt
theo slot và token usage Groq nếu có. Dự toán và chi phí trong response REST
tiếp tục dành cho video AI. Kết quả MP4 lưu thêm `narrationTimeline` tiếng Việt
theo frame (30 FPS) trong metadata, sẵn cho Phase 2 đo và khớp audio.

Với `AI_DIRECTOR_FALLBACK_ENABLED=true`, lỗi Groq hoặc kịch bản không hợp lệ
chuyển sang storyboard Python và log `TEMPLATE_FALLBACK`. Kịch bản hợp lệ
log `GROQ_DIRECTOR`. Mỗi job mới thường dùng một completion. Mặc định worker
có tối đa một lần retry lỗi tạm thời trong cửa sổ 60 giây và 4.096 completion
tokens. Có thể điều chỉnh trong `.env.example`. Tắt AI Director nếu cần
chạy worker mà không gửi metadata tour đến Groq.

## HYBRID với WaveSpeedAI

`VIDEO_PROVIDER=wavespeed` chọn provider cho job HYBRID mới. LOCAL không cần
API key. Một job đã có AI generation row dùng provider, endpoint, model, độ phân
giải và thời lượng đã lưu. Có thể giữ WAN_API_KEY ở worker để poll job Wan đang
chạy trong lúc chuyển provider. Lỗi WaveSpeed chỉ đi theo chính sách LOCAL
fallback của job.

Các mặc định trong `.env.example` tắt submit trả phí. Worker chỉ submit khi có
`WAVESPEED_API_KEY`, `WAVESPEED_ENABLED=true`, `WAVESPEED_DRY_RUN=false`,
`WAVESPEED_LIVE_TEST_ENABLED=true` và ngân sách job hợp lệ. Cấu hình mặc định
dùng 720p, 5 giây, tắt audio và prompt expansion, concurrency 2, HTTP timeout
30 giây, polling 15 giây và deadline 900 giây. Secret được cấp cho worker. API
container ghi đè key bằng giá trị rỗng.

Worker tải/đọc asset HTTPS hoặc UUID của tenant, chuẩn hóa JPEG và gửi qua
endpoint binary upload được WaveSpeed hỗ trợ. URL trả về chỉ dùng làm ảnh đầu
vào prediction. WaveSpeed công bố giữ ảnh tải lên 7 ngày. API key chỉ đi tới
host API. Worker tải clip qua downloader kiểm HTTPS, DNS/TLS, giới hạn byte và
không theo redirect. [Upload API](https://wavespeed.ai/docs/upload-files-api)

Theo bảng giá model, một clip 5 giây 720p không audio dự toán 0.1250 USD.
Worker ghi dự toán theo slot, kiểm lại đơn giá trước POST và giữ
`aiCostUncertain` khi không có bằng chứng billing. Với prediction thành công
chưa có số liệu billing, `actualCostUsd` dùng dự toán theo quy ước API hiện
tại. Prediction lỗi hoặc hủy vẫn có thể mang cờ bất định. Giữ cấu hình prompt
expansion ổn định khi còn slot WaveSpeed chưa gửi vì DB lưu thông số clip,
còn tùy chọn này được lấy từ worker. [Bảng giá và API model](https://wavespeed.ai/docs/docs-api/alibaba/wan-2.6/image-to-video-flash)

Kiểm thử WaveSpeed dùng HTTP mock trong `tests/test_wavespeed.py`. Chưa xác
minh kết quả completed, URL CDN thực tế và hóa đơn bằng tài khoản triển khai.

## HYBRID với Wan 2.6 Flash (legacy)

Chọn `VIDEO_PROVIDER=wan` nếu cần dùng Wan cho job mới. Phần còn lại của mục
này mô tả cấu hình DashScope.

`HYBRID` dùng cùng cấu hình đầu ra 30, 35 hoặc 40 giây như `LOCAL`. Đặt `aiScenes` thành 0, 1 hoặc 2 (mặc định 2). Với hai cảnh 5 giây, video 40 giây có 30 giây nguồn thật và 10 giây từ Wan. Worker chuẩn hóa và ghép hình trước khi chèn phụ đề, logo và audio. Audio từ Wan bị loại và phần âm thanh dùng pipeline local.

Ví dụ payload Postman. Thay hai UUID bằng asset ảnh đã upload trong cùng tenant, hoặc dùng URL HTTPS đã được service chấp nhận.

```json
{
  "requestId": "da-nang-hybrid-1",
  "productId": "tour-da-nang-3n2d",
  "tour": {
    "title": "Đà Nẵng 3 ngày 2 đêm",
    "highlights": ["Sơn Trà", "Biển Mỹ Khê"],
    "priceVnd": 2990000
  },
  "images": ["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"],
  "config": {"mode": "HYBRID", "durationSeconds": 40, "aspectRatio": "9:16"},
  "aiScenes": 2,
  "maxAiCostUsd": "0.2500",
  "allowLocalFallback": true
}
```

- Nếu có `aiReferenceImages`, worker chỉ dùng danh sách đó để chọn ảnh tham chiếu. Mỗi cảnh cần một ảnh hợp lệ riêng. Worker kiểm tra ảnh, chuẩn hóa JPEG rồi gửi qua Base64. UUID upload và URL nguồn không được chuyển cho Alibaba.
- `allowLocalFallback` mặc định `true`. Khi thiếu ảnh hoặc provider gặp lỗi, worker có thể dựng bằng nguồn local với cùng thời lượng. Kiểm tra `executionMode` và `fallbackReason` để phân biệt output thật sự dùng AI với fallback. `mode` luôn là chế độ yêu cầu. `aiScenes: 0` dựng LOCAL có chủ đích.
- `maxAiCostUsd` là hạn mức cho toàn job, dù có hai cảnh. Job cần hạn mức rõ ràng để được gửi lên Wan. Nếu dự toán vượt hạn mức, job trả `AI_BUDGET_EXCEEDED` trước khi gọi provider. Phí dự toán dựa trên model, khu vực, độ phân giải, thời lượng và số cảnh, được làm tròn lên từng cảnh. Phí thực tế được tính từ số giây provider báo đã xử lý nhân đơn giá đã chốt trong PostgreSQL. `aiCostUncertain: true` báo rằng còn khoản phí chưa xác nhận hoặc số giây chưa được provider trả về. So sánh với hóa đơn Model Studio khi cần đối soát.

### Cấu hình Wan

Tạo API key trong Alibaba Cloud Model Studio ở đúng khu vực/workspace của endpoint và đặt vào `WAN_API_KEY` trong `.env`. Worker giữ key và gọi API. [Hướng dẫn tạo API key](https://www.alibabacloud.com/help/en/model-studio/get-api-key) · [Endpoint theo khu vực](https://www.alibabacloud.com/help/en/model-studio/regions) · [API Wan Image-to-Video](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/legacy-image-to-video-api-reference)

| Biến | Mặc định và vai trò |
| --- | --- |
| `WAN_ENABLED` | `false`. Feature flag cho submit mới. |
| `WAN_DRY_RUN` | `true`. Có thể dùng tên `DRY_RUN` nếu không đặt `WAN_DRY_RUN`. Dry run tính dự toán và đi theo chính sách fallback, không gửi POST lên Wan. |
| `WAN_LIVE_TEST_ENABLED` | `false`. Phải bật rõ ràng để cho phép mọi lần submit có thể tính phí. |
| `WAN_API_KEY` | Secret của worker, lấy từ workspace tương ứng. |
| `WAN_BASE_URL` | `https://dashscope-intl.aliyuncs.com` (Singapore). Hỗ trợ Beijing tại `https://dashscope.aliyuncs.com` và endpoint workspace chính thức của hai khu vực. Chấp nhận tiền tố tùy chọn `/api/v1`. |
| `WAN_MODEL` | `wan2.6-i2v-flash`. |
| `WAN_RESOLUTION` | `720P`. Có thể cấu hình `1080P`. |
| `WAN_DEFAULT_DURATION` | `5` giây cho mỗi clip. |
| `WAN_MAX_CONCURRENT_TASKS` | `2`, giới hạn dùng chung trong PostgreSQL. |
| `WAN_PRICE_PER_SECOND_USD` | Tùy chọn, nâng mức dự toán trên giá niêm yết nếu cần. Hãy kiểm tra giá hiện hành trước khi bật gửi thật. [Bảng giá model](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/wan2-6-i2v-flash) |
| `WAN_POLL_INTERVAL_SECONDS` | `15` giây giữa các poll bình thường. |
| `WAN_TASK_TIMEOUT_SECONDS` | `900` giây cho giai đoạn cloud của mỗi job, gồm thời gian chờ slot và poll. |
| `WAN_HTTP_TIMEOUT_SECONDS` | `15` giây cho HTTP request API Wan. |

Provider gửi `audio: false`, `watermark: false` và lưu `task_id` riêng theo cảnh. Worker lên lịch poll qua outbox, áp dụng backoff và `Retry-After` khi lỗi tạm thời. Khi có kết quả, worker tải ngay từ domain OSS allowlist với kiểm tra HTTPS, DNS và kích thước. Không tự động tạo lại cảnh. Nếu mất kết nối sau khi đã đánh dấu submit và chưa nhận được ID, worker đánh dấu kết quả chưa rõ, giữ chỗ concurrency tối đa 24 giờ và áp dụng chính sách fallback.

### Kiểm thử Wan riêng

Chạy kiểm thử hợp đồng HTTP và tích hợp HYBRID bằng mock. Các bài này dùng MP4 thật làm fixture và không gọi Alibaba.

```sh
docker compose run --rm --build video-tests pytest -q -p no:cacheprovider \
  tests/test_wan.py tests/test_hybrid.py
```

Để chạy thử trả phí có chủ đích, cấu hình key, endpoint và bảng giá đúng khu vực. Bật `WAN_ENABLED=true`, `WAN_DRY_RUN=false` và `WAN_LIVE_TEST_ENABLED=true` rồi cập nhật worker. Gửi một job với `aiScenes: 1`, `allowLocalFallback: false`, ảnh tham chiếu đã xác thực và `maxAiCostUsd` bằng hạn mức đã duyệt. Theo dõi GET status đến khi `executionMode` là `HYBRID` và `status` là `COMPLETED`. Tắt `WAN_LIVE_TEST_ENABLED` sau phép thử để chặn submit mới.

## Worker và kiểm thử

PostgreSQL giữ trạng thái chính. Beat quét transactional outbox sau commit. Advisory lock theo job ngăn chạy song song cùng job. Worker retry lỗi tạm thời qua backoff mũ và giới hạn số lần. Khi worker bị gián đoạn, dispatcher phát lại job chưa kết thúc sau `REDISPATCH_AFTER`. Chạy Beat trên một worker khi mở rộng số bản sao. Dùng cấu hình `TASK_SOFT_TIMEOUT < TASK_HARD_TIMEOUT < REDISPATCH_AFTER`.

```sh
docker compose run --rm --build video-tests
docker compose run --rm video-tests ruff check .
```

Bộ test tạo ảnh hình học, clip FFmpeg và âm thanh tổng hợp tại runtime. Suite render và kiểm tra LOCAL 30/40 giây cùng HYBRID 40 giây với Wan mock. PostgreSQL kiểm thử chạy riêng. Nếu chạy Pytest trên máy, cài Python 3.11+, FFmpeg/FFprobe, font DejaVu Sans và `pip install -e '.[test]'`. Cấu hình `FONT_PATH` nếu font nằm ngoài đường dẫn mặc định và đặt `TEST_DATABASE_URL` trỏ đến PostgreSQL kiểm thử có quyền tạo schema.

