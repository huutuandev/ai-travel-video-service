# Báo cáo Local TTS và pipeline video

## 1. Implementation Summary

Phase 2 thêm voiceover tiếng Việt chạy CPU, tạo WAV theo từng cảnh và burn-in phụ đề ASS vào MP4. Giọng đọc ưu tiên voiceover upload, custom narration, storyboard Groq đã lưu, rồi template Python. LOCAL và HYBRID dùng chung pipeline FFmpeg. Các tích hợp Groq và WaveSpeedAI tiếp tục qua luồng Phase 1 và Phase 1.1. [Điểm tích hợp render](app/services/local_video.py) · [Worker](app/services/execution.py)

## 2. Selected TTS Engine

Chọn **VieNeu-TTS v3 Nano**, backend ONNX Runtime CPU, 48 triệu tham số, audio mono 24 kHz và bundled presets tiếng Việt. Runtime dùng `Vieneu(mode="v3nano", onnx_dir=..., threads=2)`. `default` dùng preset mặc định của SDK. Tên giọng khác được xác thực từ danh sách preset khi worker synthesize. Đã đánh giá Piper `vi_VN/vais1000`, model khoảng 63 MB và dataset CC BY 4.0. VieNeu Nano được chọn vì nhà phát hành xác nhận rõ quyền sử dụng thương mại của preset và audio xuất ra. [VieNeu Nano](https://huggingface.co/pnnbao-ump/VieNeu-TTS-v3-Nano) · [Mã nguồn SDK](https://github.com/pnnbao97/VieNeu-TTS/blob/main/src/vieneu/v3nano.py) · [Piper model card](https://huggingface.co/rhasspy/piper-voices/blob/v1.0.0/vi/vi_VN/vais1000/medium/MODEL_CARD)

## 3. Model Version and License

| Thành phần | Phiên bản và giấy phép |
|---|---|
| SDK | `vieneu==3.5.4`, Apache-2.0. Python Docker 3.12, dự án yêu cầu Python ≥3.11 |
| Model | `pnnbao-ump/VieNeu-TTS-v3-Nano` tại revision `a239fbfe19ca572f1d5f4316521806ddacc0b279`, Apache-2.0 |
| Preset voice | Bundled trong SDK, model card xác nhận Apache-2.0 và quyền dùng audio thương mại |
| Dung lượng | 281.756.486 byte cho sáu file inference preset đã xác minh tại `models/tts`. Script setup chỉ tải các file đó |

Nguồn của model ghi rõ đây là bản preview. Chất lượng giọng quảng cáo tiếng Việt nên được đánh giá bằng sample thực tế trước khi dùng ngoài môi trường test. [Model card ghim revision](https://huggingface.co/pnnbao-ump/VieNeu-TTS-v3-Nano/blob/a239fbfe19ca572f1d5f4316521806ddacc0b279/README.md) · [SDK 3.5.4](https://pypi.org/project/vieneu/3.5.4/) · [Danh sách weights](https://huggingface.co/pnnbao-ump/VieNeu-TTS-v3-Nano/tree/a239fbfe19ca572f1d5f4316521806ddacc0b279)

## 4. Hardware Requirements

Đường inference dùng CPU, OpenMP/ORT với hai threads cho mỗi process. RTX 3050 và VRAM 4 GB không nằm trên đường xử lý. SDK kéo ONNX Runtime, NumPy, SoundFile và phonemizer SEA-G2P, cùng dependencies đóng gói của release như Gradio và Librosa. Chúng chỉ cài trong target worker TTS. Nhà phát hành công bố Nano khoảng RTF 0,22 ở 16 bước trên Core i5 desktop đời 12, số này là benchmark upstream. RAM thực tế phụ thuộc process giữ cache, độ dài văn bản và tải FFmpeg. Với Docker Desktop, có thể bắt đầu với 4 GB RAM cho toàn bộ stack làm mức dự trù, rồi đo RSS và đặt lại concurrency. [Tài liệu VieNeu](https://github.com/pnnbao97/VieNeu-TTS) · [Worker target](Dockerfile)

## 5. Files Created and Modified

| Nhóm | File |
|---|---|
| Engine và tải model có chủ đích | [tts.py](app/services/tts.py), [setup_tts_model.py](setup_tts_model.py) |
| Timeline audio, chuẩn hóa và phụ đề | [narration.py](app/services/narration.py), [text_normalization.py](app/services/text_normalization.py), [subtitles.py](app/services/subtitles.py) |
| Tích hợp job và renderer | [local_video.py](app/services/local_video.py), [execution.py](app/services/execution.py), [ffmpeg.py](app/renderers/ffmpeg.py) |
| Cấu hình và triển khai | [video schema](app/schemas/video.py), [config.py](app/core/config.py), [.env.example](.env.example), [pyproject.toml](pyproject.toml), [Dockerfile](Dockerfile), [Compose](docker-compose.yml), [.gitignore](.gitignore), [.dockerignore](.dockerignore), [README](README.md) |
| Test | [test_tts.py](tests/test_tts.py), [test_narration.py](tests/test_narration.py), [test_subtitles.py](tests/test_subtitles.py), [test_text_normalization.py](tests/test_text_normalization.py), [test_local_video.py](tests/test_local_video.py), [test_hybrid.py](tests/test_hybrid.py), [test_execution.py](tests/test_execution.py), [conftest.py](tests/conftest.py) |

## 6. Groq Narration Integration

Worker đọc `Shot.narration` từ storyboard đã persist ở Phase 1.1. Nếu không có storyboard, template tận dụng caption/tour metadata và CTA theo timeline hiện tại. `narration.text` phân phối câu vào slot trước khi tổng hợp. `sceneTexts` đặt văn bản tương ứng trực tiếp theo thứ tự cảnh và cho phép chuỗi rỗng để giữ im lặng. `voiceover` upload được ưu tiên và dùng pipeline trước đây. [Phase 1.1](GROQ_AI_DIRECTOR_REPORT.md) · [Storyboard](app/services/storyboard.py) · [Nguồn narration](app/services/narration.py)

## 7. Vietnamese Text Normalization

Chuẩn hóa NFC, khoảng trắng, tiền VND, số nguyên/thập phân, phần trăm, ngày dương lịch hợp lệ, shorthand ngày/đêm, TP., km, kg và mét. Số điện thoại đọc từng chữ số. Mã chữ-số giữ nguyên. Ví dụ `2.990.000 VNĐ` thành `hai triệu chín trăm chín mươi nghìn đồng` trong âm thanh, còn caption và subtitle giữ cách viết gốc. [Chuẩn hóa](app/services/text_normalization.py) · [Ví dụ test](tests/test_text_normalization.py)

## 8. Audio Generation Pipeline

Mỗi scene có WAV riêng và metadata ghi original/normalized text, duration FFprobe và thời điểm bắt đầu/kết thúc. WAV nguồn hợp lệ được lưu staging theo job và hash nội dung, preset và revision, sau đó dùng lại khi resume. Worker dùng subprocess tồn tại qua các cảnh để cache model. Lock filesystem trên volume media mặc định chỉ cho phép một inference đồng thời trong deployment. Deadline tính theo batch TTS, gồm cả thời gian chờ slot. Khi audio dài, worker thử bỏ cụm mở đầu chung một lần, dùng `atempo` tối đa 1,18× và đo lại. Nếu vẫn vượt ngân sách thời gian của cảnh, job báo lỗi rõ ràng hoặc bỏ giọng theo `allowFallback`. [TTS](app/services/tts.py) · [Audio segments](app/services/narration.py)

## 9. Subtitle Generation

ASS UTF-8 với DejaVu Sans, viền tối, tối đa hai dòng mỗi cue và vùng riêng dưới marketing caption. Cue dùng khoảng thời gian audio đo được. Khi một segment cần nhiều cue, phân bổ khoảng nội bộ theo độ dài chữ, nên độ đồng bộ là segment-level với ước lượng trong segment. Text được escape trước khi ghi ASS. FFmpeg nhận tên file cố định từ workdir nội bộ. [ASS](app/services/subtitles.py) · [FFmpeg](app/renderers/ffmpeg.py)

## 10. Audio Mixing

Voice tổng hợp được chuẩn hóa peak theo segment và ghép PCM với khoảng nghỉ tự nhiên. FFmpeg hạ nhạc nền, duck theo voice khi bật, fade nhạc, limiter cho mix và pad/trim đúng thời lượng. Voice tổng hợp giữ khoảng nghỉ cuối, clip cung cấp bởi người dùng giữ cách xử lý voiceover trước đó. Nhạc lấy từ request và giọng phát được khi thiếu nhạc. [Audio](app/services/narration.py) · [Mixer](app/renderers/ffmpeg.py)

## 11. Final Video Rendering

Render hiện có tiếp tục xử lý Ken Burns, crossfade local, logo và CTA, dùng dynamic prompt cho hai AI slots khi HYBRID. Đầu ra MP4 H.264/AAC, YUV420P, 30 FPS, 1920×1080 hoặc 1080×1920, 30/35/40 giây, quality gates FFprobe/black/freeze chạy trước khi commit COMPLETED. Clip nguồn 720p có thể upscale để ghép output 1080p. [Local render](app/services/local_video.py) · [Validation](app/renderers/ffmpeg.py)

## 12. Configuration

| Biến | Mặc định |
|---|---|
| `TTS_ENABLED` | false |
| `TTS_PROVIDER`, `TTS_DEVICE`, `TTS_OFFLINE_ONLY` | vieneu, cpu, true |
| `TTS_MODEL_DIR`, `TTS_VOICE`, `TTS_SPEED` | /data/models/tts, default, 1.0 |
| `TTS_MAX_CONCURRENT`, `TTS_TIMEOUT_SECONDS` | 1, 180 |
| `SUBTITLE_ENABLED`, `SUBTITLE_FORMAT` | true, ass |
| `AUDIO_DUCKING_ENABLED` | true |
| `VIDEO_WORKER_TARGET`, `TTS_MODEL_HOST_DIR` | runtime, ./models/tts |

Bật `VIDEO_WORKER_TARGET=tts-worker` cùng `TTS_ENABLED=true` cho deployment dùng local TTS. Model mount read-only trong worker. Nếu thiếu weights, chạy script setup từ repo gốc trước. [Cấu hình](app/core/config.py) · [Compose](docker-compose.yml) · [Env ví dụ](.env.example)

## 13. API Compatibility

Request có object `narration` tùy chọn gồm `enabled`, `language`, `voice`, `speed`, `text` hoặc `sceneTexts`, `subtitles` và `allowFallback`. Speed giới hạn 0,90–1,15, custom text tổng tối đa 1.200 ký tự. Preset được xác thực tại worker. Các endpoint và schema DB đang dùng hỗ trợ thêm cấu hình này qua JSON payload và metadata asset. Client cũ giữ hành vi với defaults. [Request](app/schemas/video.py) · [Output metadata](app/services/local_video.py)

## 14. Test Results

- `docker compose config --quiet` trả mã 0. Ruff đạt cho `app`, `tests` và script setup. Pytest toàn bộ đạt **137 ca** bằng Docker Linux, PostgreSQL riêng và FFmpeg thật. Lần chạy có chọn lọc trên bộ test bổ sung đạt **19 ca**, gồm một ca mới xác nhận lỗi TTS bắt buộc kết thúc HYBRID với mã gốc. Có một cảnh báo tương thích Starlette TestClient.
- Bộ test automated mock engine TTS, Groq và WaveSpeedAI. Các ca kiểm render MP4 30/35/40 giây, landscape/portrait, upload voice ưu tiên, ASS theo timecode, ducking, peak mix, reuse cache khi resume và giữ lỗi audio khi xử lý HYBRID. Gate black/freeze và FFprobe chạy trên MP4 của bài tích hợp.
- **Inference thật:** chạy SDK `vieneu==3.5.4` và đúng weights đã ghim bằng CPU trong Docker với `--network none`. FFprobe xác nhận mẫu [phase2-vieneu-nano-vi.wav](artifacts/phase2-vieneu-nano-vi.wav) dài **2,710 giây**, PCM signed 16-bit, **24.000 Hz**, mono. Lần gọi đầu gồm tải model từ disk và synthesis mất 2,381 giây. Lần gọi thứ hai với câu khác, cùng process đã cache model, mất 0,679 giây. Peak RSS của subprocess được đo khoảng **481 MiB**. Smoke test dùng image test với SDK/model mount riêng. Cấu hình target worker mới được kiểm tĩnh bằng Compose.

## 15. Known Limitations

- Voiceover upload có độ ưu tiên cao nhất. Vì dạng này thiếu timing lời thoại, phụ đề burn-in tự động chỉ áp dụng cho đoạn tổng hợp local. SRT và giữ original audio clip là phần mở rộng tùy chọn.
- VieNeu v3 Nano là bản preview. Giọng tiếng Anh và đoạn nói trộn ngôn ngữ có thể kém tự nhiên hơn. Quy tắc số/mã tour là heuristic và người dùng có thể viết custom text theo cách đọc mong muốn.
- Worker đang dùng một cache model cho mỗi prefork đã từng synthesize. Lock giới hạn inference đồng thời trên cùng volume, còn RAM có thể giữ nhiều model cache nếu đặt nhiều process.
- Các lỗi render cao hơn TTS vẫn theo retry hiện tại. Crash giữa commit terminal và cleanup có thể để lại WAV staging. Workspace hiện tại thiếu metadata Git nên không thể xác minh `git status`.

## 16. Preparation for Docker and End-to-End Testing

Bộ model đã xác minh nằm tại `models/tts` và được bỏ qua trong Git/Docker build context. Trên máy khác, chạy `python setup_tts_model.py models/tts` để tải đúng revision, sau đó build worker target TTS. Kiểm tra LOCAL với asset UUID đã upload để toàn bộ pipeline AI chạy offline. Với LOCAL/Groq và HYBRID, giữ preset/cấu hình TTS như nhau trong worker, đối chiếu narration metadata, budget provider và chất lượng sample. TTS dùng model local trong cả ba cách vận hành. [Setup](setup_tts_model.py) · [Triển khai](README.md)

