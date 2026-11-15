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

### Logo đội bóng trong TVG

`tvg-logo` được lấy từ logo đội chủ nhà tìm thấy trên trang trận, không dùng logo nhà đài thay thế. Crawler đọc ảnh trong DOM và đối chiếu `alt`, `title`, class, tên file ảnh và nội dung vùng chứa với tên hai đội.

- `tvg-logo` và `x-home-logo`: logo đội chủ nhà.
- `x-away-logo`: logo đội khách (thuộc tính mở rộng, giữ riêng vì chuẩn M3U chỉ có một trường `tvg-logo`).
- `x-source-logo`: logo nhà đài, chỉ lưu làm metadata nhận diện nguồn, không đưa vào `tvg-logo`.
- Nếu trang trận không cung cấp ảnh logo đội bóng phù hợp, `tvg-logo` để trống; crawler không tự gán logo nhà đài hoặc logo giả.

Logo đội bóng được lấy từ ảnh thực tế trên trang trận; không phải mọi website đều công khai logo đội bóng trong HTML.


## Cấu hình nguồn API và website

- `sources.txt`: danh sách website cần crawl. Có thể thêm nguồn mới theo cú pháp hiện tại mà không sửa Python.
- `config.json`: cấu hình API JSON độc lập với danh sách website. Các API được crawl bổ sung và hợp nhất với kết quả website; một API lỗi không làm dừng các nguồn còn lại.
- Hỗ trợ API `GET` với `params`/`params_variants`, API `POST` JSON và phân trang; có thể khai báo endpoint chi tiết/trực tiếp theo `{id}`.
- Crawler tìm URL HLS (`.m3u8`), FLV (`.flv`) và DASH (`.mpd`) trong JSON lồng nhau; nếu API chỉ trả URL player/iframe, crawler thử phân tích trang player bằng cơ chế trình duyệt hiện có.
- `headers`, `default_stream_referer`/`stream_referer` và `stream_header_rules` được dùng khi truy vấn API và kiểm tra luồng.
- `output_file` trong `config.json` là playlist chính (`thethao.m3u`); crawler vẫn đồng bộ thêm `playlist.m3u` để tương thích với luồng sử dụng cũ.
- Trường API không nhận diện được sẽ để trống, không tự tạo tên đội, tỉ số, BLV hoặc logo.

API được cấu hình hiện tại: Chuối Chiên TV, Gà Vàng TV, Cola TV và Phá Làng TV.


### Cấu trúc EXTINF theo chuẩn MHD Sports

Cả `thethao.m3u` và `playlist.m3u` được ghi cùng một cấu trúc metadata cho mỗi luồng:

- `tvg-id`: ID ổn định dạng `mhd-match-<hash>`, dùng chung cho các luồng thuộc cùng trận đấu.
- `tvg-name`: tên đội bóng kèm BLV và nhãn luồng nếu website/API cung cấp, ví dụ `Guadalajara W vs Juárez W - Chuối Tây - HD1`.
- `tvg-logo`: logo đội chủ nhà; để trống nếu không tìm thấy logo đội thật.
- `group-title`: tên nhà đài; `group-logo`: logo nhà đài.
- `x-source`: mã nguồn ổn định; `x-sport`: mặc định `FOOTBALL`.
- Nội dung sau dấu phẩy có dạng `Tên đội 1 vs Tên đội 2 | dd/MM HH:mm | BLV | HD1`. Những trường không có dữ liệu sẽ được bỏ qua.
- `x-away-logo` tiếp tục lưu logo đội khách riêng; Referer của luồng vẫn được ghi bằng `#EXTVLCOPT:http-referrer`.

Nếu API trả nhãn stream theo key như `hd_1` hoặc `sd_2`, crawler chuẩn hóa thành `HD1` hoặc `SD2`. Nếu không có nhãn, crawler dùng định dạng luồng (HLS/FLV/DASH) làm nhãn dự phòng.
