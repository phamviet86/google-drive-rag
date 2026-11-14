# Cách cài google-drive-rag trên VPS

google-drive-rag giúp Claude Code tìm và đọc tài liệu trong Google Drive của bạn. Sau khi cài, bạn chỉ cần hỏi Claude Code trên VPS, ví dụ "tìm hợp đồng thuê văn phòng năm 2025", và Claude trả lời kèm tên tệp, số trang và đường link Drive.

Dịch vụ chạy trong một Docker container trên chính VPS, chỉ nhận kết nối từ VPS đó (địa chỉ `127.0.0.1`), không mở ra Internet.

## Mục lục

- Dịch vụ làm gì
- Chuẩn bị
- Tạo quyền đọc Google Drive
- Các bước cài đặt
- Kết nối với Claude Code
- Kiểm tra kết quả
- Việc thường ngày
- Xử lý sự cố

## Dịch vụ làm gì

- Đọc các thư mục, tệp hoặc bộ nhớ dùng chung (shared drive) mà bạn đưa link.
- Hiểu Google Docs, Sheets, Slides, Word, Excel, PowerPoint (cả bản cũ `.doc`, `.xls`, `.ppt`), PDF (cả bản scan, nhờ nhận dạng chữ trong ảnh, gọi là OCR), RTF, Markdown, văn bản, JSON, CSV và trang HTML.
- Cứ 15 phút kiểm tra Drive một lần: tệp mới được thêm, tệp sửa được đọc lại, tệp bị xóa hoặc thôi chia sẻ thì bị bỏ khỏi chỉ mục. Chỉ mục (index) là bản sao đã sắp xếp để tìm nhanh, nằm trên VPS.
- Chỉ đọc. Dịch vụ không bao giờ sửa hay xóa gì trên Drive.

## Chuẩn bị

Khi cài, dịch vụ chỉ hỏi bạn ba thứ:

1. **Khóa API cho tìm kiếm theo ý nghĩa** (không bắt buộc). Khóa API là một chuỗi bí mật để dịch vụ dùng tài khoản OpenRouter của bạn. Có khóa thì Claude tìm được cả câu hỏi diễn đạt khác chữ trong tài liệu. Không có khóa thì vẫn tìm theo từ, và bạn có thể thêm khóa sau. Tạo khóa tại trang **Keys** của OpenRouter (`openrouter.ai/settings/keys`).
2. **Tệp quyền đọc Google Drive** (tệp JSON). Cách tạo ở phần sau.
3. **Link Google Drive** của các thư mục hoặc tệp cần tìm. Mở thư mục trên Drive và chép địa chỉ trên thanh trình duyệt.

Bạn cũng cần:

- Một VPS Linux, đăng nhập được bằng SSH (ví dụ `ssh ten-ban@dia-chi-vps`).
- Claude Code đã cài trên VPS đó.
- Thư mục mã nguồn `google-drive-rag` trên máy của bạn.

## Tạo quyền đọc Google Drive

Có hai cách. Trên VPS nên dùng cách 1, vì nó không hết hạn và không cần đăng nhập lại.

### Cách 1: tài khoản dịch vụ (khuyên dùng)

Tài khoản dịch vụ (service account) là một "người dùng máy" có địa chỉ email riêng. Bạn chia sẻ thư mục cho email đó giống như chia sẻ cho đồng nghiệp.

1. Mở `console.cloud.google.com`, đăng nhập bằng Gmail của bạn và tạo một dự án mới, ví dụ tên `drive-rag`.
2. Vào **APIs & Services** > **Library**, tìm **Google Drive API** và nhấn **Enable**.
3. Vào **IAM & Admin** > **Service Accounts**, nhấn **Create service account**, đặt tên `drive-reader`, rồi nhấn **Done**. Không cần cấp vai trò nào.
4. Mở tài khoản vừa tạo, chọn thẻ **Keys** > **Add key** > **Create new key** > **JSON**.

   Kết quả: trình duyệt tải về một tệp `.json`. Đây là chìa khóa bí mật, đừng gửi cho ai.

