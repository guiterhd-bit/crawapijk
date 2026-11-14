# crawapijk

Crawler tổng hợp các luồng HLS, FLV và MPEG-DASH từ danh sách website trong `sources.txt`, xuất thành `playlist.m3u`.

## Thêm nguồn mới

Chỉ cần thêm một dòng vào `sources.txt`, không cần sửa `crawler.py`:

```text
Tên hiển thị | https://ten-mien.example/ >> /duong-dan-tran/ ;; id=tennguon
```

- `Tên hiển thị`: tên sẽ được dùng làm `group-title` trong M3U.
- URL đầu tiên: trang chủ cần quét.
- Phần sau `>>`: đoạn đường dẫn để nhận diện trang trận. Crawler chỉ nhận link cùng tên miền với trang chủ.
- `id=`: mã nguồn ổn định, tùy chọn; nếu bỏ trống sẽ tự tạo từ tên miền.
- `logo=`: URL logo nguồn thực tế, tùy chọn.

Nếu website phát luồng trực tiếp ngay tại trang chủ, có thể chỉ khai báo URL:

```text
Tên hiển thị | https://ten-mien.example/ ;; id=tennguon
```

Dòng bắt đầu bằng `#` được xem là chú thích. Các trận ở trạng thái sắp diễn ra, đang diễn ra và đã kết thúc đều được quét; crawler không lọc theo trạng thái trận.

## Chạy cục bộ

```bash
pip install requests playwright
playwright install chromium
python -m unittest discover -s tests -v
python crawler.py --max-pages 0 --workers 12
```

Mặc định crawler kiểm tra khả năng truy cập của từng luồng trước khi ghi playlist. Nếu không tìm được luồng hợp lệ, playlist hiện tại được giữ nguyên.
