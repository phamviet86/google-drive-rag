# Cách cài google-drive-rag trên VPS

google-drive-rag giúp Claude Code tìm và đọc tài liệu trong Google Drive của bạn. Sau khi cài, bạn chỉ cần hỏi Claude Code trên VPS, ví dụ "tìm hợp đồng thuê văn phòng năm 2025", và Claude trả lời kèm tên tệp, số trang và đường link Drive.

Dịch vụ chạy trong một Docker container trên chính VPS, chỉ nhận kết nối từ VPS đó (địa chỉ `127.0.0.1`), không mở ra Internet.

> **Lưu ý:** Với Docker cũ hơn bản 28.0, cổng mở trên `127.0.0.1` vẫn có thể bị máy khác cùng mạng nội bộ truy cập; hãy dùng Docker 28.0 trở lên.

## Mục lục

- Dịch vụ làm gì
- Chuẩn bị
- Tạo quyền đọc Google Drive
- Các bước cài đặt
- Kết nối với Claude Code
- Kiểm tra kết quả
- Tìm trong một thư mục hoặc một tệp
- Thêm hoặc bớt nguồn ngay trong Claude
- Nguồn là thư mục trên VPS
- Hạ thấp, ẩn hoặc loại tệp khỏi chỉ mục
- Việc thường ngày
- Quản trị từ Claude (tùy chọn)
- Xử lý sự cố

## Dịch vụ làm gì

- Đọc các thư mục, tệp hoặc bộ nhớ dùng chung (shared drive) mà bạn đưa link. Cũng đọc được thư mục nằm ngay trên VPS (phần "Nguồn là thư mục trên VPS").
- Hiểu Google Docs, Sheets, Slides, Word, Excel, PowerPoint (cả bản cũ `.doc`, `.xls`, `.ppt`), PDF (cả bản scan, nhờ nhận dạng chữ trong ảnh, gọi là OCR), ảnh chụp tài liệu (`.jpg`, `.png`, `.webp`), RTF, Markdown, văn bản, JSON, CSV và trang HTML.
- Đọc được chữ tiếng Việt có dấu và tiếng Anh trong bản scan và ảnh chụp. Cách thêm ngôn ngữ khác ở phần "Việc thường ngày".
- Cứ 15 phút kiểm tra Drive một lần: tệp mới được thêm, tệp sửa được đọc lại, tệp bị xóa hoặc thôi chia sẻ thì bị bỏ khỏi chỉ mục. Chỉ mục (index) là bản sao đã sắp xếp để tìm nhanh, nằm trên VPS.
- Không muốn chờ 15 phút? Nói với Claude, ví dụ "Tôi vừa thêm tệp vào Drive, đọc lại Drive giúp tôi". Claude sẽ bảo dịch vụ đọc Drive ngay, rồi xem tiến độ. Mỗi phút chỉ nhận một yêu cầu như vậy, và lịch 15 phút vẫn chạy như cũ.
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

   Trong lúc đang đọc, mục `sync` có thêm phần `progress` cho biết dịch vụ đang ở bước nào: `listing` (liệt kê tệp), `extracting` (đọc nội dung tệp) hay `embedding` (tính dữ liệu cho tìm theo ý nghĩa). `files_done` là số tệp đã xong trên tổng `files_total` của mỗi nguồn. Đọc xong thì `progress` trở về `null`.

   Mục `skipped` đếm các tệp bị bỏ qua vì dịch vụ chưa đọc được loại tệp đó, ví dụ tệp nén `.rar`, `.zip` hay lối tắt Drive. Các tệp này không lỗi, chỉ không được tìm. Xem danh sách (tối đa 100 tệp đầu):

   ```bash
   sudo docker compose exec gdrive-rag gdrive-rag status --skipped
   ```

2. Mở Claude Code và hỏi: "Dùng google-drive-rag tìm tài liệu về <chủ đề của bạn>".

   Kết quả: Claude trả lời kèm tên tệp, vị trí (trang, trang chiếu hoặc trang tính) và link Drive.

## Tìm trong một thư mục hoặc một tệp

Bạn có thể bảo Claude chỉ tìm trong một thư mục (gồm cả các thư mục con bên trong) hoặc chỉ trong vài tệp. Cách này giúp kết quả gọn và đúng chỗ hơn. Hãy dán link Drive của thư mục hoặc tệp vào câu hỏi, hoặc gọi đúng tên thư mục.