5. Chép địa chỉ email của tài khoản dịch vụ (dạng `drive-reader@drive-rag-123.iam.gserviceaccount.com`).
6. Trên Google Drive, chia sẻ từng thư mục cần tìm cho email đó với quyền **Người xem** (Viewer).

### Cách 2: đăng nhập Gmail (OAuth)

Dùng khi bạn không muốn chia sẻ thư mục cho tài khoản dịch vụ.

1. Trong cùng dự án Google Cloud, bật **Google Drive API** như bước 2 ở trên.
2. Vào **Google Auth Platform**, điền tên ứng dụng và email của bạn, chọn đối tượng **External**, rồi thêm Gmail của bạn vào danh sách **Test users**.
3. Vào **Clients** > **Create client**, chọn loại **Desktop app**, rồi tải tệp JSON về.

> **Quan trọng:** Khi ứng dụng còn ở trạng thái **Testing**, Google cho quyền đăng nhập hết hạn sau 7 ngày. Khi đó dịch vụ báo lỗi đăng nhập và bạn phải chạy lại lệnh `auth google` (xem phần "Việc thường ngày"). Muốn tránh, hãy dùng cách 1.

## Các bước cài đặt

1. Chép thư mục mã nguồn lên VPS. Trên máy của bạn, chạy:

   ```bash
   scp -r google-drive-rag ten-ban@dia-chi-vps:~/
   ```

   Kết quả: trên VPS có thư mục `~/google-drive-rag`.

