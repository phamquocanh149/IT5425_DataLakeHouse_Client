# Climate Collector — Phase 1

Client Python 3.11/3.12 dùng chung cho USDM, NOAA, USGS và NLDAS. Hợp đồng API
được đối chiếu trực tiếp với `../climate-platform/server/{main,ingestion,security,metadata,config}.py`.
Không sửa mã Server. Theo yêu cầu hiện tại, bỏ qua kết nối và upload Server thật;
HTTP mocking không phải bằng chứng tương thích với một Server đang triển khai.

## Cài đặt và cấu hình

Trong PowerShell, clone repo và chạy các lệnh tại thư mục gốc:

```powershell
git clone https://github.com/phamquocanh149/IT5425_DataLakeHouse_Client.git
cd IT5425_DataLakeHouse_Client
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m pytest -q
```

Máy hiện tại đã có `.venv` riêng dùng Python 3.11.0, được tạo từ Python của
`climate-platform/.venv`. Không cần activate hoặc thay đổi ExecutionPolicy.
`requirements-lock.txt` ghi phiên bản đã cài để có thể tái tạo môi trường.

Sau khi sao chép `.env.example`, chọn `CLIENT_ROLE` phù hợp (dùng `all` cho cả bốn nguồn).
Điền `SERVER_BASE_URL` và `SERVER_API_KEY` được Server cấp. Không commit `.env`.
Biến môi trường hệ điều hành ưu tiên hơn `.env`; đường dẫn `.env` dựa vào vị trí
project nên dùng được trong Task Scheduler.

| Vai trò | CLIENT_ID (chỉ dùng cục bộ) | CLIENT_ROLE |
|---|---|---|
| Client 01 | client01 | usdm_noaa |
| Client 02 | client02 | nldas_usgs |
| Client dùng chung | local-client | all |

Vai trò `all` chỉ mở cả bốn nguồn ở Client. Server vẫn quyết định danh tính theo
API key; không gửi `CLIENT_ID` trong multipart. Dùng khóa Client 01 sẽ được Server
ghi nhận là Client 01, kể cả khi cấu hình local-client/all.

Nếu chạy Server **trên cùng máy này**, có thể đặt URL `http://127.0.0.1:8000` sau khi
bạn khởi động Server. Khi tách máy, phải đổi sang IP/hostname Server truy cập được
qua LAN/Tailscale; tuyệt đối không dùng localhost cho Server ở máy khác.
Client luôn kiểm tra chứng chỉ HTTPS và không tự theo redirect mang API key.

## Chạy thủ công

```powershell
.\.venv\Scripts\python.exe scripts/check_server.py
.\.venv\Scripts\python.exe scripts/generate_sample.py --source usdm
.\.venv\Scripts\python.exe scripts/send_file.py --file data/pending/sample_usdm.csv --source usdm --date 2021-06-22
```

Với USGS, tạo mẫu bằng `--source usgs`, rồi gửi `sample_usgs.csv` với `--source usgs`.
Hai CSV nhỏ hơn 1 MB là **mock data kiểm tra truyền file, không phải dữ liệu khoa học**.
Collector thật hiện là skeleton và báo `NotImplementedError`, không tự tải dữ liệu.

## Hợp đồng đã đọc từ source Server

| Endpoint | Hành vi |
|---|---|
| GET /health | Không cần key; status=ok, service=climate-data-server |
| POST /upload | Header X-API-Key; multipart file, source, source_date |
| GET /uploads | Cần key; trả list metadata; có filter source tùy chọn |

- File mới: HTTP 200, `status=VERIFIED`, metadata có `file_id`, `sha256`, `size_bytes`, `status=VERIFIED`.
- File trùng: **HTTP 200**, `status=DUPLICATE`, metadata của bản cũ. Không coi HTTP 409 là thành công.
- Server mặc định giới hạn 20 × 1024 × 1024 byte; file rỗng bị từ chối.
  Cấu hình Server có thể ghi đè giới hạn: cần xác minh khi triển khai; Client chỉ cho phép <=20 MiB.
- Thiếu/sai khóa: 401; source/ngày/file rỗng sai: 400; quá lớn: 413; thiếu field: 422.
- Không gửi checksum như field mới; Client so SHA-256 và kích thước với metadata trả về.
- Server chống trùng theo hash toàn cục, không theo source/ngày/client. Nếu source/ngày
  khác bản có sẵn, Client giữ pending để người vận hành kiểm tra. Không đổi giao thức
  hay tuyên bố Server đã lưu metadata quan sát mới.
- Không có khác biệt tên endpoint/field so với đề bài. Response và duplicate trước đó
  chưa được đề bài quy định cụ thể; triển khai dùng hành vi trong code Server.

## Lưu trữ, retry và xác nhận

Mỗi lần gửi tạo job UUID với snapshot `data/pending/<job>/payload` và `metadata.json`.
Đọc/copy/hash theo chunk; HTTPX stream file object; mỗi retry mở lại từ byte 0.
Không sửa hoặc xóa file người dùng đưa vào. Snapshot được chuyển sang
`data/uploaded/<job>/` khi response HTTP 200 xác nhận trạng thái và checksum/kích thước.
Nếu `/uploads` không truy cập được hoặc thiếu bản ghi, `listing_check` thể hiện rõ;
response upload đã xác nhận vẫn là bằng chứng tiếp nhận, không giả lập kết quả list.

Lỗi giữ payload trong pending; `data/failed/<job>.json` là báo cáo trỏ tới payload,
không di chuyển dữ liệu chưa được xác nhận ra khỏi pending. Gửi lại bằng cùng lệnh
với file nguồn hoặc đường dẫn payload trong báo cáo; một job mới được tạo và Server
chống trùng theo hash. Filename gửi lại từ payload sẽ là `payload`, nhưng bản trùng
giữ metadata tên gốc ở Server. Không tự quét/gửi lại mọi file trong pending.

`UPLOAD_RETRY_COUNT=3` nghĩa là tối đa 3 lần thử lại sau lần đầu (4 requests).
Backoff 1, 2, 4 giây, tối đa 30 giây; retry timeout, network/protocol error và
HTTP 408/429/500/502/503/504. Không retry 400/401/403/409/413/422.
Metadata lưu số lần retry, HTTP status, trạng thái thật từ Server, checksum, thời gian,
file ID và kết quả đối chiếu danh sách. Nhật ký thành công là JSON trong `logs/`.
Lỗi không ghi response body hoặc chuỗi exception mạng có thể chứa credentials.

## Windows Task Scheduler (sau khi kiểm thử Server thật thành công)

Chưa đăng ký scheduled task. Khi sẵn sàng, Create Task và cấu hình:

- Program: đường dẫn tuyệt đối tới `.venv\Scripts\python.exe` trong repo.
- Arguments: `scripts/send_file.py --file data/pending/sample_usdm.csv --source usdm --date 2021-06-22`.
- Start in: đường dẫn tuyệt đối thư mục gốc repo `IT5425_DataLakeHouse_Client`.
- Chọn tài khoản có quyền đọc `.env` và ghi data/logs; đặt trigger theo nhu cầu;
  chọn không khởi chạy instance mới nếu task đang chạy. Exit code 0 là thành công, 1 là lỗi.

Lệnh trên chỉ minh họa gửi một file cố định. Trước lịch thu thập thật cần triển khai
collector tạo file và ngày tương ứng, kiểm thử URL/key thật và kiểm tra giới hạn Server.