Ví dụ câu hỏi trong Claude Code:

- "Dùng google-drive-rag tìm 'hạn thanh toán' chỉ trong thư mục https://drive.google.com/drive/folders/…"
- "Trong thư mục Kế toán, tìm các báo cáo thuế năm 2025." Claude sẽ tự xem cây thư mục để biết thư mục Kế toán ở đâu.
- "Chỉ tìm trong tệp https://docs.google.com/document/d/…/edit: điều khoản phạt hợp đồng là gì?"
- "Thư mục Hợp đồng có những thư mục con nào?"

Kết quả: Claude chỉ trả lời bằng tài liệu nằm trong thư mục hoặc tệp đó. Nếu thư mục hoặc tệp chưa được đọc vào chỉ mục, Claude báo rõ là không tìm thấy nó, thay vì trả về kết quả rỗng.

Nguồn vừa thêm cần một lần đọc đầu tiên. Trong lúc đó bạn vẫn tìm được trong các tệp đã đọc xong, kèm một lời nhắc như `source 'Kế toán' is still on its first sync (120 of 433 files indexed)`, nghĩa là mới đọc 120 trên 433 tệp. Kết quả lúc này chưa đủ; hãy hỏi lại khi đọc xong.

> **Lưu ý:** Thư mục mới thêm hoặc mới đổi tên trên Drive được cập nhật ở lần đọc Drive kế tiếp (tối đa 15 phút, hoặc ngay khi bạn nhờ Claude đọc lại Drive). Đổi tên hay chuyển thư mục không làm dịch vụ đọc lại nội dung tệp.

Muốn tự thử trên VPS, chạy lệnh tìm kèm `--folder` (link thư mục) hoặc `--file` (link tệp, dùng nhiều lần được):

```bash
sudo docker compose exec gdrive-rag gdrive-rag search "báo cáo thuế" --folder "<link thư mục>"
```

## Thêm hoặc bớt nguồn ngay trong Claude

Không cần gõ lệnh và không cần bật `GDRAG_ADMIN_TOOLS`. Ba công cụ sau luôn có sẵn trong Claude Code:

- `add_source`: thêm một link thư mục hoặc tệp Drive và bắt đầu đọc ngay.
- `remove_source`: bỏ một nguồn. Claude báo trước sẽ bỏ bao nhiêu tài liệu khỏi chỉ mục, và chỉ làm khi bạn đồng ý.
- `set_path_rules`: đặt quy tắc hạ thấp, ẩn hoặc loại tệp (xem hai phần dưới đây). Cũng báo trước, và chỉ làm khi bạn đồng ý.

Ví dụ câu nói với Claude:

- "Thêm thư mục Drive này làm nguồn: https://drive.google.com/drive/folders/…"
- "Bỏ nguồn Kế toán đi."

Kết quả: Claude nói rõ đã thêm hay bỏ gì. Nguồn mới được đọc ngay, và lúc đầu chỉ tìm được trong các tệp đã đọc xong.

Chỉ nguồn Google xác nhận là bạn có quyền mở mới được lưu. Lệnh `source add` trên VPS cũng vậy: nếu Google chưa cho mở, nguồn không được lưu. Sau khi thêm bằng lệnh, dịch vụ đọc nguồn ở lần đọc Drive kế tiếp, hoặc bạn chạy `sudo docker compose exec gdrive-rag gdrive-rag sync`.

Các công cụ đổi quyền Google hay khóa tìm theo ý nghĩa thì vẫn tắt sẵn (xem phần "Quản trị từ Claude").

## Nguồn là thư mục trên VPS

Ngoài Drive, dịch vụ đọc được một thư mục nằm ngay trên VPS, ví dụ thư mục chứa hợp đồng bạn tải về. Dịch vụ chỉ đọc, không sửa hay xóa gì trong thư mục đó.

1. Chọn thư mục gốc. Mở tệp `.env` trong `~/google-drive-rag` và thêm dòng, thay bằng đường dẫn của bạn:

   ```text
   GDRAG_LOCAL_HOST_DIR=/home/ten-ban/tai-lieu
   ```

   Để trống thì dịch vụ dùng thư mục `./local` nằm cạnh `docker-compose.yml`. Trong container, thư mục này hiện ra là `/local`, chỉ đọc. Đừng tự đặt `GDRAG_LOCAL_ROOT` trong `.env`.