2. Đăng nhập VPS và cài Docker (bỏ qua nếu đã có):

   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   ```

   Kết quả: lệnh `docker --version` in ra số phiên bản.

3. Chép tệp JSON quyền Google lên VPS vào thư mục `secrets`. Trên máy của bạn, chạy (thay tên tệp cho đúng):

   ```bash
   ssh ten-ban@dia-chi-vps 'mkdir -p ~/google-drive-rag/secrets'
   scp ten-tep-da-tai.json ten-ban@dia-chi-vps:~/google-drive-rag/secrets/google.json
   ```

4. Trên VPS, giao tệp đó cho dịch vụ và khóa lại để người khác không đọc được:

   ```bash
   cd ~/google-drive-rag
   sudo chown -R 10001:10001 secrets && sudo chmod 600 secrets/google.json
   ```

5. Dựng và chạy dịch vụ. Lần đầu mất khoảng 5 đến 15 phút vì phải tải LibreOffice và bộ nhận dạng chữ:

   ```bash
   sudo docker compose up -d --build
   ```

   Kết quả: dòng cuối báo container `google-drive-rag-gdrive-rag-1` đã **Started**.

6. Trả lời ba câu hỏi cài đặt:

   ```bash
   sudo docker compose exec gdrive-rag gdrive-rag setup
   ```

   - Câu 1: dán khóa OpenRouter, hoặc nhấn Enter để bỏ qua. Ký tự gõ vào không hiện ra, đó là bình thường.
   - Câu 2: gõ `1` nếu dùng tài khoản dịch vụ, `2` nếu dùng đăng nhập Gmail; đường dẫn tệp là `/secrets/google.json`.
   - Câu 3: dán từng link Drive, mỗi link một dòng, rồi nhấn Enter ở dòng trống để kết thúc.

   Nếu chọn `2`, màn hình hiện một địa chỉ dài. Mở địa chỉ đó trên máy tính của bạn, đăng nhập và nhấn **Allow**. Trình duyệt sẽ báo không mở được trang `localhost`, đó là bình thường. Chép toàn bộ địa chỉ trên thanh trình duyệt và dán vào VPS.

   Kết quả: màn hình liệt kê các nguồn đã thêm với tên thư mục Drive.

7. Bắt đầu đọc Drive ngay, không chờ 15 phút:

   ```bash
   sudo docker compose restart gdrive-rag
   ```

> **Lưu ý:** Không muốn trả lời câu hỏi? Chép `.env.example` thành `.env`, điền ba giá trị vào đó (đường dẫn tệp là `/secrets/google.json`), rồi chạy lại bước 5. Cách này chỉ đủ với tài khoản dịch vụ; đăng nhập Gmail vẫn cần bước 6 một lần.

## Kết nối với Claude Code

Chạy lệnh này trên VPS, bằng tài khoản bạn dùng Claude Code:

```bash
claude mcp add --transport http --scope user google-drive-rag http://127.0.0.1:8765/mcp
```

MCP là cách Claude Code nói chuyện với công cụ bên ngoài. `--scope user` cho Claude dùng dịch vụ ở mọi thư mục dự án; bỏ phần này nếu chỉ muốn dùng trong thư mục hiện tại.

Kết quả: lệnh `claude mcp list` hiện dòng `google-drive-rag` với trạng thái đã kết nối.

## Kiểm tra kết quả

1. Xem tiến độ đọc tài liệu:

   ```bash
   sudo docker compose exec gdrive-rag gdrive-rag status
   ```

   Kết quả: mục `documents` có `indexed` lớn hơn 0. Thư mục lớn cần vài phút đến vài giờ cho lần đầu.

2. Mở Claude Code và hỏi: "Dùng google-drive-rag tìm tài liệu về <chủ đề của bạn>".

   Kết quả: Claude trả lời kèm tên tệp, vị trí (trang, trang chiếu hoặc trang tính) và link Drive.

## Việc thường ngày

| Việc | Lệnh (chạy trong `~/google-drive-rag`) |
| --- | --- |
| Thêm thư mục Drive | `sudo docker compose exec gdrive-rag gdrive-rag source add <link>` |
| Xem danh sách nguồn | `sudo docker compose exec gdrive-rag gdrive-rag source list` |
| Bỏ một nguồn | `sudo docker compose exec gdrive-rag gdrive-rag source remove <số thứ tự hoặc tên>` |
| Đọc lại Drive ngay | `sudo docker compose exec gdrive-rag gdrive-rag sync` |
| Đăng nhập Gmail lại | `sudo docker compose exec gdrive-rag gdrive-rag auth google` |
| Thêm hoặc đổi khóa OpenRouter | Chạy lại `setup`; tài liệu cũ được tính lại ý nghĩa ở lần đọc kế tiếp |
| Xem nhật ký | `sudo docker compose logs --tail 100 gdrive-rag` |
| Cập nhật mã mới | Chép mã mới lên VPS, rồi `sudo docker compose up -d --build` |

Dữ liệu chỉ mục nằm trong ổ Docker tên `gdrive-rag-data`, nên vẫn còn khi bạn dựng lại container.

## Xử lý sự cố

| Hiện tượng | Nguyên nhân | Cách xử lý |
| --- | --- | --- |
| Nguồn báo `not found or not shared` | Thư mục chưa chia sẻ cho email tài khoản dịch vụ, hoặc link sai | Chia sẻ thư mục với quyền Người xem, rồi chạy `sync` |
| Báo `Google sign-in expired` | Đăng nhập Gmail hết hạn sau 7 ngày (ứng dụng ở chế độ Testing) | Chạy `auth google`, hoặc chuyển sang tài khoản dịch vụ |
| `Permission denied` khi đọc `/secrets/google.json` | Tệp chưa giao cho dịch vụ | Chạy lại bước 4 |
| `status` báo tệp lỗi | Tệp hỏng, có mật khẩu hoặc quá 200 MB | Mở tệp trên Drive để kiểm tra; các tệp khác không bị ảnh hưởng |
| `ocr_unavailable` lớn hơn 0 | Bộ nhận dạng chữ không chạy được, trang scan chưa có chữ | Dựng lại container bằng bước 5 và xem nhật ký |
| Claude Code không thấy công cụ | Dịch vụ chưa chạy hoặc chưa đăng ký | Chạy `sudo docker compose ps`, rồi lặp lại phần "Kết nối với Claude Code" |
