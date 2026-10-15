# Flat Studio — ảnh váy → rập phẳng (web app, Windows)

Ảnh váy mặc thật → layout cắt-may **phẳng**. Web app khởi động bằng `.bat`, không cần quyền admin.

## Cài (1 lần)
```
setup.bat
```
Tự tải **Python + Node + Git** portable vào `%USERPROFILE%\dsds_studio\tools`, cài `aiohttp/pillow/opencv/…` + `codex` (npm), tự tải **`openart.exe`** (bản Windows), rồi lần lượt mở **đăng nhập OpenArt** (chờ bạn xong) → **đăng nhập ChatGPT/codex**.

Cần chuẩn bị thêm:
- `.env` cạnh file này có dòng `ARK_API_KEY=ark-...` (cho dola-seed nhìn ảnh).
- `bin\chatgpt-imagegen` (script python — gen chính, chạy qua python nên chạy được trên Windows).
- `bin\openart.exe` (fallback) — **đã đóng gói sẵn** trong `bin\`, không cần tải. (Nếu thiếu, setup sẽ tự tải lại từ GitHub release.)

## Chạy
```
start.bat            REM mở http://127.0.0.1:8770
start.bat 9000       REM đổi cổng
```

## macOS
Windows dùng `setup.bat`/`start.bat`; macOS dùng 2 file `.command` (double-click chạy):
```
./setup-mac.command   # 1 lần: brew cài Python/Node/Git + venv + thư viện + codex + OpenArt(Mac) login
./start-mac.command   # chạy app (vòng lặp cho nút Restart), mặc định cổng 8770; ./start-mac.command 9000 để đổi
```
**Nếu double-click báo "không xác minh được / không mở được" (Gatekeeper chặn file tải từ zip) — chọn 1 cách:**

- **Cách A (nhanh nhất, 1 lệnh Terminal, làm 1 lần):** mở **Terminal** (`⌘+Space` → gõ "Terminal"), gõ đoạn sau + **1 dấu cách** ở cuối (chưa Enter):
  ```
  xattr -dr com.apple.quarantine 
  ```
  rồi **kéo-thả thư mục `studio`** vào cửa sổ Terminal (tự điền đường dẫn) → **Enter**. Xong, double-click chạy bình thường.
- **Cách B (không cần Terminal):** double-click file `.command` → bấm **Cancel** → vào **System Settings → Privacy & Security** → thấy dòng "…was blocked" → **Open Anyway** → xác nhận. Làm cho cả `setup-mac.command` và `start-mac.command` lần đầu. (macOS Sequoia 15 đã bỏ mẹo chuột-phải → Open cho script nên phải qua đây; máy cũ hơn có thể chuột phải → **Open** → **Open**.)

**Máy Intel (x86_64):** Homebrew bản mới đã bỏ hỗ trợ Intel (báo "only supported on Apple Silicon"). `setup-mac.command` tự nhận diện: máy Intel → cài Python 3.12 (python.org, installer universal2) + Node 20 (nodejs.org) + Git (Xcode Command Line Tools) thay cho Homebrew. Cần macOS 11 (Big Sur) trở lên. Chỉ Apple Silicon mới dùng Homebrew.

OpenArt bản Mac (arm64 + amd64) đã bundle sẵn trong `bin/`, `setup-mac` tự copy đúng loại máy. Lõi tool chạy y hệt Windows; chỉ khác bộ khởi chạy.

## Giao diện
- **Bàn cắt (trên):** hiện real-time — mảnh crop → 4 panel gen → thành phẩm ghép.
- **Bảng điều khiển (dưới):** chọn chế độ (1 ảnh / 2 ảnh / 2-view / nhóm / **In tràn**), thả ảnh, bấm **Tạo rập**, xem **Nhật ký**.
  - **In tràn (AOP):** cho mọi trang phục — xuất 1 tấm phẳng = nền màu vải + graphic mặt trước (không tách panel). Ảnh nhiều người → mỗi người 1 tấm.
  - **Cả bộ (outfit):** 1 bản vẽ kỹ thuật phẳng cả áo + quần, đủ chi tiết mặt trước (nút, túi, huy hiệu, miếng đầu gối…), nền xám, không người. Ảnh nhiều người → mỗi người 1 bản.
  - **Tách mảnh (pieces):** dàn từng mảnh rập tách rời (thân trước, tay, mũ, túi, ống quần, bo…) trên 1 sheet xám — như tờ rập cắt-may. Mặt trước, mỗi người 1 sheet.
- **Badge codex hết hạn:** hiện nút *Đăng nhập lại* (chạy `codex login`, hiện URL/mã) khi phát hiện codex lỗi auth.

## Pipeline (lõi)
`dola-seed` (ARK vision) nhìn+crop → `chatgpt-imagegen` (codex) gen, lỗi → **fallback OpenArt Seedream 4.5** → cắt nền + ghép. Xem `flat_pipeline.py`.

## Tự cập nhật
`autoupdate.sh` chạy nền: mỗi 60s `git fetch` + `reset --hard` về remote. Cần cấu hình 1 lần:
```
git init && git add . && git commit -m init
git remote add origin <URL-REPO>
```
Có code mới → app hiện nút **Restart**. Dữ liệu (ảnh) nằm ở `%USERPROFILE%\dsds_studio\` (ngoài repo) nên reset an toàn.

## Dữ liệu (NGOÀI folder studio)
Mặc định nằm cạnh studio (cùng folder cha): `../flat_studio_data/`
- Ảnh upload: `flat_studio_data/uploads/`
- Kết quả mỗi lần chạy: `flat_studio_data/runs/<timestamp>/` (crop_*, panel_*, final.png, run.log)

Đổi chỗ khác: đặt biến môi trường `STUDIO_DATA=D:\duong\dan\folder` trước khi chạy (hoặc thêm vào start.bat).
Để ngoài repo nên autoupdate `git reset --hard` không đụng tới.

## Mở thư mục nhanh
Bấm vào bất kỳ ảnh nào trên bàn cắt (mảnh crop, panel, hay thành phẩm) → mở thẳng thư mục chứa ảnh đó bằng File Explorer.