2. Cho dịch vụ quyền đọc tệp. Tệp cần quyền 644 và thư mục cần quyền 755, để người dùng của container (số 10001) đọc được:

   ```bash
   sudo chmod -R a+rX /home/ten-ban/tai-lieu
   ```

3. Chạy lại dịch vụ:

   ```bash
   sudo docker compose up -d
   ```

4. Thêm một thư mục con của nó làm nguồn. Cách 1, nói với Claude: "Thêm nguồn local:hop-dong" (`hop-dong` là thư mục con trong thư mục gốc; `local:.` là cả thư mục gốc). Cách 2, dùng lệnh:

   ```bash
   sudo docker compose exec gdrive-rag gdrive-rag source add local:hop-dong
   ```

   Kết quả: nguồn hiện trong `source list`, rồi được đọc ở lần đọc kế tiếp (tối đa 15 phút, hoặc chạy `gdrive-rag sync`).

Những điều nên biết:

- Tệp và thư mục có tên bắt đầu bằng dấu chấm (như `.git`) hoặc tệp khóa tạm của Word (`~$...`) bị bỏ qua.
- Đường dẫn ngoài thư mục gốc bị từ chối, giống như không có.
- Thư mục `secrets` và thư mục dữ liệu của dịch vụ không bao giờ bị đọc, kể cả khi chúng nằm trong thư mục gốc. Tệp trông giống khóa bí mật (khóa riêng, tệp khóa Google, mã đăng nhập) cũng bị từ chối và hiện là tệp lỗi.
- Tên tiếng Việt có dấu được nhận đúng, kể cả tên tệp chép từ máy Mac.
- Thư mục gốc trống, chưa gắn (ví dụ ổ đĩa chưa cắm) hoặc không đọc được: dịch vụ báo lỗi trong `sync_status` (mục `local_folder`) và không xóa gì khỏi chỉ mục. Cắm lại ổ đĩa là xong. Điều này cũng đúng với một thư mục nguồn bên trong, ví dụ ổ USB: nếu nó bỗng trống trong khi đã có tài liệu, dịch vụ không xóa gì.
- Nếu chính thư mục nguồn bị xóa trong khi thư mục gốc vẫn bình thường, nguồn đó được dọn khỏi chỉ mục.
- Drive hỏng hay thiếu quyền Google không ảnh hưởng nguồn trên VPS, và ngược lại.
- Đổi tên một tệp thì tệp được đọc lại, nhưng không phải tính lại dữ liệu ý nghĩa (bản scan thì nhận dạng chữ lại).
- Gỡ nguồn: nói "Bỏ nguồn local:hop-dong", Claude báo trước rồi hỏi bạn đồng ý (`remove_source`).

## Hạ thấp, ẩn hoặc loại tệp khỏi chỉ mục

Đôi khi một thư mục chứa bản cũ hay bản gốc đính kèm (ví dụ thư mục `attachments`) và các bản này cứ đứng trên bài viết chính trong kết quả tìm. Bạn có thể đặt quy tắc cho từng nguồn, bằng lệnh dưới đây hoặc bằng cách nhờ Claude (công cụ `set_path_rules`). Có ba loại:

- `demote` (hạ thấp): tệp khớp vẫn được tìm, nhưng mọi kết quả của tệp đó xếp sau 10 tệp khác đứng ngay dưới nó, nên bài viết chính lên trước.
- `exclude` (ẩn): tệp khớp không hiện trong kết quả tìm. Tệp vẫn nằm trong chỉ mục; Claude vẫn đọc được tệp đó khi bạn đưa link, hoặc khi bạn bảo chỉ tìm trong đúng tệp đó.
- `ignore` (loại khỏi chỉ mục): dịch vụ không đọc thư mục hoặc tệp đó nữa. Khác với `exclude`, tệp bị xóa khỏi chỉ mục, Claude không đọc được nữa, và dịch vụ không tải về hay xử lý nó. Dùng khi bạn không muốn dịch vụ chạm vào thư mục đó, ví dụ thư mục chứa ảnh nặng hoặc dữ liệu riêng tư.

