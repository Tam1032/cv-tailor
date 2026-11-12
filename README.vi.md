# CV Tailor

Web app chạy trên máy của bạn, dùng Claude để chỉnh CV LaTeX cho khớp với một JD cụ thể.

- **JD**: dán text, dán link public, tải file PDF, hoặc ảnh chụp màn hình (có thể Ctrl+V dán thẳng ảnh vào trang).
- **CV**: file `.tex` mẫu của bạn (app nhớ CV gần nhất trong trình duyệt).
- **Kết quả**: file `.tex` đã sửa và `.pdf` đã biên dịch, xem trước ngay trên trang và tải về được. Bạn tự chọn **thư mục lưu** (nút "Chọn…" mở hộp thoại chọn thư mục) và **tên file**. File trùng tên sẽ không bị ghi đè mà được thêm `_2`, `_3`… Mỗi lần chạy cũng được lưu một bản lịch sử trong `output/<thời-gian>_<công-ty>/`, kèm `report.json` ghi lại mọi thay đổi.

Claude chỉ được viết lại câu chữ theo thuật ngữ của JD, sắp xếp lại, nhấn mạnh hoặc lược bớt. App **không bịa thêm** kinh nghiệm, số liệu hay kỹ năng. Những yêu cầu của JD mà CV chưa có sẽ được liệt kê riêng để bạn tự quyết định.

## Cài đặt

Cần có:

1. **Python 3.10+**
2. **Một trình biên dịch LaTeX**, chọn một trong các cách sau:
   - [Tectonic](https://tectonic-typesetting.github.io/) (gọn nhất, tự tải package khi cần): `brew install tectonic`, hoặc `cargo install tectonic`, hoặc trên Windows tải file `tectonic-…-x86_64-pc-windows-msvc.zip` ở trang Releases trên GitHub, giải nén `tectonic.exe` vào một thư mục có trong PATH.
   - TeX Live / MacTeX / MiKTeX (Windows: https://miktex.org) có `pdflatex` (hoặc `latexmk`). Trên Ubuntu: `sudo apt install texlive-latex-extra texlive-fonts-recommended lmodern`.
   App tự dò theo thứ tự tectonic → latexmk → pdflatex, và dùng xelatex nếu CV có `fontspec`.
3. **Một trong hai cách kết nối Claude** (chọn trong ô "Kết nối Claude" trên trang):
   - **Claude Code, dùng gói claude.ai Pro/Max, không tốn thêm tiền.** Cài Node.js 18+, chạy `npm install -g @anthropic-ai/claude-code`, rồi chạy `claude` một lần để đăng nhập bằng tài khoản claude.ai (gõ `/exit` để thoát). App sẽ gọi `claude -p` và tính vào hạn mức sử dụng của gói. Dùng nhiều trong thời gian ngắn có thể bị tạm giới hạn vài giờ.
   - **API key, trả tiền theo token.** Tạo key tại https://console.anthropic.com → API Keys (phải nạp credit riêng), rồi điền vào `.env`.

## Chạy

macOS / Linux:

```bash
cd cv-tailor
cp .env.example .env      # chỉ cần nếu dùng API key; dùng Claude Code thì bỏ qua
./run.sh
```

Windows (Command Prompt):

```bat
cd cv-tailor
run.bat
```

`run.bat` tự tạo môi trường Python, cài thư viện, đọc `.env` (nếu có) và mở trình duyệt. Nếu dùng API key, sao chép `.env.example` thành `.env` rồi bỏ dấu `#` ở dòng `ANTHROPIC_API_KEY`.

Lưu ý riêng cho Windows:
- Khi cài Python, tick ô **"Add python.exe to PATH"**.
- Claude Code trên Windows cần **Git for Windows** (https://git-scm.com/download/win). Cài Git trước, sau đó cài Claude Code.
- Nếu app báo **"Không tìm thấy trình biên dịch LaTeX"**: cài MiKTeX (https://miktex.org/download, hoặc chạy `winget install ChristianSchenk.MiKTeX` trong CMD), rồi tắt app (Ctrl+C) và chạy lại `run.bat`. App tự tìm MiKTeX/TeX Live ở các thư mục cài mặc định kể cả khi chưa có trong PATH. Nếu cài ở chỗ khác, đặt biến `CV_TAILOR_LATEX_DIR` trỏ tới thư mục chứa `pdflatex.exe`. Sau khi cài xong, bấm **"Biên dịch lại PDF"** ở kết quả cũ để có PDF mà không tốn thêm token.
- Nếu dùng MiKTeX: mở **MiKTeX Console → Settings**, chọn "Install missing packages: **Always**", để lần biên dịch đầu không bị dừng vì hỏi cài package.

Mở http://127.0.0.1:8000, chọn JD và CV, bấm **Phân tích JD và sửa CV**.

Sau khi đã phân tích một JD, nút **Sửa lại với JD đã phân tích** cho phép chạy lại (đổi model, effort, ghi chú) mà không tốn token phân tích JD lần nữa.

## Danh sách môn học (Related Coursework)

File `courses.md` chứa tất cả môn đại học của bạn, chia theo trường. Lần đầu, app dùng bản mẫu `courses.example.md`. Bạn có thể sửa ngay trên trang, trong mục **"Danh sách môn học đại học"**, rồi bấm **Lưu danh sách** (app sẽ ghi ra `courses.md`, file này không bị ghi đè khi bạn cập nhật app).

Khi ô **"Chọn Related Coursework hợp với JD"** được bật, Claude viết lại dòng "Related courses" của từng trường với 3–6 môn hợp với JD nhất. Claude chỉ chọn môn có trong danh sách và giữ đúng trường của môn đó.

## Truy cập từ máy khác (điện thoại, laptop khác)

App vẫn chạy trên máy chính (máy có Claude Code và MiKTeX). Các máy khác chỉ cần trình duyệt.

1. Trên máy chính, tạo file `.env` (sao chép từ `.env.example`) và thêm:
   ```
   CV_TAILOR_HOST=0.0.0.0
   CV_TAILOR_PASSWORD=mat-khau-cua-ban
   ```
   Bắt buộc có mật khẩu, nếu thiếu app sẽ không chạy.
2. Chạy `run.bat`. Màn hình in ra địa chỉ IPv4 của máy, ví dụ `192.168.1.25`. Nếu Windows Firewall hỏi, chọn **Allow** cho mạng **Private**.
3. Trên máy khác **cùng mạng Wi-Fi/LAN**, mở `http://192.168.1.25:8000`. Trình duyệt sẽ hỏi đăng nhập: tên gõ gì cũng được, mật khẩu là `CV_TAILOR_PASSWORD`.

Lưu ý:
- Mọi lượt sửa CV đều chạy bằng Claude Code và hạn mức gói của máy chính.
- Thư mục lưu là thư mục trên máy chính. Muốn lấy file về máy đang dùng thì bấm **Tải .tex** / **Tải PDF**. Nút "Chọn…" chỉ hiện khi mở app ngay trên máy chính.
- Máy chính phải đang bật và đang chạy `run.bat`.
- Truy cập từ ngoài mạng nhà (4G, quán cà phê…): cài [Tailscale](https://tailscale.com) miễn phí trên cả hai máy, rồi dùng địa chỉ Tailscale của máy chính (dạng `100.x.y.z:8000`). Không nên mở port ra Internet.

## Cách app tiết kiệm token

| Bước | Làm gì | Vì sao rẻ |
|---|---|---|
| 1. Đọc JD | Link và PDF được trích text **ngay trên máy** (BeautifulSoup, pypdf). Chỉ ảnh hoặc PDF scan mới gửi nguyên file. | Không trả tiền cho HTML, menu, footer. |
| 2. Tóm tắt JD | `claude-haiku-5-5` (rẻ nhất, ~$0.10/1M token input) rút JD thành danh sách yêu cầu ngắn gọn (JSON). Kết quả được cache trên đĩa theo hash, cùng JD chạy lại thì tốn 0 token. | Model đắt chỉ đọc vài trăm token tóm tắt thay vì cả JD dài. |
| 3. Gửi CV | Chỉ gửi phần thân CV, tách theo section, bỏ comment. Preamble LaTeX (package, định dạng) giữ ở máy; chỉ gửi các dòng `\newcommand` để Claude hiểu macro. | Không gửi phần không cần sửa. |
| 4. Prompt caching | System prompt cố định và khối CV được đánh dấu `cache_control`. Sửa cùng một CV cho nhiều JD trong vòng 5 phút thì phần CV được đọc từ cache với giá ~5–10% so với bình thường. | Phần lặp lại gần như miễn phí. |
| 5. Trả về dạng edit | Claude trả JSON (structured output) chỉ gồm các thay đổi `old → new`, không viết lại cả file. App tự áp edit và ghép lại với preamble gốc. | Token output đắt gấp 5 lần input, mà giờ chỉ còn vài trăm token. |
| 6. Tự sửa khi cần | Nếu biên dịch lỗi, chỉ gửi **section đã đổi** cùng đoạn log lỗi ngắn. Nếu CV dài thêm trang, nhờ rút gọn một lần. | Chỉ tốn thêm token khi thật sự có lỗi. |

Với chế độ Claude Code, các bước 1–3 và 5–6 giữ nguyên. Bước 4 do Claude Code tự quản lý cache. App gọi Claude Code với system prompt riêng và tắt hết tool (trừ khi cần đọc ảnh/PDF JD), để không tốn hạn mức cho prompt mặc định của Claude Code. Một lần sửa CV thử nghiệm mất khoảng 30 giây.

Bảng chi phí ở cuối trang kết quả cho thấy số token và chi phí ước tính của từng bước (gồm cả token cache ghi/đọc).

Gợi ý chọn model: `claude-opus-5-5` (mặc định, viết tốt nhất), `claude-sonnet-5-5` (rẻ bằng một nửa, vẫn rất tốt), `claude-haiku-5-5` (rẻ nhất, hợp để thử nhanh). Effort `low` nhanh và rẻ hơn, `high` kỹ hơn.

## Cấu trúc

```
cv-tailor/
├── app/
│   ├── main.py        # FastAPI: /api/jd, /api/tailor, tải file
│   ├── jd_input.py    # đọc JD từ text / link / PDF
│   ├── llm.py         # gọi Claude (API hoặc Claude Code): tóm tắt JD, sửa CV, sửa lỗi
│   ├── latex.py       # tách section, áp edit, biên dịch PDF
│   └── static/index.html
├── tests/             # chạy: python -m pytest tests (không cần API key)
├── output/            # kết quả mỗi lần chạy
└── data/jd_cache/     # cache tóm tắt JD
```

## Lưu ý

- App nhận ra section qua `\section`, `\cvsection` và mọi macro tự định nghĩa có chữ "sec" trong tên (ví dụ `\secname`). Nếu template của bạn dùng tên khác, cả CV vẫn chạy được nhưng sẽ được coi là một khối lớn.
- Comment `%` nằm trên một dòng riêng trong phần thân CV sẽ bị bỏ ở file kết quả.
- Trang cần đăng nhập hoặc tải bằng JavaScript (LinkedIn, một số trang tuyển dụng) không đọc được qua link; hãy dán text hoặc chụp màn hình.
- Luôn đọc lại CV trước khi gửi. Phần "Các thay đổi" hiển thị từng đoạn trước/sau để bạn kiểm tra nhanh.
