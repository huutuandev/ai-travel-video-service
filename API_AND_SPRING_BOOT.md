# Hợp đồng API và tích hợp Java Spring Boot

## REST API

Các đường dẫn dưới `/api/v1/videos` yêu cầu `X-Service-API-Key` của tenant. Lấy tài liệu máy đọc tại `/openapi.json` và giao diện tại `/docs`.

| Phương thức | Đường dẫn | Kết quả |
|---|---|---|
| GET | `/health/live` | 200 với `status=ok` |
| GET | `/health/ready` | 200 với `status=ready`, hoặc 503 khi DB/Redis chưa sẵn sàng |
| POST | `/api/v1/videos/assets` | 201 với `assetId`, body binary và Content-Type phù hợp |
| POST | `/api/v1/videos/generate` | 202 với `jobId`; 409 nếu cùng tenant + requestId và payload khác |
| GET | `/api/v1/videos/{jobId}` | 200 với trạng thái job, 404 khi không tìm thấy job của tenant |
| GET | `/api/v1/videos?page=1&pageSize=20` | Danh sách phân trang của tenant |
| GET | `/api/v1/videos/{jobId}/download` | MP4 khi COMPLETED, 404 nếu chưa có output hoặc tenant khác |

401 chỉ ra key thiếu hoặc không hợp lệ. 422 là validation ở API. Một số lỗi budget hoặc asset chỉ được phát hiện ở worker sau HTTP 202. Kiểm `status`, `errorCode` và `errorMessage` từ job detail. Gửi lại cùng `requestId` và payload cho timeout khi submit. Khi tạo video theo yêu cầu khác, cấp ID mới.

`mode` là yêu cầu ban đầu. `executionMode` là đường render thực tế và `fallbackReason` mô tả fallback. Với canary HYBRID, chỉ xem là đạt khi `executionMode=HYBRID`. Đọc `estimatedCostUsd` và `actualCostUsd` bằng BigDecimal. Khi `aiCostUncertain=true`, đối soát với provider để phân biệt estimate và khoản billed. Chi phí Groq không nằm trong các trường USD video này.

## Request JSON

Các ví dụ dùng UUID minh họa. Upload media trong cùng tenant hoặc dùng URL HTTPS công khai đã kiểm soát. Director, TTS và các cờ trả phí được cấu hình ở worker qua môi trường.

### 1. LOCAL offline 40 giây

Tắt Director và các video provider trong worker, bật TTS và thay UUID bằng kết quả upload.

```json
{
  "requestId": "travel-example-1",
  "productId": "tour-da-nang",
  "tour": {
    "title": "Khám phá Đà Nẵng",
    "destination": "Đà Nẵng",
    "priceVnd": 2990000,
    "callToAction": "Liên hệ để đặt tour"
  },
  "images": [
    "11111111-aaaa-4111-8111-111111111111"
  ],
  "config": {
    "mode": "LOCAL",
    "durationSeconds": 40,
    "aspectRatio": "16:9"
  },
  "narration": {
    "enabled": true
  }
}
```

### 2. LOCAL với Groq

Bật AI_DIRECTOR_ENABLED ở worker sau khi tài khoản được duyệt. Mode LOCAL giữ video provider ngoài luồng.

```json
{
  "requestId": "travel-example-2",
  "productId": "tour-da-nang",
  "tour": {
    "title": "Khám phá Đà Nẵng",
    "destination": "Đà Nẵng",
    "priceVnd": 2990000,
    "callToAction": "Liên hệ để đặt tour"
  },
  "images": [
    "11111111-aaaa-4111-8111-111111111111"
  ],
  "config": {
    "mode": "LOCAL",
    "durationSeconds": 40,
    "aspectRatio": "16:9"
  },
  "narration": {
    "enabled": true
  }
}
```

### 3. LOCAL custom narration

sceneTexts theo thứ tự các cảnh thực tế. Có thể dùng text thay cho sceneTexts.