Chọn thế nào: muốn tệp vẫn tìm được nhưng xuống thấp, dùng `demote`. Muốn tệp biến mất khỏi kết quả nhưng vẫn mở được khi có link, dùng `exclude`. Muốn dịch vụ bỏ hẳn, dùng `ignore`.

Quy tắc là mẫu đường dẫn, so với cột `path` trong kết quả tìm, không phân biệt chữ hoa và dấu tiếng Việt:

- `*` thay cho một phần tên, `**` thay cho nhiều tầng thư mục.
- `attachments/**` khớp mọi tệp trong mọi thư mục tên `attachments`, ở tầng nào cũng được.
- `*.bak` khớp mọi tệp có đuôi `.bak`.
- Mẫu bắt đầu bằng `/` chỉ khớp từ thư mục gốc của nguồn.
- Mỗi nguồn có tối đa 20 mẫu hạ thấp và 20 mẫu ẩn, mỗi mẫu tối đa 100 ký tự.
- Mẫu khớp mọi tệp (như `*` hay `**/*`) bị từ chối. Mẫu ẩn làm mất hết mọi tệp của nguồn cũng bị từ chối; khi đó hãy bỏ nguồn bằng `source remove`.
- Một tệp nằm trong hai nguồn lồng nhau (một thư mục và thư mục con của nó, cùng được thêm làm nguồn) chỉ có một đường dẫn, tính từ nguồn đã liệt kê nó trước. Vì vậy mẫu bắt đầu bằng `/` của nguồn kia có thể không khớp, và mẫu ẩn của một nguồn cũng ẩn tệp đó khi tìm trong nguồn kia.

Các bước:

1. Xem quy tắc hiện có và mẫu mới sẽ khớp bao nhiêu tệp (lệnh này chưa đổi gì):

   ```bash
   sudo docker compose exec gdrive-rag gdrive-rag source rules <số thứ tự hoặc tên nguồn>
   ```

2. Đặt quy tắc, ví dụ hạ thấp mọi tệp trong thư mục `attachments`:

   ```bash
   sudo docker compose exec gdrive-rag gdrive-rag source rules 1 --demote "attachments/**"
   ```

   Kết quả: câu trả lời có `"applied": true` và số tệp mỗi mẫu khớp. Lần tìm kế tiếp đã dùng quy tắc mới.

### Loại thư mục hoặc tệp khỏi chỉ mục (`ignore`)

Bạn nói với Claude, ví dụ "Đừng đọc thư mục https://drive.google.com/drive/folders/… nữa". Claude trước hết cho xem sẽ loại bao nhiêu tài liệu và thư mục, rồi chỉ làm khi bạn đồng ý. Hoặc dùng lệnh, đưa link thư mục (hoặc tệp), hay mẫu đường dẫn. Một mã Drive viết trơn chỉ được hiểu là mã khi dịch vụ đã biết nó; nếu chưa, hãy dán link hoặc viết `id:<mã>`. Mục nào chưa khớp gì, Claude sẽ cảnh báo để bạn kiểm tra lỗi gõ:

```bash
sudo docker compose exec gdrive-rag gdrive-rag source rules <nguồn> --ignore "<link thư mục>"
```

Với thư mục trên VPS, dùng `local:<đường dẫn>`, ví dụ `local:hop-dong/cu`. Kết quả:

- Thư mục biến mất khỏi kết quả tìm và khỏi danh sách thư mục ngay. Ở lần đọc kế tiếp, nó được xóa hẳn khỏi chỉ mục. Nếu nhờ Claude, lần đọc đó được xếp lịch ngay; nếu dùng lệnh, đó là lần đọc theo lịch 15 phút, hoặc bạn chạy `gdrive-rag sync`.
- Drive và thư mục trên VPS không bị đổi gì.
- Đổi tên hoặc chuyển thư mục đó trên Drive thì quy tắc vẫn đúng, vì nó nhớ thư mục theo mã chứ không theo tên.
- Các tệp bị loại hiện trong danh sách tệp bị bỏ qua (`status --skipped`) với lý do `ignored by rule`.
- Một tệp nằm trong hai nguồn mà một nguồn loại nó thì cả hai nguồn đều bỏ nó.
- Dịch vụ từ chối quy tắc làm trống cả nguồn; khi đó hãy bỏ nguồn.
- Muốn đọc lại: bỏ mục đó khỏi danh sách `--ignore`. Lần đọc kế tiếp đưa tệp trở lại.

