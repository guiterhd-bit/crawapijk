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

Dòng bắt đầu bằng `#` được xem là chú thích. Crawler quét trang trận được tìm thấy, sau đó chỉ đưa vào playlist các trận đang trực tiếp và các trận sắp diễn ra có giờ khai cuộc từ hiện tại đến hết ngày mai (theo giờ Việt Nam). Trận đã kết thúc và trận ở các ngày xa hơn không được đưa vào playlist. Các trận sắp diễn ra chỉ được thêm khi trang nguồn cung cấp được giờ thi đấu có thể phân tích và có luồng phát hợp lệ.

Bộ phân tích ngày giờ hỗ trợ cả định dạng `HH:MM-DD/MM/YYYY` (ví dụ `11:05-14/11/2026`) thường gặp trên thẻ/trang trận Ném Biên TV. Khi lấy thông tin thẻ trận từ trang chủ, crawler so sánh các phần tử cha để ưu tiên khối có tên hai đội và ngày giờ thay vì chỉ lấy nhãn nút xem trận.

## Chạy cục bộ

```bash
pip install requests playwright
playwright install chromium
python -m unittest discover -s tests -v
python crawler.py --max-pages 0 --workers 12
```

Mặc định crawler kiểm tra khả năng truy cập của từng luồng trước khi ghi playlist. Nếu không tìm được luồng hợp lệ, playlist hiện tại được giữ nguyên.


### Bổ sung trích xuất metadata từ thẻ trận

Crawler giữ nguyên bộ trích xuất metadata hiện có (JSON, tiêu đề trang, nội dung trang và slug URL).
Nếu các trường đội bóng hoặc tỉ số còn thiếu/tiêu đề trang không phải tên trận, crawler mới dùng bộ phân tích bổ sung từ nội dung thẻ trận. Bộ phân tích hỗ trợ dạng có tên giải đứng trước, ví dụ `SV.League Women - Hisamitsu Springs W VS PFU Blue Cats W -11:05-14/11/2026`.
Các giá trị đã trích xuất hợp lệ không bị ghi đè; parser bổ sung chỉ điền trường còn thiếu hoặc thay tiêu đề trang yếu không có cặp đội bóng.


### Tối ưu thời gian quét trang trận

Workflow dành tối đa 90 giây cho mỗi nguồn để quét trang trận (trước đây là 45 giây), với giới hạn job 30 phút. Trước khi mở từng trang trận, crawler ưu tiên trận đang trực tiếp, sau đó đến trận sắp diễn ra; các thẻ có giờ bắt đầu rõ ràng cũ hơn 3 giờ sẽ được bỏ qua. Trận đang trực tiếp và thẻ không có giờ hoặc không đọc được giờ vẫn được giữ lại để hạn chế bỏ sót nguồn. Có thể điều chỉnh bằng `--scan-budget` và `--scan-lookback-hours`.

### Bổ sung nhận diện luồng player cho Chuối Chiên TV và các nguồn tương tự

Khi quét trang trận, crawler không chỉ bắt request mạng `.m3u8`/`.flv`/`.mpd` và response có Content-Type tương ứng. Crawler còn đọc cấu hình player trong HTML/script nội tuyến, các thẻ `video`/`source` và URL iframe để lấy manifest được nhúng sẵn nhưng chưa phát request do người dùng chưa nhấn Play. Với request phát sinh trong iframe, crawler lưu URL frame làm `Referer` ứng viên để kiểm tra luồng. Đây là cơ chế bổ sung, không thay thế cách bắt request/response hiện có.


### Cửa sổ ưu tiên quét trận

Workflow dùng `--scan-lookback-hours 3`: các thẻ trận có giờ bắt đầu cũ hơn 3 giờ so với thời điểm chạy (múi giờ Việt Nam) sẽ bị bỏ qua khi mở trang trận. Trận được nhận diện đang trực tiếp và thẻ không có thời gian phân tích được vẫn được giữ lại. Có thể điều chỉnh bằng tham số `--scan-lookback-hours`, ví dụ `6` để giữ lại các trận trong 6 giờ gần nhất.