```json
{
  "requestId": "travel-example-3",
  "productId": "tour-da-nang",
  "tour": {
    "title": "Khám phá Đà Nẵng",
    "destination": "Đà Nẵng",
    "priceVnd": 2990000,
    "callToAction": "Liên hệ để đặt tour"
  },
  "images": [
    "11111111-aaaa-4111-8111-111111111111"
  ],
  "config": {
    "mode": "LOCAL",
    "durationSeconds": 40,
    "aspectRatio": "16:9"
  },
  "narration": {
    "enabled": true,
    "sceneTexts": [
      "Cùng khám phá Đà Nẵng.",
      "Liên hệ để đặt tour."
    ]
  }
}
```

### 4. HYBRID một clip

Canary có thể phát sinh phí. Bật các cờ submit chỉ sau khi được duyệt, kiểm executionMode=HYBRID.

```json
{
  "requestId": "travel-example-4",
  "productId": "tour-da-nang",
  "tour": {
    "title": "Khám phá Đà Nẵng",
    "destination": "Đà Nẵng",
    "priceVnd": 2990000,
    "callToAction": "Liên hệ để đặt tour"
  },
  "images": [
    "11111111-aaaa-4111-8111-111111111111"
  ],
  "config": {
    "mode": "HYBRID",
    "durationSeconds": 40,
    "aspectRatio": "16:9"
  },
  "narration": {
    "enabled": true
  },
  "aiScenes": 1,
  "maxAiCostUsd": "0.1250",
  "allowLocalFallback": false
}
```

### 5. HYBRID hai clip

Hai ảnh tham chiếu riêng biệt, tổng dự toán 0,2500 USD tại profile 720p/5s/audio off.

```json
{
  "requestId": "travel-example-5",
  "productId": "tour-da-nang",
  "tour": {
    "title": "Khám phá Đà Nẵng",
    "destination": "Đà Nẵng",
    "priceVnd": 2990000,
    "callToAction": "Liên hệ để đặt tour"
  },
  "images": [
    "11111111-aaaa-4111-8111-111111111111",
    "22222222-aaaa-4222-8222-222222222222"
  ],
  "config": {
    "mode": "HYBRID",
    "durationSeconds": 40,
    "aspectRatio": "16:9"
  },
  "narration": {
    "enabled": true
  },
  "aiScenes": 2,
  "maxAiCostUsd": "0.2500",
  "allowLocalFallback": false
}
```

### 6. Portrait

Output dọc 1080×1920.

```json
{
  "requestId": "travel-example-6",
  "productId": "tour-da-nang",
  "tour": {
    "title": "Khám phá Đà Nẵng",
    "destination": "Đà Nẵng",
    "priceVnd": 2990000,
    "callToAction": "Liên hệ để đặt tour"
  },
  "images": [
    "11111111-aaaa-4111-8111-111111111111"
  ],
  "config": {
    "mode": "LOCAL",
    "durationSeconds": 35,
    "aspectRatio": "9:16"
  },
  "narration": {
    "enabled": true
  }
}
```

### 7. Landscape

Output ngang 1920×1080, ví dụ FFmpeg thuần khi Director tắt.

```json
{
  "requestId": "travel-example-7",
  "productId": "tour-da-nang",
  "tour": {
    "title": "Khám phá Đà Nẵng",
    "destination": "Đà Nẵng",
    "priceVnd": 2990000,
    "callToAction": "Liên hệ để đặt tour"
  },
  "images": [
    "11111111-aaaa-4111-8111-111111111111"
  ],
  "config": {
    "mode": "LOCAL",
    "durationSeconds": 30,
    "aspectRatio": "16:9"
  },
  "narration": {
    "enabled": false
  }
}
```

### 8. Có nhạc nền

backgroundMusic dùng UUID audio tenant đã upload.

```json
{
  "requestId": "travel-example-8",
  "productId": "tour-da-nang",
  "tour": {
    "title": "Khám phá Đà Nẵng",
    "destination": "Đà Nẵng",
    "priceVnd": 2990000,
    "callToAction": "Liên hệ để đặt tour"
  },
  "images": [
    "11111111-aaaa-4111-8111-111111111111"
  ],
  "config": {
    "mode": "LOCAL",
    "durationSeconds": 40,
    "aspectRatio": "16:9"
  },
  "narration": {
    "enabled": true
  },
  "backgroundMusic": "33333333-aaaa-4333-8333-333333333333"
}
```

