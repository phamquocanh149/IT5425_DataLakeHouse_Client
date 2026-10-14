# Kết quả Phase 1 — 2026-10-14

- Windows build 26200 / 25H2. Python mặc định của shell: 3.14.7; môi trường Conda
  `course`: 3.8.20. Client dùng `.venv` riêng **Python 3.11.0**.
- Thư mục `climate-platform` được đọc để đối chiếu API, không sửa Server.
- Không tìm thấy Tailscale qua PATH hoặc vị trí cài đặt chuẩn.
- Kết nối PyPI ban đầu bị sandbox chặn socket (WinError 10013). Cài đặt thành công
  khi chạy với quyền mạng được công cụ chấp thuận; không thay đổi Firewall.
- Thư viện chính đã cài: httpx 0.28.1, python-dotenv 1.2.4, pytest 9.1.1.
  Toàn bộ phiên bản trong `requirements-lock.txt`.
- Lệnh `.venv\Scripts\python.exe -m pytest -q`: **34 passed in 0.56s**.
  Bao gồm cấu hình/URL/key, checksum đọc chunk, validation file/source/ngày,
  multipart và authentication, lỗi 400/401/403/409/413/422, bounded retry/backoff,
  mất response rồi nhận DUPLICATE, checksum sai, response không hợp lệ,
  duplicate khác source, redaction credentials và trạng thái pending/uploaded.
- `scripts/send_file.py --help` chạy thành công.
- `scripts/check_server.py` với URL trống trả exit code 1 và thông báo cấu hình sai,
  không phát request. Đây là kiểm tra CLI, không phải kiểm tra Server thật.
- Đã tạo `data/pending/sample_usdm.csv` (78 byte) và `sample_usgs.csv` (83 byte).
- **Chưa có upload thật hoặc xác minh `/health`/OpenAPI trên Server đang chạy**,
  theo quyết định bỏ qua kết nối của người dùng. Không khẳng định tương thích runtime.
- Còn cần điền URL/key, chạy Server, xác minh cấu hình giới hạn thực tế, gửi mẫu
  dưới 1 MB và kiểm tra metadata trước khi vận hành định kỳ.

Các file triển khai: `client/{config,api_client,uploader,checksum,retry,metadata}.py`,
ba CLI trong `scripts/`, bốn skeleton trong `collectors/`, test suite trong `tests/`,
`.env` cục bộ, `.env.example`, `.gitignore`, `pytest.ini`, hai requirements và README.
Các thư mục data/logs có `.gitkeep`; dữ liệu runtime và credentials được ignore.
