# Batch Lark → Flat pieces (chạy qua đêm, không giao diện)

Ngày: 2026-10-15 · Trạng thái: đã duyệt thiết kế, chờ review spec

## Mục tiêu

Chạy 1 lệnh terminal qua đêm: đọc Lark Base, với mỗi mẫu **No File chưa có ảnh** → tải ảnh
Link 1…N → Claude chọn ảnh đầu vào → `flat_pipeline.py` sinh mảnh rập (pieces 3D) + bản vẽ
phẳng cả bộ (art) → Claude QA, gen lại mảnh lỗi → up kết quả lên Base. Xong mẫu này sang mẫu
tiếp, tự dừng an toàn, báo kết quả về Lark.

Không thay giao diện web hiện có (giữ nguyên, không xoá).

## Nguồn dữ liệu (Lark Base)

| Thứ | Giá trị |
|---|---|
| Base | `NQMeb69ZRak4kfs40oQlMfySgXA` (sg.larksuite.com) |
| Bảng | `ORD_01` = `tblZhM8uIGIzQ4fx` |
| View | `1. DS Order` = `vew0cgQ2cZ` |
| Lọc "cần làm" | `Check File` (multi-select) chứa `No File` |
| Cột ảnh đầu vào | `Link 1` … `Link 20` (text kiểu URL, giá trị dạng Markdown `[url](url)`) |
| Cột kết quả | `Flat Pieces` = `fldVzvTA9Q` (attachment) — có file = "đã có ảnh" |
| Cột ghi chú | `Flat Note` = `fldGVXw4ph` (text) |
| Thông tin phụ (chỉ đọc) | `SKU Design`, `Color`, `Create Date`, `Check File` |
| Truy cập | `lark-cli --profile tool_ds --as user` (app Tool_DS `cli_aa5a2152f1f5df1d`, user token của chủ tài khoản, không dùng bot) |

Số liệu lúc thiết kế: ~3.374 dòng No File, ~2.690 bộ link khác nhau, nhiều sản phẩm không
phải quần áo (Magnet Die Cut, Plaque, Vase, Mug…).

**Tool chỉ ghi 2 cột `Flat Pieces` và `Flat Note`.** Không đụng `Check File`, `DS Status`,
`Image` hay cột khác.

## Lệnh chạy

```
python batch.py                          # chạy tới hết việc
python batch.py --until 07:00 --limit 30 # dừng theo giờ / số nhóm
python batch.py --dry-run --limit 3      # làm đủ trừ ghi Base
python batch.py --date 2026-10-14       # chạy bù mẫu của 1 ngày khác
python batch.py --selfcheck              # kiểm tra logic, không gọi mạng
```

## Kiến trúc

| File | Việc |
|---|---|
| `studio/batch.py` | Vòng lặp: hàng đợi → từng nhóm → điều kiện dừng; lock; báo Lark |
| `studio/lark_base.py` | Bọc `lark-cli`: đọc dòng (phân trang 2000, `--overwrite`), up đính kèm, gỡ đính kèm, ghi Note |
| `studio/judge.py` | Ghép contact sheet + gọi `claude -p` → JSON (chọn ảnh, chia màu, QA, viết hint) |
| `studio/flat_pipeline.py` | Lõi sẵn có, chỉ thêm: `--color`, `--hint` (+ sửa lỗi regen run 2 ảnh/combined) |

`judge.py` gọi `claude -p` headless, mỗi lần một ngữ cảnh mới (không phình token), ép trả JSON;
ảnh luôn được **ghép thành 1 contact sheet trước khi gửi** (tiết kiệm token).

## Luồng

1. **Hàng đợi.** Lấy dòng trong view có `No File`, `Flat Pieces` trống, `Flat Note` không bắt
   đầu bằng `SKIP`, và **`Create Date` = ngày bắt đầu chạy** (mẫu ngày cũ không chạy; `--date
   YYYY-MM-DD` để chọn ngày khác). Gom theo bộ link (Link 1…20 sau khi bóc Markdown). Sắp **mới nhất trước**
   theo `Create Date` (lấy dòng mới nhất trong nhóm).