Mỗi lần ghi `--demote`, `--exclude` hay `--ignore` là thay cả danh sách đó. Ghi `--demote` mà không kèm mẫu nào là xóa hết quy tắc hạ thấp. Đổi `demote` hay `exclude` không làm dịch vụ đọc lại tệp nào; đổi `ignore` thì có một lần đọc lại đầy đủ để áp dụng. `source list` cho thấy quy tắc của mỗi nguồn, và lệnh tìm kèm `--explain` cho thấy kết quả nào bị hạ thấp bởi mẫu nào.

## Việc thường ngày

| Việc | Lệnh (chạy trong `~/google-drive-rag`) |
| --- | --- |
| Thêm thư mục Drive | `sudo docker compose exec gdrive-rag gdrive-rag source add <link>` |
| Xem danh sách nguồn | `sudo docker compose exec gdrive-rag gdrive-rag source list` |
| Bỏ một nguồn | `sudo docker compose exec gdrive-rag gdrive-rag source remove <số thứ tự hoặc tên>` |
| Thêm thư mục trên VPS | `sudo docker compose exec gdrive-rag gdrive-rag source add local:<đường dẫn>` (xem phần trên) |
| Hạ thấp, ẩn hoặc loại tệp | `sudo docker compose exec gdrive-rag gdrive-rag source rules <nguồn> --demote "<mẫu>"` (hoặc `--exclude`, `--ignore`; xem phần trên) |
| Xem tệp bị bỏ qua | `sudo docker compose exec gdrive-rag gdrive-rag status --skipped` |
| Đọc lại Drive ngay | `sudo docker compose exec gdrive-rag gdrive-rag sync` |
| Thử tìm một câu | `sudo docker compose exec gdrive-rag gdrive-rag search "<câu tìm>"` |
| Xem vì sao một kết quả đứng đầu | Thêm `--explain` vào cuối lệnh tìm ở dòng trên |
| Đăng nhập Gmail lại | `sudo docker compose exec gdrive-rag gdrive-rag auth google` |
| Thêm hoặc đổi khóa OpenRouter | `sudo docker compose exec gdrive-rag gdrive-rag embedding set --api-key-file /secrets/openrouter.key` (xem trước), thêm `--yes` để làm |
| Tắt tìm theo ý nghĩa | `sudo docker compose exec gdrive-rag gdrive-rag embedding remove --yes` |
| Xóa quyền Google đã lưu | `sudo docker compose exec gdrive-rag gdrive-rag auth remove --yes` |
| Xem nhật ký | `sudo docker compose logs --tail 100 gdrive-rag` |
| Cập nhật mã mới | Chép mã mới lên VPS, rồi `sudo docker compose up -d --build` |

Dữ liệu chỉ mục nằm trong ổ Docker tên `gdrive-rag-data`, nên vẫn còn khi bạn dựng lại container. Cập nhật lên bản mới không làm dịch vụ đọc lại tệp hay tính lại dữ liệu ý nghĩa.

### Đọc bản scan bằng ngôn ngữ khác

Mặc định dịch vụ đọc chữ trong bản scan bằng tiếng Việt và tiếng Anh. Muốn thêm một ngôn ngữ, ví dụ tiếng Pháp (mã `fra`):

1. Mở tệp `Dockerfile`, tìm dòng `tesseract-ocr tesseract-ocr-vie tesseract-ocr-eng` và thêm `tesseract-ocr-fra` vào cuối dòng đó.
2. Thêm dòng sau vào tệp `.env` (tạo tệp từ `.env.example` nếu chưa có):

   ```text
   GDRAG_OCR_LANGS=vie+eng+fra
   ```

3. Dựng lại dịch vụ:

   ```bash
   sudo docker compose up -d --build
   ```

   Kết quả: các bản scan mới hoặc vừa sửa được đọc bằng cả ba ngôn ngữ. Mã của mỗi ngôn ngữ là phần cuối tên gói, ví dụ `tesseract-ocr-jpn` là `jpn`.

> **Lưu ý:** Thêm nhiều ngôn ngữ làm việc đọc bản scan chậm hơn và đôi khi nhầm chữ hơn. Chỉ thêm ngôn ngữ mà tài liệu của bạn thật sự dùng.