### 9. Có phụ đề

Bật TTS và SUBTITLE_ENABLED ở worker. Subtitle được burn-in từ speech đã tổng hợp.

```json
{
  "requestId": "travel-example-9",
  "productId": "tour-da-nang",
  "tour": {
    "title": "Khám phá Đà Nẵng",
    "destination": "Đà Nẵng",
    "priceVnd": 2990000,
    "callToAction": "Liên hệ để đặt tour"
  },
  "images": [
    "11111111-aaaa-4111-8111-111111111111"
  ],
  "config": {
    "mode": "LOCAL",
    "durationSeconds": 40,
    "aspectRatio": "16:9"
  },
  "narration": {
    "enabled": true,
    "subtitles": true
  }
}
```

## Java Spring Boot

Cấu hình `TRAVEL_VIDEO_BASE_URL` và `TRAVEL_VIDEO_SERVICE_API_KEY` qua secret store của backend. Dùng loopback khi backend chạy trên host cùng máy. Trong một network Compose chung, dùng host `video-api:8000`. Giữ key theo tenant và chỉ truyền vào HTTP client phía server.

Ví dụ dưới dùng Java 17, Jackson và JDK HttpClient. Gọi `status` từ scheduler của Spring hoặc background workflow với deadline hữu hạn, ví dụ kiểm mỗi 3–10 giây và dừng khi terminal. Lưu jobId ngay sau submit. Mỗi request HTTP đã có timeout riêng.

```java
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.UUID;

final class TravelVideoClient {
    private final URI base;
    private final String apiKey;
    private final ObjectMapper json;
    private final HttpClient client = HttpClient.newBuilder()
        .connectTimeout(Duration.ofSeconds(5)).build();

    TravelVideoClient(URI base, String apiKey, ObjectMapper json) {
        this.base = base;
        this.apiKey = apiKey;
        this.json = json;
    }

    private HttpRequest.Builder request(String path, Duration timeout) {
        return HttpRequest.newBuilder(base.resolve(path))
            .timeout(timeout).header("X-Service-API-Key", apiKey);
    }

    UUID submit(JsonNode payload) throws IOException, InterruptedException {
        var response = client.send(request("/api/v1/videos/generate", Duration.ofSeconds(15))
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(json.writeValueAsString(payload))).build(),
            HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 202)
            throw new IOException("Video submit HTTP " + response.statusCode());
        return UUID.fromString(json.readTree(response.body()).path("jobId").asText());
    }

    JsonNode status(UUID jobId) throws IOException, InterruptedException {
        var response = client.send(request("/api/v1/videos/" + jobId, Duration.ofSeconds(10))
            .GET().build(), HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 200)
            throw new IOException("Video status HTTP " + response.statusCode());
        return json.readTree(response.body());
    }

    Path download(UUID jobId, Path target) throws IOException, InterruptedException {
        String path = "/api/v1/videos/" + jobId + "/download";
        var response = client.send(request(path, Duration.ofMinutes(2)).GET().build(),
            HttpResponse.BodyHandlers.ofInputStream());
        try (var body = response.body()) {
            if (response.statusCode() != 200)
                throw new IOException("Video download HTTP " + response.statusCode());
            Files.copy(body, target);
            return target;
        }
    }
}
```

Trên trạng thái FAILED, lưu mã lỗi để phân loại retry rồi hiển thị thông báo phù hợp. HTTP 409 cần payload gốc hoặc ID mới theo ý định người dùng. HTTP 401/404 cần kiểm quyền và tenant trước khi retry. HTTP timeout của client không đồng nghĩa job bị hủy. API hiện không có endpoint hủy. Chỉ mở tải khi COMPLETED và cast tiền từ chuỗi JSON bằng `new BigDecimal(value)` khi trường khác null.