2. **Mỗi nhóm:**
   1. Tải Link 1…N → `flat_studio_data/batch/<YYYY-MM-DD>/<nhóm>/src/`.
   2. **Judge chọn ảnh:** contact sheet đánh số các ảnh + danh sách (record_id, SKU Design,
      Color) của nhóm. Claude trả:
      - `skip` + lý do (không phải quần áo) → Note `SKIP: …` cho cả nhóm, xong.
      - hoặc `front` (số ảnh), `back` (số ảnh | null), `mode` (`infer`|`two`|`combined`),
        `crop` tuỳ chọn (bbox 0–1000 nếu ảnh nhiều người / cần cắt), và
        `colors`: `[{color, matches_photo, record_ids}]` — chia nhóm con theo màu suy từ
        `SKU Design` + `Color`.
   3. **Mỗi nhóm con màu:** chạy pipeline `--pieces` (3D, mặc định) và `--art`; màu khác ảnh
      gốc → truyền `--color <màu>` (ép màu nền vải, giữ họa tiết).
   4. **QA + gen lại** (mục dưới).
   5. **Up + ghi Note** (mục dưới), rồi dọn file tạm.
3. Sau mỗi nhóm kiểm tra điều kiện dừng.

## QA và gen lại

**Contact sheet QA** (1 tấm/nhóm con màu): hàng trên = ảnh gốc đã crop (trước, sau nếu có);
dưới = mọi mảnh + art, có nhãn tên mảnh. Mỗi ô ~384px, cả tấm ≤ ~1600px.

Claude trả `{mảnh: {ok, score 1-10, errors: [mã]}}` với mã lỗi cố định:

| Mã | Nghĩa |
|---|---|
| `mo_nhoe` | mờ, nhoè, vỡ nét, nhiễu |
| `sai_mau` | màu nền/họa tiết lệch ảnh gốc hoặc màu yêu cầu |
| `sau_giong_truoc` | mảnh sau bê graphic chỉ có ở mặt trước |
| `sai_hoa_tiet` | họa tiết khác gốc: thiếu, thừa, sai vị trí |
| `sai_manh` | lẫn phần khác (thân dính tay/mũ, có người, móc treo, mockup) |
| `khong_tran` | mảnh áo/quần còn viền/lề, không tràn kín |
| `vung_den` | vùng trơn bị tô đen/void |
| `cat_loi` | mảnh váy cắt silhouette vỡ/mất góc |
| `art_thieu` | art thiếu chi tiết mặt trước / sai bố cục trên-dưới |

**Gen lại** — chỉ mảnh lỗi; mảnh đạt giữ nguyên:
- **Lần 1:** y nguyên — dùng lại `pieces.json`, `--only <mảnh>`.
- **Lần 2–3:** Claude viết `--hint` nêu lỗi cụ thể (vd "BACK panel: plain body, NO chest
  graphic"), chạy lại vision (bỏ `pieces.json`) và **crop lại** (bbox chặt hơn hoặc chọn ảnh
  khác trong Link 1…N do Claude chỉ định).
- Mỗi lần gen lại → QA bằng contact sheet nhỏ (ảnh gốc + mảnh vừa gen).
- **Sau 3 lần vẫn lỗi:** up **bản điểm cao nhất** trong 4 bản (gốc + 3 lần), tên file thêm
  `_QAFAIL`, Note ghi lỗi.

## Up lên Base

- **Tên file:** `<SKU Design bỏ size + mã lô cuối>_<mảnh>.png`, vd
  `HKH032604A21-Black-2D Hoodie_ao_truoc.png`, `…_art.png`, `…_tay_sau_QAFAIL.png`.
  Ký tự cấm trong tên file thay bằng `_`.
- Váy: PNG nền trong suốt; áo/quần: PNG đục, in tràn.
- `+record-upload-attachment` **nối thêm** file, nên:
  1. Ngay trước khi up, đọc lại dòng; `Flat Pieces` đã có file → không up (chỉ log).
  2. Up mọi mảnh của 1 dòng trong **1 lệnh** (nhiều `--file`).
  3. Lỗi giữa chừng → gỡ file vừa up khỏi ô (`+record-remove-attachment`), Note `LỖI: up`.
  4. Mỗi dòng của nhóm con màu up riêng, cùng bộ file.