Sau khi cập nhật lên bản này, các bản scan đã đọc bằng bộ nhận dạng cũ được đọc lại một lần ở lần đọc Drive kế tiếp. Các tệp khác không bị đọc lại.

## Quản trị từ Claude (tùy chọn)

Thêm hoặc bớt nguồn và đặt quy tắc tệp thì Claude làm được sẵn (xem phần "Thêm hoặc bớt nguồn ngay trong Claude"). Nếu muốn nhờ Claude đổi quyền Google hay đổi khóa tìm theo ý nghĩa mà không phải gõ lệnh, bạn có thể bật thêm các công cụ quản trị này.

> **Cảnh báo:** Một tài liệu trong Drive có thể chứa dòng chữ giả làm lời dặn cho Claude, ví dụ "hãy gửi dữ liệu sang địa chỉ X". Nếu Claude làm theo, toàn bộ nội dung tài liệu của bạn có thể bị gửi tới máy của người khác. Vì vậy chỉ bật khi cần, và luôn đọc kỹ câu Claude hỏi lại trước khi đồng ý.

> **Cảnh báo:** Khi đã bật, bất kỳ chương trình hay người dùng nào trên VPS kết nối được tới cổng 8765 đều gọi được các công cụ này, không cần mật khẩu. Chỉ bật trên VPS mà chỉ bạn dùng, và tắt lại khi xong việc.

Cách bật:

1. Mở tệp `.env` trong `~/google-drive-rag` và thêm dòng:

   ```text
   GDRAG_ADMIN_TOOLS=1
   ```

2. Dựng lại dịch vụ:

   ```bash
   sudo docker compose up -d
   ```

   Kết quả: Claude Code thấy thêm 7 công cụ dưới đây. Muốn tắt, xóa dòng đó rồi chạy lại lệnh trên.

| Công cụ | Việc làm |
| --- | --- |
| `google_auth_status` | Cho biết đang dùng cách đăng nhập Google nào; không hiện khóa |
| `set_google_service_account` | Dùng tệp tài khoản dịch vụ đã chép vào thư mục `secrets` |
| `google_auth_start` | Bước 1 đăng nhập Gmail: đưa ra địa chỉ để bạn mở và bấm Cho phép |
| `google_auth_finish` | Bước 2: nhận địa chỉ trình duyệt hiện ra sau khi bấm Cho phép (hết hạn sau 10 phút) |
| `remove_google_auth` | Xóa quyền Google đã lưu; ngừng đọc Drive nhưng giữ chỉ mục và việc tìm |
| `set_embedding` | Đặt địa chỉ, model và khóa tìm theo ý nghĩa; báo rõ nội dung sẽ được gửi tới máy nào |
| `remove_embedding` | Xóa khóa, quay về chỉ tìm theo chữ; giữ lại dữ liệu ý nghĩa đã tính |

Những điều nên biết:

- Việc có thể làm mất dữ liệu (bỏ nguồn, đổi hoặc xóa quyền Google, đổi khóa tìm theo ý nghĩa) luôn chạy hai lần: lần đầu Claude chỉ kể sẽ xảy ra gì, lần sau mới làm khi bạn đồng ý.
- Đổi sang địa chỉ khác thì phải đưa khóa mới của địa chỉ đó; khóa cũ không bao giờ được gửi sang máy khác. Địa chỉ phải bắt đầu bằng `https://`. Máy trong mạng nội bộ (ghi bằng số, ví dụ `http://192.168.1.5:8080/v1`) chỉ dùng được khi đã thêm dòng `GDRAG_ALLOW_LOCAL_EMBEDDINGS=1` vào `.env`.
- Máy nối qua Tailscale (địa chỉ dạng `100.x.y.z`) hoặc mạng dùng chung kiểu CGNAT cũng được tính là mạng nội bộ, nên cũng cần dòng `GDRAG_ALLOW_LOCAL_EMBEDDINGS=1`.
- Khóa đã lưu gắn với địa chỉ nó được tạo cho. Nếu bạn xóa dòng `GDRAG_EMBEDDING_BASE_URL` khỏi `.env` hoặc đổi sang địa chỉ khác, khóa đã lưu không được dùng nữa: dịch vụ chỉ tìm theo chữ, và `sync_status` kèm cảnh báo. Hãy khôi phục dòng đó, hoặc nhờ Claude đặt khóa mới bằng `set_embedding`. Khóa ghi trong `.env` (`GDRAG_EMBEDDING_API_KEY`) không bị ràng buộc như vậy. Khóa lưu từ bản cũ được gắn với địa chỉ đang dùng ở lần khởi động đầu tiên sau khi cập nhật.
- Xóa quyền Google chỉ xóa bản lưu trên VPS, không thu hồi quyền ở phía Google. Muốn thu hồi hẳn: với đăng nhập Gmail, gỡ ứng dụng tại https://myaccount.google.com/permissions; với tài khoản dịch vụ, xóa khóa của nó trong Google Cloud hoặc thôi chia sẻ thư mục.
- Thư mục `secrets` chỉ nên chứa những tệp dịch vụ này cần (tệp quyền Google, tệp khóa tìm theo ý nghĩa). Đừng để ở đó khóa hay mật khẩu của việc khác.
- Tệp khóa phải nằm trong thư mục `secrets`. Claude chỉ cần tên tệp, ví dụ `openrouter.key`, nên khóa không phải dán vào cuộc trò chuyện. Công cụ không đọc được tệp nằm ngoài thư mục này.
- Khóa và mã đăng nhập không bao giờ hiện trong câu trả lời, trong `sync_status` hay trong nhật ký.
- Nếu `.env` đang ghi sẵn khóa, địa chỉ hay tệp Google, hãy chạy một lần `sudo docker compose exec gdrive-rag gdrive-rag config import-env`. Lệnh này lưu các giá trị đó vào dịch vụ (không in khóa ra) và liệt kê những dòng trong `.env` có thể để trống. Để trống các dòng đó rồi chạy `sudo docker compose up -d`.
- Giá trị ghi trong `.env` luôn thắng. Nếu `.env` đã ghi một giá trị khác, công cụ từ chối và nói tên dòng cần xóa trong `.env`. Muốn quản lý một mục bằng Claude, hãy để trống dòng đó trong `.env` rồi dựng lại dịch vụ. `sync_status` cho biết mỗi mục lấy từ `.env` ("environment"), từ công cụ hoặc lệnh ("stored") hay mặc định ("default").
- Sau khi đổi quyền Google, Drive chưa được đọc lại ngay. Hãy chia sẻ các thư mục cho tài khoản mới trước, rồi nhờ Claude gọi `sync_now`. Một thư mục mà tài khoản mới không mở được sẽ bị bỏ khỏi chỉ mục ở lần đọc kế tiếp.
- Nguồn ghi trong `GDRAG_DRIVE_LINKS` không bỏ được bằng công cụ, vì lần khởi động sau nó sẽ được thêm lại.
- Khi Drive đang được đọc, việc bỏ nguồn bị từ chối; hãy thử lại khi `sync_status` báo đã xong. Các thay đổi khác được lưu ngay và dùng từ bước đọc kế tiếp.

## Xử lý sự cố

| Hiện tượng | Nguyên nhân | Cách xử lý |
| --- | --- | --- |
| Nguồn báo `not found or not shared` | Thư mục chưa chia sẻ cho email tài khoản dịch vụ, hoặc link sai | Chia sẻ thư mục với quyền Người xem, rồi chạy `sync` |
| Báo `Google sign-in expired` | Đăng nhập Gmail hết hạn sau 7 ngày (ứng dụng ở chế độ Testing) | Chạy `auth google`, hoặc chuyển sang tài khoản dịch vụ |
| `Permission denied` khi đọc `/secrets/google.json` | Tệp chưa giao cho dịch vụ | Chạy lại bước 4 |
| `status` báo tệp lỗi | Tệp hỏng, có mật khẩu hoặc quá 200 MB | Mở tệp trên Drive để kiểm tra; các tệp khác không bị ảnh hưởng |
| `ocr_unavailable` lớn hơn 0 | Thiếu chương trình Tesseract hoặc dữ liệu của một ngôn ngữ trong `GDRAG_OCR_LANGS`, nên trang scan chưa có chữ | Kiểm tra mục "Đọc bản scan bằng ngôn ngữ khác", rồi dựng lại container bằng bước 5. Tệp sẽ được đọc lại khi OCR chạy được |
| Claude Code không thấy công cụ | Dịch vụ chưa chạy hoặc chưa đăng ký | Chạy `sudo docker compose ps`, rồi lặp lại phần "Kết nối với Claude Code" |