- **Ghi Note sau khi up thành công**, gộp ≤200 dòng/lệnh, tuần tự (tránh lỗi 1254291).
- Mẫu Note (1 dòng, ghi đè):
  - `OK 2026-10-16 · 6 mảnh + art · gen lại: tay_sau×1`
  - `QA FAIL 2026-10-16 · tay_sau (sau_giong_truoc) sau 4 lần`
  - `SKIP: không phải quần áo (Magnet Die Cut)` → các lần sau bỏ qua
  - `LỖI: tải ảnh | judge | up | quá thời gian` → Flat Pieces trống nên lần sau thử lại
- `--dry-run`: bỏ mọi bước ghi Base, phần còn lại chạy thật.

## Lỗi, dừng, an toàn

| Tình huống | Xử lý |
|---|---|
| Link chết / tải lỗi | thử lại 2 lần → `LỖI: tải ảnh` → nhóm tiếp |
| Claude trả JSON hỏng | hỏi lại 1 lần → `LỖI: judge` → nhóm tiếp |
| Gen lỗi (codex lẫn OpenArt) | tính là 1 lần gen hỏng trong vòng 4 lần |
| **Hết hạn mức** codex/Claude | **báo Lark**, chờ 30 phút, thử lại tối đa 3 lần; vẫn hết → dừng đợt |
| **Hết đăng nhập** codex / lark-cli | dừng ngay |
| 5 nhóm liên tiếp lỗi cùng kiểu | dừng đợt |
| 1 nhóm > 45 phút | bỏ nhóm, `LỖI: quá thời gian` |
| Ctrl+C / mất điện | nhóm dở chưa ghi Base (chỉ ghi ở cuối nhóm) → chạy lại là làm tiếp |
| `--until HH:MM` / `--limit N` | làm nốt nhóm đang chạy rồi dừng |

- **Lock** `flat_studio_data/batch.lock` (PID) chặn 2 lần chạy song song; lock của PID đã chết
  thì tự gỡ.
- **Dọn đĩa:** sau khi up thành công xoá ảnh tải về + bản gen trung gian; giữ mảnh cuối,
  contact sheet, JSON judge, log trong `flat_studio_data/batch/<ngày>/<nhóm>/`.
- Chạy **tuần tự** từng nhóm (bản đầu); song song để sau nếu cần.

## Báo về Lark

Webhook bot Lark đọc từ `.env` (`LARK_WEBHOOK=…`, không commit). Gửi khi:
- **Chạm hạn mức** (mỗi lần bắt đầu chờ 30 phút): `⏸ Hết hạn mức codex — chờ 30' (lần 1/3)`.
- **Dừng đợt** vì bất kỳ lý do nào (hết việc, `--until`, `--limit`, hết hạn mức, hết đăng
  nhập, 5 lỗi liên tiếp, Ctrl+C, lỗi không lường): tóm tắt
  `Đợt 15/10: 42 OK · 3 QA FAIL · 120 SKIP · 2 LỖI · dừng vì: <lý do> · log: <đường dẫn>`.

Gửi lỗi webhook không được làm dừng đợt (chỉ log).

## Kiểm thử

1. `python batch.py --selfcheck` (assert, không mạng): bóc URL Markdown, lọc hàng đợi, gom
   nhóm link + màu, sắp mới nhất trước, đặt tên file, máy trạng thái gen lại 4 lần (judge giả
   lập), điều kiện dừng, phân loại lỗi hạn mức/đăng nhập. `flat_pipeline.py --selfcheck` vẫn
   pass (thêm assert cho `--color`/`--hint`).
2. `--dry-run --limit 3` trên Base thật → cùng xem contact sheet, mảnh, JSON QA.
3. `--limit 3` ghi thật → kiểm tra `Flat Pieces` / `Flat Note` trên Base.
4. Đêm đầu `--until 07:00 --limit 30`; ổn mới bỏ giới hạn.
5. Codex review trước khi coi là xong.

## Ngoài phạm vi

Giao diện web mới; chạy song song nhiều nhóm; tự đổi `Check File`/`DS Status`; xử lý sản phẩm
không phải quần áo.
