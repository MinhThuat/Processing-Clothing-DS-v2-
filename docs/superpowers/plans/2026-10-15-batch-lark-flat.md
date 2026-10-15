# Batch Lark → Flat pieces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Một lệnh `python batch.py` chạy qua đêm: đọc Lark Base → tải ảnh → Claude chọn ảnh → `flat_pipeline.py` (pieces 3D + art) → Claude QA + gen lại → up `Flat Pieces` + ghi `Flat Note`, báo Lark khi chạm hạn mức / dừng.

**Architecture:** Thêm 4 file nhỏ trong `studio/` cạnh lõi sẵn có: `batch_errors.py` (phân loại lỗi), `lark_base.py` (bọc `lark-cli`), `judge.py` (contact sheet + `claude -p`), `batch.py` (hàng đợi, máy trạng thái gen lại, vòng lặp đêm). `flat_pipeline.py` chỉ thêm cờ `--single/--color/--hint` và không còn chết cả run khi 1 mảnh lỗi. Mọi bước I/O nằm sau các hàm thuần có `--selfcheck` (assert, không mạng) theo đúng kiểu `flat_pipeline.py --selfcheck` của repo.

**Tech Stack:** Python 3 (stdlib + Pillow 12 + python-dotenv, đã có trong `requirements.txt`), `lark-cli` 1.0.96 profile `tool_ds`, Claude Code CLI 2.1.281 (`claude -p`), `flat_pipeline.py` (codex → OpenArt).

**Spec:** `docs/superpowers/specs/2026-10-15-batch-lark-flat-design.md`

## Global Constraints

- Lark: Base `NQMeb69ZRak4kfs40oQlMfySgXA`, bảng `tblZhM8uIGIzQ4fx` (ORD_01), view `vew0cgQ2cZ` (1. DS Order); lệnh luôn `lark-cli … --profile tool_ds --as user` (không dùng bot).
- Lọc cần làm: `Check File` chứa `No File` + `Flat Pieces` trống + `Flat Note` không bắt đầu bằng `SKIP`.
- Chỉ ghi 2 cột: `Flat Pieces` = `fldVzvTA9Q` (attachment), `Flat Note` = `fldGVXw4ph` (text). Không đụng cột nào khác.
- Ảnh luôn được ghép thành **1 contact sheet** trước khi đưa Claude nhìn (ô ~384px, tấm ≤ 1600px).
- `claude -p` luôn gọi kiểu ngữ cảnh tối thiểu (~4k token/lần): `--tools Read --allowedTools Read --strict-mcp-config --disable-slash-commands --setting-sources "" --no-session-persistence --system-prompt <ngắn> --output-format json`, model mặc định `sonnet` (env `FLAT_JUDGE_MODEL`).
- Gen lại: lần 1–2 y nguyên (`--cache pieces.json --only`), lần 3–4 Claude viết `--hint` + chọn lại ảnh/crop; sau 4 lần up bản điểm cao nhất, tên `_QAFAIL`.
- Hạn mức: báo Lark → chờ 30' → thử lại, tối đa 3 lần; lần 4 dừng. Hết đăng nhập → dừng ngay. 5 nhóm liên tiếp lỗi cùng kiểu → dừng. 1 nhóm > 45' → `LỖI: quá thời gian`.
- Báo Lark khi chạm hạn mức và khi dừng đợt (mọi lý do); webhook ở `studio/.env` khoá `LARK_WEBHOOK` (đã bị `.gitignore`), lỗi gửi không làm dừng đợt.
- Hàng đợi sắp **mới nhất trước** theo `Create Date`; `--limit N` đếm số nhóm đã gen (nhóm SKIP không tính).
- Kiểm thử theo kiểu repo: hàm `_selfcheck()` dùng `assert`, chạy `python <file>.py` hoặc `python batch.py --selfcheck`; không thêm pytest.
- Code/comment tiếng Việt như code hiện có; prompt cho model bằng tiếng Anh.
- Lệnh chạy từ thư mục `studio/` (`cd /mnt/6C96C1A096C16AE2/vsc/CLOTHING_STUDIO/studio`). Commit từ repo root, kết thúc message bằng dòng `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## File Structure

| File | Trách nhiệm |
|---|---|
| Create `studio/batch_errors.py` | `QuotaError`, `AuthError`, `StepError(kind)`, regex `QUOTA_RE`/`AUTH_RE`, `raise_for(text, kind)` |
| Create `studio/lark_base.py` | Hằng số Base; `clean_url`, `parse_row`, `fetch_rows`, `get_row`, `upload_files`, `remove_files`, `write_notes` |
| Create `studio/judge.py` | `grid`, `contact_sheet`, `ask` (claude -p), `pick`/`qa`/`rehint` + `norm_pick`/`norm_qa` |
| Create `studio/batch.py` | Hàm thuần (`sku_stem`, `piece_filename`, `build_queue`, ghi chú, `Stopper`, `improve`) + I/O (tải, crop, chạy pipeline, nhóm con, nhóm, up, dọn, lock, báo Lark, `main`) |
| Modify `studio/flat_pipeline.py` | `COLOR`/`HINT` + `_prompt_extra()`; cờ `--single/--color/--hint`; mảnh lỗi không giết cả run |
| Modify `studio/server.py:174-178` | `/regen` tìm `pieces.json` ở thư mục run khi không có `person_N/` (lỗi run 2 ảnh / combined) |
| Modify `studio/.env` (không commit) | thêm `LARK_WEBHOOK=` |

---

### Task 1: batch_errors.py — phân loại lỗi

**Files:**
- Create: `studio/batch_errors.py`

**Interfaces:**
- Produces: `class QuotaError(RuntimeError)`, `class AuthError(RuntimeError)`, `class StepError(RuntimeError)` với thuộc tính `.kind: str`; `QUOTA_RE`, `AUTH_RE` (compiled regex); `raise_for(text: str, kind: str) -> NoReturn` (luôn ném: AuthError nếu khớp AUTH_RE, QuotaError nếu khớp QUOTA_RE, còn lại `StepError(kind)`).

- [ ] **Step 1: Viết selfcheck trước (file chỉ có test)**

Tạo `studio/batch_errors.py`:

```python
"""Lỗi dùng chung cho batch.py / judge.py / lark_base.py.

QuotaError -> batch chờ 30' rồi thử lại (tối đa 3 lần); AuthError -> dừng đợt ngay;
StepError(kind) -> ghi 'LỖI <ngày>: <kind>' cho nhóm rồi sang nhóm tiếp.
"""
import re


def _selfcheck():
    def kind_of(text):
        try:
            raise_for(text, "gen")
        except AuthError:
            return "auth"
        except QuotaError:
            return "quota"
        except StepError as e:
            return e.kind
    assert kind_of('HTTP 429: {"error":{"type":"usage_limit_reached"}}') == "quota"
    assert kind_of("Claude AI usage limit reached|1760000000") == "quota"
    assert kind_of("token refresh failed: HTTPError") == "auth"
    assert kind_of("[vision   ] lỗi codex: codex chưa đăng nhập") == "auth"
    assert kind_of("RuntimeError: openart không tạo file") == "gen"
    assert kind_of("") == "gen"
    e = StepError("tải ảnh", "x")
    assert e.kind == "tải ảnh" and "x" in str(e)
    print("batch_errors selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
```

- [ ] **Step 2: Chạy, xác nhận FAIL**

Run: `cd /mnt/6C96C1A096C16AE2/vsc/CLOTHING_STUDIO/studio && python3 batch_errors.py`
Expected: `NameError: name 'raise_for' is not defined`

- [ ] **Step 3: Thêm phần cài đặt (chèn ngay sau `import re`, trước `def _selfcheck`)**

```python
class QuotaError(RuntimeError):
    """Hết hạn mức codex / Claude / OpenArt."""


class AuthError(RuntimeError):
    """Hết đăng nhập codex / lark-cli / Claude."""


class StepError(RuntimeError):
    """Lỗi 1 bước của 1 nhóm. kind: 'tải ảnh' | 'judge' | 'gen' | 'up' | 'lark' | 'quá thời gian'."""

    def __init__(self, kind, msg=""):
        super().__init__(f"{kind}: {msg}" if msg else kind)
        self.kind = kind


QUOTA_RE = re.compile(
    r"HTTP 429|usage[_ ]limit|rate[_ ]limit|too many requests|quota|out of credits"
    r"|insufficient (credits|balance)|credit balance", re.I)
AUTH_RE = re.compile(
    r"token refresh failed|access token expired|refresh token expired|HTTP 401|\bunauthori[sz]ed"
    r"|run `?codex login|codex chưa đăng nhập|need_user_authorization|not logged in"
    r"|invalid api key|openart login", re.I)


def raise_for(text, kind):
    """Luôn ném lỗi: AuthError / QuotaError nếu nội dung khớp, không thì StepError(kind)."""
    t = (text or "").strip()
    if AUTH_RE.search(t):
        raise AuthError(t[-300:])
    if QUOTA_RE.search(t):
        raise QuotaError(t[-300:])
    raise StepError(kind, t[-300:])
```

- [ ] **Step 4: Chạy, xác nhận PASS**

Run: `python3 batch_errors.py`
Expected: `batch_errors selfcheck OK`

- [ ] **Step 5: Commit**

```bash
cd /mnt/6C96C1A096C16AE2/vsc/CLOTHING_STUDIO
git add studio/batch_errors.py
git commit -m "feat(batch): phân loại lỗi hạn mức / đăng nhập / bước

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: lark_base.py — đọc/ghi Base qua lark-cli

**Files:**
- Create: `studio/lark_base.py`

**Interfaces:**
- Consumes: `batch_errors.AuthError`, `batch_errors.StepError`.
- Produces:
  - Hằng `PROFILE, BASE, TABLE, VIEW, F_PIECES, F_NOTE, N_LINKS, FIELDS`.
  - `clean_url(v) -> str`
  - `parse_row(raw: dict) -> dict` với khoá `record_id, sku, color, created, check_file (list[str]), links (list[str] đã bóc), has_pieces (bool), pieces (list[{file_token,name,size}]), note (str)`.
  - `fetch_rows() -> list[dict]` (mọi dòng No File của view, đã `parse_row`).
  - `get_row(rid: str) -> dict` (1 dòng, `parse_row`).
  - `upload_files(rid: str, files: list[Path]) -> None` (1 lệnh, nối vào `Flat Pieces`).
  - `remove_files(rid: str, tokens: list[str]) -> None`.
  - `write_notes(notes: dict[str, str]) -> None` (≤200 dòng/lệnh, tuần tự).

- [ ] **Step 1: Viết selfcheck trước**

Tạo `studio/lark_base.py`:

```python
"""Đọc/ghi Lark Base bằng lark-cli (profile tool_ds, user token — không dùng bot).

Chỉ GHI 2 cột: Flat Pieces (đính kèm) và Flat Note (text).
Spec: docs/superpowers/specs/2026-10-15-batch-lark-flat-design.md
"""
import json, re, subprocess, tempfile
from pathlib import Path

from batch_errors import AuthError, StepError


def _selfcheck():
    u = "https://i.etsystatic.com/1/r/il/a/2/il_fullxfull.2_x.jpg"
    assert clean_url(f"[{u}]({u})") == u
    assert clean_url(u) == u
    assert clean_url(None) == "" and clean_url("") == "" and clean_url("không có link") == ""
    raw = {"record_id": "recA", "SKU Design": " HKH-Black-2D Hoodie-L-G1 ", "Color": None,
           "Create Date": "2026-04-13T11:22:20.000+07:00", "Check File": ["No File", "Urgent"],
           "Flat Pieces": [], "Flat Note": None, "Link 1": f"[{u}]({u})", "Link 3": u}
    r = parse_row(raw)
    assert r["record_id"] == "recA" and r["sku"] == "HKH-Black-2D Hoodie-L-G1" and r["color"] == ""
    assert r["links"] == [u, u] and r["has_pieces"] is False and r["note"] == ""
    assert r["check_file"] == ["No File", "Urgent"]
    r2 = parse_row({**raw, "Flat Pieces": [{"file_token": "t1", "name": "a.png"}], "Flat Note": "SKIP: x"})
    assert r2["has_pieces"] and r2["pieces"][0]["file_token"] == "t1" and r2["note"] == "SKIP: x"
    assert len(FIELDS) == 6 + N_LINKS
    print("lark_base selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
```

- [ ] **Step 2: Chạy, xác nhận FAIL**

Run: `python3 lark_base.py`
Expected: `NameError: name 'clean_url' is not defined`

- [ ] **Step 3: Thêm phần cài đặt (chèn sau dòng `from batch_errors import …`, trước `def _selfcheck`)**

```python
PROFILE = "tool_ds"
BASE = "NQMeb69ZRak4kfs40oQlMfySgXA"
TABLE = "tblZhM8uIGIzQ4fx"      # ORD_01
VIEW = "vew0cgQ2cZ"             # 1. DS Order
F_PIECES = "fldVzvTA9Q"         # Flat Pieces (attachment)
F_NOTE = "fldGVXw4ph"           # Flat Note (text)
N_LINKS = 20
FIELDS = (["SKU Design", "Color", "Create Date", "Check File", "Flat Pieces", "Flat Note"]
          + [f"Link {i}" for i in range(1, N_LINKS + 1)])
_MD = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
_URL = re.compile(r"https?://[^\s)\]]+")
# lỗi đăng nhập/cấu hình -> dừng đợt; tránh bắt nhầm chữ 'file_token' trong lỗi upload
_AUTH = re.compile(r"user_access_token|access token|refresh token|need_user_authorization"
                   r"|auth login|unauthori|not_configured|invalid_client|expired", re.I)


def clean_url(v):
    """'[url](url)' / url trần / None -> url hoặc ''."""
    v = v if isinstance(v, str) else " ".join(map(str, v or []))
    m = _MD.search(v)
    if m:
        return m.group(1)
    m = _URL.search(v)
    return m.group(0) if m else ""


def parse_row(raw):
    """1 dòng NDJSON của lark-cli -> dict gọn cho batch."""
    return {
        "record_id": raw["record_id"],
        "sku": (raw.get("SKU Design") or "").strip(),
        "color": (raw.get("Color") or "").strip(),
        "created": raw.get("Create Date") or "",
        "check_file": raw.get("Check File") or [],
        "links": [u for u in (clean_url(raw.get(f"Link {i}")) for i in range(1, N_LINKS + 1)) if u],
        "has_pieces": bool(raw.get("Flat Pieces")),
        "pieces": raw.get("Flat Pieces") or [],
        "note": (raw.get("Flat Note") or "").strip(),
    }


def _cli(args, timeout=300):
    """Chạy lark-cli, trả JSON stdout. Lỗi đăng nhập -> AuthError, lỗi khác -> StepError('lark')."""
    cmd = ["lark-cli", *map(str, args), "--profile", PROFILE, "--as", "user"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise StepError("lark", f"lark-cli quá {timeout}s: {' '.join(map(str, args[:2]))}")
    m = re.search(r"\{.*\}", p.stdout or "", re.S)
    try:
        js = json.loads(m.group()) if m else {}
    except ValueError:
        js = {}
    if p.returncode == 0 and js.get("ok") is not False:
        return js
    err = (json.dumps(js["error"], ensure_ascii=False) if js.get("error")
           else (p.stderr or p.stdout or "")[-400:])
    if _AUTH.search(err):
        raise AuthError(f"lark-cli: {err[:300]}")
    raise StepError("lark", err[:300])


def _read_ndjson(args):
    """Chạy lệnh đọc có --output ndjson -> (summary JSON, list dòng thô)."""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "rows.ndjson"
        js = _cli([*args, "--format", "ndjson", "--output", out, "--overwrite"])
        lines = out.read_text(encoding="utf-8").splitlines() if out.exists() else []
    return js, [json.loads(l) for l in lines if l.strip()]


def fetch_rows():
    """Mọi dòng có Check File chứa 'No File' trong view 1. DS Order (phân trang 2000/lần)."""
    filt = json.dumps({"logic": "and", "conditions": [["Check File", "intersects", ["No File"]]]})
    rows, off = [], 0
    while True:
        args = ["base", "+record-list", "--base-token", BASE, "--table-id", TABLE, "--view-id", VIEW,
                "--offset", off, "--filter-json", filt]
        for f in FIELDS:
            args += ["--field-id", f]
        js, raws = _read_ndjson(args)
        rows += [parse_row(r) for r in raws]
        if not js.get("has_more"):
            return rows
        off = js.get("next_offset") or off + len(raws)


def get_row(rid):
    """Đọc lại 1 dòng (ngay trước khi up)."""
    args = ["base", "+record-get", "--base-token", BASE, "--table-id", TABLE, "--record-id", rid]
    for f in FIELDS:
        args += ["--field-id", f]
    _, raws = _read_ndjson(args)
    if not raws:
        raise StepError("lark", f"không đọc được {rid}")
    return parse_row(raws[0])


def upload_files(rid, files):
    """Up nhiều file vào Flat Pieces của 1 dòng trong 1 lệnh (lark-cli nối thêm, không thay)."""
    args = ["base", "+record-upload-attachment", "--base-token", BASE, "--table-id", TABLE,
            "--record-id", rid, "--field-id", F_PIECES]
    for f in files:
        args += ["--file", Path(f).resolve()]
    _cli(args, timeout=900)


def remove_files(rid, tokens):
    """Gỡ file khỏi Flat Pieces (dọn khi up dở)."""
    args = ["base", "+record-remove-attachment", "--base-token", BASE, "--table-id", TABLE,
            "--record-id", rid, "--field-id", F_PIECES, "--yes"]
    for t in tokens:
        args += ["--file-token", t]
    _cli(args)


def write_notes(notes):
    """Ghi Flat Note: {record_id: text}; ≤200 dòng/lệnh, tuần tự (tránh lỗi 1254291)."""
    items = list(notes.items())
    for i in range(0, len(items), 200):
        body = {"update_records": {rid: {F_NOTE: text} for rid, text in items[i:i + 200]}}
        _cli(["base", "+record-batch-update", "--base-token", BASE, "--table-id", TABLE,
              "--json", json.dumps(body, ensure_ascii=False)])
```

- [ ] **Step 4: Chạy selfcheck, xác nhận PASS**

Run: `python3 lark_base.py`
Expected: `lark_base selfcheck OK`

- [ ] **Step 5: Smoke đọc thật (chỉ đọc, không ghi)**

Run:
```bash
python3 -c "
import lark_base as lb
rows = lb.fetch_rows()
print(len(rows), 'dòng No File;', sum(1 for r in rows if r['links']), 'có link')
r = lb.get_row(rows[0]['record_id']); print(r['record_id'], r['sku'], len(r['links']), r['has_pieces'])"
```
Expected: khoảng `33xx dòng No File; 33xx có link` và một dòng `rec… <SKU> <số link> False`. Nếu ra `AuthError` → chạy `lark-cli auth login --profile tool_ds` rồi thử lại.

- [ ] **Step 6: Commit**

```bash
cd /mnt/6C96C1A096C16AE2/vsc/CLOTHING_STUDIO
git add studio/lark_base.py
git commit -m "feat(batch): lark_base bọc lark-cli đọc/up/ghi note

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: flat_pipeline.py — `--single`, `--color`, `--hint`; server regen fix

**Files:**
- Modify: `studio/flat_pipeline.py` (dòng 43 `EMIT = None`; 385 `base=_base_colour`; 481 `def gen_panel`; 544-549 vòng `for spec in plan`; 600-613 `run_pieces`/`run_art`; selfcheck ~659; argparse 662-694)
- Modify: `studio/server.py:174-178` (hàm `regen`)

**Interfaces:**
- Produces (CLI dùng bởi batch Task 6):
  - `--single`: coi ảnh `--front` là 1 bộ đồ, **bỏ bước tách người** (cả `--pieces` lẫn `--art`).
  - `--color "<màu>"`: ép màu nền vải (thay `{base}` của mảnh full-bleed + câu RECOLOUR cho mọi prompt).
  - `--hint "<ghi chú>"`: ghép `FIX: …` vào cuối mọi prompt gen.
  - Đầu ra khi `--pieces --single -o D/final.png --emit D`: `D/final_<slug>.png`, `D/crop_front.png`, `D/pieces.json` (+ `D/crop_back.png` nếu `--back`). `--art --single -o D/art.png --emit D/art_work`: `D/art.png`.
  - Mảnh lỗi gen → log `✗ mảnh <slug> lỗi` và **làm tiếp mảnh khác**; chỉ ném lỗi (exit ≠ 0) khi không mảnh nào ra.
  - Hàm `_prompt_extra() -> str` (rỗng khi không có `COLOR`/`HINT`).

- [ ] **Step 1: Thêm assert vào `_selfcheck()` (ngay trước `print("selfcheck OK")`)**

```python
    # --color / --hint: ghép vào cuối prompt; mặc định rỗng
    globals()["COLOR"], globals()["HINT"] = None, None
    assert _prompt_extra() == ""
    globals()["COLOR"], globals()["HINT"] = "heliconia pink", "NO chest graphic on back"
    ex = _prompt_extra()
    assert "heliconia pink" in ex and "FIX: NO chest graphic on back" in ex
    globals()["COLOR"], globals()["HINT"] = None, None
```

- [ ] **Step 2: Chạy, xác nhận FAIL**

Run: `python3 flat_pipeline.py --selfcheck`
Expected: `NameError: name '_prompt_extra' is not defined`

- [ ] **Step 3: Cài đặt `COLOR`/`HINT`/`_prompt_extra` (thay dòng 43 `EMIT = None …` bằng khối dưới)**

```python
EMIT = None   # nếu set: crop/panel ghi thẳng vào thư mục này (cho UI web poll)
COLOR = None  # --color: ép màu nền vải (batch: nhóm con màu khác ảnh gốc)
HINT = None   # --hint: ghi chú sửa lỗi QA ghép cuối prompt (batch: gen lại lần 3-4)

def _prompt_extra():
    """Phần ghép thêm vào MỌI prompt gen theo --color/--hint (rỗng nếu không truyền)."""
    s = ""
    if COLOR:
        s += (f" RECOLOUR: the garment's BASE fabric colour must be {COLOR}; keep every graphic, "
              "motif and its position exactly — only the base fabric colour changes.")
    if HINT:
        s += " FIX: " + HINT
    return s
```

Trong `gen_panel` (dòng 481), thêm dòng đầu thân hàm, ngay sau docstring:

```python
    prompt = prompt + _prompt_extra()
```

Trong `_make_piece` (dòng 385) đổi `base=_base_colour(spec["desc"])` thành:

```python
base=COLOR or _base_colour(spec["desc"])
```

- [ ] **Step 4: Mảnh lỗi không giết cả run — thay khối dòng 544-549 trong `run_pieces_one`**

Khối cũ:
```python
    outs = []
    for spec in plan:
        log("gen", f"gen mảnh {spec['slug']} ({'phẳng' if flat else '3D'}) ...")
        outs.append(_make_piece(spec, refs, work, out, flat=flat))
    log("done", f"{len(outs)} mảnh: " + ", ".join(o.name for o in outs))
    return outs[0] if outs else out
```
Khối mới:
```python
    outs, errs = [], []
    for spec in plan:
        log("gen", f"gen mảnh {spec['slug']} ({'phẳng' if flat else '3D'}) ...")
        try:
            outs.append(_make_piece(spec, refs, work, out, flat=flat))
        except Exception as e:                  # 1 mảnh lỗi -> làm tiếp mảnh khác (batch gen lại sau)
            log("gen", f"  ✗ mảnh {spec['slug']} lỗi: {str(e)[:300]}")
            errs.append(e)
    log("done", f"{len(outs)} mảnh: " + ", ".join(o.name for o in outs))
    if errs and not outs:
        raise errs[-1]
    return outs[0] if outs else out
```

- [ ] **Step 5: `--single` — thay `run_pieces` và `run_art` (dòng 600-613)**

```python
def run_pieces(front, out, back=None, combined=False, only=None, cache=None, flat=False, single=False):
    """Tách mảnh. 1 ảnh (tự suy sau) -> tách từng người. --back/--combined/--single -> 1 trang phục.
    only = chỉ gen slug đã chọn; cache = gen lẻ dùng lại mô tả cũ; flat = vẽ phẳng vector thay vì 3D."""
    if back or combined or cache or single:
        client = _ark_client()
        tag = "gen lẻ (cache)" if cache else ("+sau thật (2 ảnh)" if back else
                                             ("2-view (1 ảnh)" if combined else "1 bộ (--single)"))
        log("start", f"PIECES tách mảnh | {Path(front).name} | {tag} | {'phẳng' if flat else '3D'}")
        run_pieces_one(client, front, Path(out), back=back, combined=combined, only=only, cache=cache, flat=flat)
        return [Path(out)]
    return _run_perperson(front, Path(out), run_pieces_one, "PIECES tách mảnh", "piecesset_", only=only, flat=flat)

def run_art(front, out, single=False):
    """Art phẳng: 1 bản vẽ nguyên bộ mặt trước. Tách từng người (--single: không tách)."""
    if single:
        log("start", f"ART phẳng nguyên bộ | {Path(front).name} | 1 bộ (--single)")
        return [run_art_one(_ark_client(), front, Path(out))]
    return _run_perperson(front, Path(out), run_art_one, "ART phẳng nguyên bộ", "artset_")
```

- [ ] **Step 6: Argparse — thêm 3 cờ (ngay trước `ap.add_argument("--selfcheck", …)`)**

```python
    ap.add_argument("--single", action="store_true",
                    help="coi --front là 1 bộ đồ, bỏ bước tách người (batch đã tự crop)")
    ap.add_argument("--color", help="ép màu nền vải (vd 'heliconia pink'), giữ họa tiết")
    ap.add_argument("--hint", help="ghi chú sửa lỗi ghép cuối mọi prompt gen")
```

Và trong nhánh `elif a.front:` thay đoạn từ `if a.emit:` tới hết lời gọi `run_pieces(...)` bằng:

```python
        if a.emit:
            globals()["EMIT"] = a.emit
        globals()["COLOR"], globals()["HINT"] = a.color, a.hint
        out = Path(a.out)
        logpath = _open_log(a.log or out.with_suffix(".log"))
        log("start", f"log -> {logpath}")
        if a.art:
            run_art(a.front, out, single=a.single)
        else:                                   # mặc định: pieces
            run_pieces(a.front, out, back=a.back, combined=a.combined, only=a.only, cache=a.cache,
                       flat=a.flat, single=a.single)
```

- [ ] **Step 7: Chạy selfcheck, xác nhận PASS + help có cờ mới**

Run: `python3 flat_pipeline.py --selfcheck && python3 flat_pipeline.py --help | grep -E "single|color|hint"`
Expected: `selfcheck OK` rồi 3 dòng help `--single`, `--color`, `--hint`.

- [ ] **Step 8: Sửa `/regen` của server cho run 2 ảnh / combined (`studio/server.py`, hàm `regen`)**

Ngay sau dòng `pdir = rundir / f"person_{person}"` thêm:

```python
    if not (pdir / "pieces.json").is_file():   # run 2 ảnh / combined: pieces.json nằm thẳng trong run
        pdir = rundir
```

Run: `python3 -c "import ast,sys; ast.parse(open('server.py').read()); print('server.py OK')"`
Expected: `server.py OK`

- [ ] **Step 9: Commit**

```bash
cd /mnt/6C96C1A096C16AE2/vsc/CLOTHING_STUDIO
git add studio/flat_pipeline.py studio/server.py
git commit -m "feat(pipeline): --single/--color/--hint, mảnh lỗi không giết cả run; fix regen run 2 ảnh

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: judge.py — contact sheet + claude -p

**Files:**
- Create: `studio/judge.py`

**Interfaces:**
- Consumes: `batch_errors.StepError`, `batch_errors.raise_for`.
- Produces:
  - `QA_CODES: list[str]` (9 mã lỗi của spec).
  - `grid(items: list[tuple[Path,str]], cols: int|None=None) -> list[list[tuple]]`
  - `contact_sheet(rows: list[list[tuple[Path,str]]], out: Path, tile=384, max_side=1600) -> Path`
  - `ask(prompt: str, images: list[Path], timeout=300) -> dict` (QuotaError/AuthError/StepError('judge'))
  - `norm_pick(js: dict, rows: list[dict], n: int) -> dict` → `{"skip": str}` hoặc `{"skip": None, "front": int, "back": int|None, "mode": "infer"|"two"|"combined", "crop": list|None, "colors": [{"color": str, "matches_photo": bool, "record_ids": [str]}]}` (mọi record_id có đúng 1 lần).
  - `norm_qa(js: dict, names: list[str]) -> dict[str, {"ok": bool, "score": float, "errors": list[str]}]`
  - `pick(sheet: Path, rows: list[dict], n: int) -> dict` (= `norm_pick(ask(...))`)
  - `qa(sheet: Path, names: list[str], colour: str, n_refs: int) -> dict` (= `norm_qa(ask(...))`)
  - `rehint(sheet: Path, fails: dict[str, list[str]], n_src: int) -> dict` → `{"hint": str, "front": int|None, "crop": list|None}`

- [ ] **Step 1: Viết selfcheck trước**

Tạo `studio/judge.py`:

```python
"""Claude nhìn ảnh qua `claude -p` (headless, ngữ cảnh tối thiểu ~4k token/lần).

Luôn ghép nhiều ảnh thành 1 contact sheet trước khi gửi (tiết kiệm token).
"""
import json, math, os, re, subprocess, tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from batch_errors import StepError, raise_for


def _selfcheck():
    td = Path(tempfile.mkdtemp())
    items = []
    for i, c in enumerate(["red", "blue", "green", "black", "white"], 1):
        p = td / f"{i}.png"; Image.new("RGB", (900, 1200), c).save(p); items.append((p, str(i)))
    items.append((td / "missing.png", "6"))          # ảnh hỏng không làm sập sheet
    rows = grid(items)
    assert [len(r) for r in rows] == [3, 3]
    out = contact_sheet(rows, td / "s.jpg")
    assert max(Image.open(out).size) <= 1600
    assert _extract_json('bla {"a": 1} bla') == {"a": 1}
    rows_ = [{"record_id": "r1", "sku": "X-Black-Hoodie-L-G1", "color": ""},
             {"record_id": "r2", "sku": "X-Pink-Hoodie-S-G1", "color": ""},
             {"record_id": "r3", "sku": "X-Hoodie-M-G1", "color": ""}]
    assert norm_pick({"skip": "Magnet Die Cut"}, rows_, 5) == {"skip": "Magnet Die Cut"}
    p = norm_pick({"skip": None, "front": 2, "back": 2, "mode": "two", "crop": [10, 20, 900, 990],
                   "colors": [{"color": "black", "matches_photo": True, "record_ids": ["r1", "zz"]},
                              {"color": "pink", "matches_photo": False, "record_ids": ["r2", "r1"]}]},
                  rows_, 5)
    assert p["front"] == 2 and p["back"] is None and p["mode"] == "infer"   # back trùng front -> bỏ
    assert p["crop"] == [10, 20, 900, 990]
    ids = [x for c in p["colors"] for x in c["record_ids"]]
    assert sorted(ids) == ["r1", "r2", "r3"] and len(ids) == 3             # r3 bị sót -> vào nhóm 'photo'
    assert p["colors"][1] == {"color": "pink", "matches_photo": False, "record_ids": ["r2"]}
    try:
        norm_pick({"front": 9, "colors": []}, rows_, 5); raise AssertionError("front sai phải lỗi")
    except StepError as e:
        assert e.kind == "judge"
    q = norm_qa({"pieces": {"ao_truoc": {"ok": True, "score": 9, "errors": []},
                            "ao_sau": {"ok": True, "score": 7, "errors": ["sau_giong_truoc", "xyz"]}}},
                ["ao_truoc", "ao_sau", "art"])
    assert q["ao_truoc"] == {"ok": True, "score": 9.0, "errors": []}
    assert q["ao_sau"] == {"ok": False, "score": 7.0, "errors": ["sau_giong_truoc"]}
    assert q["art"]["ok"] is False and q["art"]["score"] == 0.0
    print("judge selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
```

- [ ] **Step 2: Chạy, xác nhận FAIL**

Run: `python3 judge.py`
Expected: `NameError: name 'grid' is not defined`

- [ ] **Step 3: Cài đặt (chèn sau dòng `from batch_errors import …`, trước `def _selfcheck`)**

```python
MODEL = os.getenv("FLAT_JUDGE_MODEL", "sonnet")
TILE, MAX_SIDE, LABEL_H = 384, 1600, 28
SYS = ("You are a meticulous garment-print QA assistant. Use the Read tool to view the image "
       "file(s) you are given, then answer with ONE JSON object only — no prose.")
QA_CODES = ["mo_nhoe", "sai_mau", "sau_giong_truoc", "sai_hoa_tiet", "sai_manh",
            "khong_tran", "vung_den", "cat_loi", "art_thieu"]


# ---------- contact sheet ----------
def grid(items, cols=None):
    """Chia list (path, nhãn) thành các hàng gần vuông."""
    cols = cols or max(1, math.ceil(math.sqrt(len(items))))
    return [items[i:i + cols] for i in range(0, len(items), cols)]


def contact_sheet(rows, out, tile=TILE, max_side=MAX_SIDE):
    """rows = [[(path, nhãn), ...], ...] -> 1 ảnh lưới có nhãn, cạnh dài ≤ max_side."""
    ncol = max(len(r) for r in rows)
    sheet = Image.new("RGB", (ncol * tile, len(rows) * (tile + LABEL_H)), "white")
    d = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=20)
    for y, row in enumerate(rows):
        for x, (path, label) in enumerate(row):
            ox, oy = x * tile, y * (tile + LABEL_H)
            try:
                im = ImageOps.contain(Image.open(path).convert("RGBA"), (tile - 8, tile - 8))
                bg = Image.new("RGB", im.size, (200, 200, 200))     # nền xám: thấy rõ PNG trong suốt
                bg.paste(im, mask=im.split()[-1])
                sheet.paste(bg, (ox + (tile - im.width) // 2, oy + LABEL_H + (tile - im.height) // 2))
            except Exception:
                d.text((ox + 10, oy + LABEL_H + 10), "(bad image)", fill="red", font=font)
            d.rectangle([ox, oy, ox + tile - 1, oy + LABEL_H - 1], fill=(30, 30, 30))
            d.text((ox + 6, oy + 3), str(label)[:32], fill="white", font=font)
    if max(sheet.size) > max_side:
        sheet.thumbnail((max_side, max_side))
    out = Path(out); out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=88)
    return out


# ---------- claude -p ----------
def _extract_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError(f"không có JSON: {(text or '')[:120]!r}")
    return json.loads(m.group())


def ask(prompt, images, timeout=300):
    """prompt + ảnh -> dict JSON. Hỏi lại 1 lần nếu JSON hỏng/quá giờ; vẫn hỏng -> StepError('judge')."""
    paths = "\n".join(f"- {Path(p).resolve()}" for p in images)
    full = f"View these image file(s) with the Read tool:\n{paths}\n\n{prompt}"
    cmd = ["claude", "-p", full, "--model", MODEL, "--tools", "Read", "--allowedTools", "Read",
           "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "",
           "--no-session-persistence", "--system-prompt", SYS, "--output-format", "json"]
    last = ""
    for _ in range(2):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                               errors="replace", cwd=str(Path(images[0]).resolve().parent))
        except subprocess.TimeoutExpired:
            last = f"claude -p quá {timeout}s"
            continue
        try:
            env = json.loads(p.stdout)
        except ValueError:
            raise_for((p.stdout or "") + (p.stderr or ""), "judge")
        res = env.get("result") or ""
        if env.get("is_error"):
            raise_for(res + (p.stderr or ""), "judge")
        try:
            return _extract_json(res)
        except ValueError as e:
            last = str(e)
    raise StepError("judge", last)


# ---------- chuẩn hoá JSON trả về ----------
def _box(b):
    ok = (isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b)
          and all(0 <= v <= 1000 for v in b) and b[2] > b[0] and b[3] > b[1])
    return [int(v) for v in b] if ok else None


def norm_pick(js, rows, n):
    if js.get("skip"):
        return {"skip": str(js["skip"])[:150]}
    front = js.get("front")
    if not isinstance(front, int) or not 1 <= front <= n:
        raise StepError("judge", f"front không hợp lệ: {front!r}")
    back = js.get("back")
    back = back if isinstance(back, int) and 1 <= back <= n and back != front else None
    mode = js.get("mode") if js.get("mode") in ("infer", "two", "combined") else "infer"
    if mode == "two" and back is None:
        mode = "infer"
    if mode != "two":
        back = None
    ids = [r["record_id"] for r in rows]
    seen, colors = set(), []
    for c in js.get("colors") or []:
        rids = [x for x in (c.get("record_ids") or []) if x in ids and x not in seen]
        if not rids:
            continue
        seen |= set(rids)
        color = str(c.get("color") or "photo")
        colors.append({"color": color, "record_ids": rids,
                       "matches_photo": bool(c.get("matches_photo", True)) or color.lower() == "photo"})
    rest = [x for x in ids if x not in seen]
    if rest:                                   # Claude sót dòng -> coi như màu ảnh gốc
        photo = next((c for c in colors if c["matches_photo"]), None)
        if photo:
            photo["record_ids"] += rest
        else:
            colors.append({"color": "photo", "matches_photo": True, "record_ids": rest})
    return {"skip": None, "front": front, "back": back, "mode": mode,
            "crop": _box(js.get("crop")), "colors": colors}


def norm_qa(js, names):
    got = js.get("pieces") if isinstance(js, dict) and isinstance(js.get("pieces"), dict) else {}
    out = {}
    for n in names:
        r = got.get(n) if isinstance(got.get(n), dict) else {}
        errs = [e for e in (r.get("errors") or []) if e in QA_CODES]
        score = r.get("score") if isinstance(r.get("score"), (int, float)) else 0
        out[n] = {"ok": bool(r.get("ok")) and not errs, "score": float(score), "errors": errs}
    return out


# ---------- 3 câu hỏi ----------
PICK = """The contact sheet shows {n} product photos of ONE listing, labelled 1..{n}.
Order rows that use these photos (record_id | SKU Design | Color column):
{rows}

1. Is the product a GARMENT / APPAREL item (t-shirt, shirt, hoodie, dress, skirt, pants, jogger, jersey,
jumpsuit, pajamas, athletic set, kids clothing...)? If NOT (magnet, plaque, vase, mug, tumbler, decor,
cap, guest book, patch, accessory...) return {{"skip": "<short reason with the product type>"}}.
2. Otherwise choose inputs for a flat sewing-pattern generator:
 - "front": label of the clearest photo of the FRONT of the whole garment (sharp, uncropped, least
   occluded; prefer real worn/flat photos over size charts, text banners or close-ups).
 - "back": label of a photo of the real BACK of the SAME garment, or null.
 - "mode": "two" if back is a separate photo; "combined" if the chosen front photo itself shows the
   front and back side by side (then back = null); otherwise "infer".
 - "crop": [x0,y0,x1,y1] integers 0-1000 of the front photo, tight around ONE outfit when the photo
   has several people/garments or lots of empty space; else null.
 - "colors": split the order rows by the garment colour each row asks for. Read colour words in
   SKU Design / Color; personal names, sizes and design numbers are NOT colours; a row without a colour
   uses the photo colour. Each item {{"color": "<colour words, or 'photo'>", "matches_photo": true|false,
   "record_ids": [...]}}; every record_id exactly once.
Return ONLY: {{"skip": null, "front": 1, "back": null, "mode": "infer", "crop": null, "colors": [...]}}"""

QA = """Top row = {n_refs} REFERENCE photo(s) of the real garment. Other tiles = generated flat pieces,
label = piece name. Required base fabric colour: {colour}.
Pieces: {names}. Suffix _truoc = FRONT panel, _sau = BACK panel; ao = torso, quan = pants, tay = sleeve,
than = dress bodice, ta = dress skirt; "art" = one flat technical drawing of the whole outfit (front).
Judge every piece against the reference: ok (true only if usable as a production print file),
score 1-10, errors = codes from:
 mo_nhoe (blurry/smeared/noisy), sai_mau (base or print colour differs from reference / required colour),
 sau_giong_truoc (a BACK piece copies front-only graphics: chest print, logo, badge, zipper, buttons),
 sai_hoa_tiet (artwork missing/extra/misplaced vs reference), sai_manh (contains other parts: sleeves or
 hood on a torso piece, a person, hanger, mockup), khong_tran (torso/pants/sleeve piece not full-bleed:
 margins, border or garment outline visible), vung_den (plain areas filled black/void/glow),
 cat_loi (dress cut-out broken / missing corners), art_thieu (art misses front details or wrong
 top/bottom layout).
Return ONLY: {{"pieces": {{"<name>": {{"ok": true, "score": 9, "errors": []}}, ...}}}} covering every name."""

REHINT = """Generated pieces failed QA twice: {fails} (piece: error codes).
Contact sheet: source photos labelled 1..{n} of the garment, then the failed pieces (label = name).
Write ONE short FIX instruction in English (<= 40 words) for the image generator that prevents these
exact errors, e.g. "BACK torso panel: plain base fabric, NO chest graphic, NO zipper".
Also re-pick the input: "front" = label of the best FRONT photo (may differ from before) and optional
"crop" [x0,y0,x1,y1] integers 0-1000 tight around the single garment (or null).
Return ONLY: {{"hint": "...", "front": 1, "crop": null}}"""


def pick(sheet, rows, n):
    lines = "\n".join(f"{r['record_id']} | {r['sku']} | {r['color'] or '-'}" for r in rows)
    return norm_pick(ask(PICK.format(n=n, rows=lines), [sheet]), rows, n)


def qa(sheet, names, colour, n_refs):
    return norm_qa(ask(QA.format(n_refs=n_refs, colour=colour, names=", ".join(names)), [sheet]), names)


def rehint(sheet, fails, n_src):
    txt = "; ".join(f"{k}: {','.join(v) or '?'}" for k, v in fails.items())
    js = ask(REHINT.format(fails=txt, n=n_src), [sheet])
    front = js.get("front") if isinstance(js.get("front"), int) and 1 <= js["front"] <= n_src else None
    return {"hint": str(js.get("hint") or "")[:400], "front": front, "crop": _box(js.get("crop"))}
```

- [ ] **Step 4: Chạy selfcheck, xác nhận PASS**

Run: `python3 judge.py`
Expected: `judge selfcheck OK`

- [ ] **Step 5: Smoke gọi Claude thật (1 lần, ~$0.01)**

Run:
```bash
python3 -c "
from pathlib import Path; import tempfile, judge
from PIL import Image, ImageDraw
d = Path(tempfile.mkdtemp()); p = d/'t.png'
im = Image.new('RGB',(300,300),'white'); ImageDraw.Draw(im).rectangle([80,80,220,220], fill='red'); im.save(p)
s = judge.contact_sheet(judge.grid([(p,'1')]), d/'s.jpg')
print(judge.ask('What shape and colour is in tile 1? Return {\"shape\":..., \"color\":...}', [s]))"
```
Expected: dạng `{'shape': 'square', 'color': 'red'}`.

- [ ] **Step 6: Commit**

```bash
cd /mnt/6C96C1A096C16AE2/vsc/CLOTHING_STUDIO
git add studio/judge.py
git commit -m "feat(batch): judge — contact sheet + claude -p chọn ảnh/QA/hint

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: batch.py — phần logic thuần (hàng đợi, tên file, ghi chú, dừng, máy gen lại)

**Files:**
- Create: `studio/batch.py`

**Interfaces:**
- Consumes: không (thuần).
- Produces (Task 6 dùng):
  - Hằng `MAX_ATTEMPTS = 4`, `SAME_ERR_STOP = 5`.
  - `sku_stem(sku) -> str`, `safe_name(s, maxlen=120) -> str`, `piece_filename(sku, slug, failed=False) -> str`.
  - `needs_work(row) -> bool`, `build_queue(rows) -> list[{"links": list[str], "rows": list[dict], "newest": str}]` (mới nhất trước).
  - `group_id(g) -> str`.
  - `note_ok(day, n_pieces, regen: dict) -> str`, `note_fail(day, fails: dict) -> str`.
  - `class Stopper(until: str|None, limit: int|None, now=datetime.now)` với `.record(kind)` (kind: `None` = gen xong, `"skip"`, hoặc chuỗi lỗi) và `.reason() -> str|None`.
  - `improve(names, qa_fn, regen_fn, rehint_fn, keep_fn) -> (best: dict[name,(attempt:int, score:float)], fails: dict[name,list[str]], regen: dict[name,int])`.

- [ ] **Step 1: Viết selfcheck trước**

Tạo `studio/batch.py`:

```python
#!/usr/bin/env python3
"""Flat Studio batch — chạy qua đêm, không giao diện.

  python batch.py [--until 07:00] [--limit N] [--dry-run]
  python batch.py --selfcheck

Lark Base (view 1. DS Order) -> dòng No File chưa có Flat Pieces -> tải Link 1..N -> Claude chọn ảnh
-> flat_pipeline (pieces 3D + art) -> Claude QA, gen lại -> up Flat Pieces + ghi Flat Note.
Spec: docs/superpowers/specs/2026-10-15-batch-lark-flat-design.md
"""
import argparse, hashlib, io, json, os, re, shutil, subprocess, sys, time, traceback, urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path


def _selfcheck():
    # tên file: bỏ mã lô + size; ký tự cấm -> _
    assert sku_stem("HKH032604A21-Black-2D Hoodie-L-G130426") == "HKH032604A21-Black-2D Hoodie"
    assert sku_stem("VTY022506A07-Christoph Stelzer1-Button Shirt-2XL-J150426") == \
        "VTY022506A07-Christoph Stelzer1-Button Shirt"
    assert sku_stem("VPC132605A14-Austin 080626-Car visor clip-AP8626") == \
        "VPC132605A14-Austin 080626-Car visor clip"
    assert sku_stem("X") == "X"
    assert piece_filename("A/B-Black-Tee-M-G1", "tay_sau", failed=True) == "A_B-Black-Tee_tay_sau_QAFAIL.png"
    assert piece_filename("A-Tee-S-G1", "art") == "A-Tee_art.png"
    # hàng đợi: lọc, gom theo bộ link, mới nhất trước
    def row(rid, links, created, **kw):
        return {"record_id": rid, "sku": f"S-{rid}-Tee-M-G1", "color": "", "created": created,
                "check_file": kw.get("cf", ["No File"]), "links": links,
                "has_pieces": kw.get("hp", False), "pieces": [], "note": kw.get("note", "")}
    rows = [row("a", ["u1"], "2026-04-01T00:00:00.000+07:00"),
            row("b", ["u1"], "2026-04-03T00:00:00.000+07:00"),
            row("c", ["u2"], "2026-04-02T00:00:00.000+07:00"),
            row("d", ["u3"], "2026-04-09T00:00:00.000+07:00", hp=True),
            row("e", ["u4"], "2026-04-09T00:00:00.000+07:00", note="SKIP: mug"),
            row("f", ["u5"], "2026-04-09T00:00:00.000+07:00", cf=["Urgent"]),
            row("g", [], "2026-04-09T00:00:00.000+07:00"),
            row("h", ["u6"], "2026-04-09T00:00:00.000+07:00", note="LỖI 2026-10-15: tải ảnh")]
    q = build_queue(rows)
    assert [[r["record_id"] for r in g["rows"]] for g in q] == [["h"], ["a", "b"], ["c"]]
    assert q[1]["newest"] == "2026-04-03T00:00:00.000+07:00"
    assert group_id(q[1]).startswith("S-a-Tee_") and len(group_id(q[1])) <= 80
    # ghi chú
    assert note_ok("2026-10-16", 6, {"tay_sau": 1}) == "OK 2026-10-16 · 6 mảnh + art · gen lại: tay_sau×1"
    assert note_ok("2026-10-16", 4, {}) == "OK 2026-10-16 · 4 mảnh + art"
    assert note_fail("2026-10-16", {"tay_sau": ["sau_giong_truoc"]}) == \
        "QA FAIL 2026-10-16 · tay_sau (sau_giong_truoc) sau 4 lần"
    # dừng: limit đếm nhóm đã gen (skip không tính); 5 lỗi liên tiếp cùng kiểu
    s = Stopper(None, 2)
    s.record("skip"); assert s.reason() is None
    s.record(None); s.record(None); assert s.reason() == "đủ --limit 2"
    s = Stopper(None, None)
    for _ in range(4):
        s.record("tải ảnh")
    assert s.reason() is None
    s.record("skip"); s.record("tải ảnh"); assert s.reason() == "5 nhóm liên tiếp lỗi 'tải ảnh'"
    s = Stopper(None, None)
    for k in ["gen", "gen", "judge", "gen", "gen", None, "gen"]:
        s.record(k)
    assert s.reason() is None
    now = [datetime(2026, 10, 15, 23, 0)]
    s = Stopper("07:00", None, now=lambda: now[0])
    assert s.reason() is None
    now[0] = datetime(2026, 10, 16, 7, 0); assert s.reason() == "hết giờ 07:00"
    # máy gen lại: A đạt ngay; B đạt ở lần 3 (sau hint); C lỗi cả 5 bản -> chọn bản điểm cao nhất
    script = {"A": [(True, 9)], "B": [(False, 3), (False, 4), (False, 2), (True, 8)],
              "C": [(False, 2), (False, 6), (False, 3), (False, 5), (False, 4)]}
    tries = Counter(); calls = {"regen": [], "hint": 0, "keep": []}
    def qa_fn(todo):
        out = {}
        for n in todo:
            ok, sc = script[n][tries[n]]; tries[n] += 1
            out[n] = {"ok": ok, "score": sc, "errors": [] if ok else ["mo_nhoe"]}
        return out
    def regen_fn(todo, attempt, hint):
        calls["regen"].append((tuple(todo), attempt, hint))
    def rehint_fn(fails):
        calls["hint"] += 1; assert set(fails) == {"B", "C"}; return "FIX IT"
    def keep_fn(n, attempt):
        calls["keep"].append((n, attempt))
    best, fails, regen = improve(["A", "B", "C"], qa_fn, regen_fn, rehint_fn, keep_fn)
    assert best == {"A": (0, 9), "B": (3, 8), "C": (1, 6)}
    assert fails == {"C": ["mo_nhoe"]} and regen == {"B": 3, "C": 4}
    assert calls["hint"] == 1
    assert calls["regen"] == [(("B", "C"), 1, None), (("B", "C"), 2, None), (("B", "C"), 3, "FIX IT"),
                              (("C",), 4, "FIX IT")]
    assert ("A", 0) in calls["keep"] and ("C", 4) in calls["keep"]
    print("batch selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
```

- [ ] **Step 2: Chạy, xác nhận FAIL**

Run: `python3 batch.py`
Expected: `NameError: name 'sku_stem' is not defined`

- [ ] **Step 3: Cài đặt (chèn sau các dòng import, trước `def _selfcheck`)**

```python
MAX_ATTEMPTS = 4         # gen lại: lần 1-2 y nguyên, lần 3-4 có hint + chọn lại ảnh/crop
SAME_ERR_STOP = 5        # 5 nhóm liên tiếp lỗi cùng kiểu -> dừng đợt
_SIZE = re.compile(r"^(XXS|XS|S|M|L|XL|XXL|XXXL|\d{1,2}XL|\d{1,2}T|\d{1,2}Y|\d{1,2}-\d{1,2}Y?"
                   r"|one ?size|free ?size|\d+(\.\d+)?\s*(x\s*\d+(\.\d+)?)?\s*(in|inch|cm)?)$", re.I)


# ---------- tên ----------
def sku_stem(sku):
    """SKU Design bỏ mã lô (đoạn cuối) và size (đoạn kế cuối nếu là size)."""
    parts = [p.strip() for p in (sku or "").split("-")]
    if len(parts) > 1:
        parts = parts[:-1]
    if len(parts) > 1 and _SIZE.match(parts[-1]):
        parts = parts[:-1]
    return "-".join(parts)


def safe_name(s, maxlen=120):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", s or "").strip(" ._")
    return s[:maxlen] or "item"


def piece_filename(sku, slug, failed=False):
    return f"{safe_name(sku_stem(sku))}_{slug}{'_QAFAIL' if failed else ''}.png"


# ---------- hàng đợi ----------
def needs_work(row):
    return ("No File" in row["check_file"] and not row["has_pieces"] and bool(row["links"])
            and not row["note"].upper().startswith("SKIP"))


def build_queue(rows):
    """Gom dòng cần làm theo bộ link; nhóm có dòng mới nhất lên trước."""
    groups = {}
    for r in rows:
        if needs_work(r):
            groups.setdefault(tuple(r["links"]), []).append(r)
    out = [{"links": list(k), "rows": v, "newest": max(x["created"] for x in v)} for k, v in groups.items()]
    out.sort(key=lambda g: g["newest"], reverse=True)
    return out


def group_id(g):
    h = hashlib.sha1("|".join(g["links"]).encode()).hexdigest()[:6]
    return f"{safe_name(sku_stem(g['rows'][0]['sku']), 60)}_{h}"


# ---------- ghi chú ----------
def note_ok(day, n_pieces, regen):
    s = f"OK {day} · {n_pieces} mảnh + art"
    if regen:
        s += " · gen lại: " + ", ".join(f"{k}×{v}" for k, v in sorted(regen.items()))
    return s


def note_fail(day, fails):
    body = " · ".join(f"{k} ({','.join(v) or '?'})" for k, v in sorted(fails.items()))
    return f"QA FAIL {day} · {body} sau {MAX_ATTEMPTS} lần"


# ---------- điều kiện dừng ----------
class Stopper:
    """--until HH:MM, --limit N (đếm nhóm đã gen, SKIP không tính), N nhóm liên tiếp lỗi cùng kiểu."""

    def __init__(self, until=None, limit=None, now=datetime.now):
        self.limit, self.now, self.done = limit, now, 0
        self.err_kind, self.err_run, self.deadline = None, 0, None
        if until:
            h, m = map(int, until.split(":"))
            t = now().replace(hour=h, minute=m, second=0, microsecond=0)
            self.deadline = t if t > now() else t + timedelta(days=1)

    def record(self, kind):
        """kind: None = gen xong (OK/QA FAIL), 'skip', hoặc tên lỗi StepError."""
        if kind == "skip":
            return                              # SKIP: không tính limit, không cắt chuỗi lỗi
        if kind is None:
            self.done += 1
            self.err_kind, self.err_run = None, 0
        elif kind == self.err_kind:
            self.err_run += 1
        else:
            self.err_kind, self.err_run = kind, 1

    def reason(self):
        if self.deadline and self.now() >= self.deadline:
            return f"hết giờ {self.deadline:%H:%M}"
        if self.limit and self.done >= self.limit:
            return f"đủ --limit {self.limit}"
        if self.err_run >= SAME_ERR_STOP:
            return f"{self.err_run} nhóm liên tiếp lỗi '{self.err_kind}'"
        return None


# ---------- máy gen lại ----------
def improve(names, qa_fn, regen_fn, rehint_fn, keep_fn):
    """QA + gen lại mảnh lỗi tối đa MAX_ATTEMPTS lần.
      qa_fn(todo) -> {name: {ok, score, errors}}   chấm bản hiện tại của các mảnh
      regen_fn(todo, attempt, hint)                gen lại (attempt 1..4; hint từ lần 3)
      rehint_fn({name: errors}) -> hint            gọi 1 lần trước lần 3 (viết hint + chọn lại ảnh/crop)
      keep_fn(name, attempt)                       lưu bản hiện tại để chọn bản tốt nhất
    -> best {name: (attempt, score)}, fails {name: errors} (vẫn lỗi), regen {name: số lần gen lại}."""
    best, last, regen = {}, {}, {}
    todo, hint = list(names), None
    for attempt in range(MAX_ATTEMPTS + 1):
        if attempt:
            if attempt == 3:
                hint = rehint_fn({n: last[n]["errors"] for n in todo})
            regen_fn(list(todo), attempt, hint)
            for n in todo:
                regen[n] = attempt
        res = qa_fn(list(todo))
        nxt = []
        for n in todo:
            r = res.get(n) or {"ok": False, "score": 0, "errors": []}
            last[n] = r
            keep_fn(n, attempt)
            if r["ok"]:
                best[n] = (attempt, r["score"])         # bản đạt luôn được chọn
            else:
                if n not in best or r["score"] > best[n][1]:
                    best[n] = (attempt, r["score"])
                nxt.append(n)
        todo = nxt
        if not todo:
            break
    return best, {n: last[n]["errors"] for n in todo}, regen
```

- [ ] **Step 4: Chạy selfcheck, xác nhận PASS**

Run: `python3 batch.py`
Expected: `batch selfcheck OK`

- [ ] **Step 5: Commit**

```bash
cd /mnt/6C96C1A096C16AE2/vsc/CLOTHING_STUDIO
git add studio/batch.py
git commit -m "feat(batch): hàng đợi, tên file, ghi chú, điều kiện dừng, máy gen lại

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: batch.py — I/O: tải, pipeline, nhóm, up, dọn, lock, báo Lark, main

**Files:**
- Modify: `studio/batch.py` (thêm import nội bộ + các hàm I/O + `main`; đổi khối `__main__`)
- Modify: `studio/.env` (thêm `LARK_WEBHOOK`, KHÔNG commit — đã bị `.gitignore`)

**Interfaces:**
- Consumes: Task 1 (`QuotaError, AuthError, StepError, AUTH_RE, raise_for`), Task 2 (`lark_base.fetch_rows/get_row/upload_files/remove_files/write_notes`), Task 3 (CLI `--single/--color/--hint`, file `final_<slug>.png`, `pieces.json`, `art.png`), Task 4 (`judge.grid/contact_sheet/pick/qa/rehint`), Task 5 (mọi hàm thuần).
- Produces: CLI `python batch.py [--until HH:MM] [--limit N] [--dry-run] [--selfcheck]`; thư mục `flat_studio_data/batch/<YYYY-MM-DD>/` gồm `batch.log` + mỗi nhóm `<group_id>/` (sheet_pick.jpg, pick.json, `c<k>/` có sheet_qa_*.jpg, qa_*.json, pipe_*.log, `out/*.png`).

- [ ] **Step 1: Thêm assert I/O-cục-bộ vào `_selfcheck()` (ngay trước `print("batch selfcheck OK")`)**

```python
    # crop theo bbox 0-1000; bbox quá nhỏ/None -> giữ nguyên ảnh
    import tempfile
    from PIL import Image as _I
    td = Path(tempfile.mkdtemp()); src = td / "s.jpg"; _I.new("RGB", (1000, 500), "white").save(src)
    assert _I.open(make_input(src, [100, 200, 900, 800], td / "a.png")).size == (800, 300)
    assert _I.open(make_input(src, None, td / "b.png")).size == (1000, 500)
    assert _I.open(make_input(src, [100, 100, 101, 101], td / "c.png")).size == (1000, 500)
    # lock: PID đã chết -> tự gỡ; PID sống -> chặn
    lk = td / "x.lock"; lk.write_text("999999999")
    acquire_lock(lk); assert lk.read_text() == str(os.getpid())
    try:
        acquire_lock(lk); raise AssertionError("lock sống phải chặn")
    except SystemExit:
        pass
    release_lock(lk); assert not lk.exists()
```

- [ ] **Step 2: Chạy, xác nhận FAIL**

Run: `python3 batch.py --selfcheck` (hoặc `python3 batch.py` nếu `__main__` chưa đổi)
Expected: `NameError: name 'make_input' is not defined`

- [ ] **Step 3: Thêm import nội bộ (ngay sau `from pathlib import Path`)**

```python
from PIL import Image

import judge
import lark_base as lb
from batch_errors import AUTH_RE, AuthError, QuotaError, StepError, raise_for

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("STUDIO_DATA", ROOT.parent / "flat_studio_data")).expanduser()
BATCH = DATA / "batch"
LOCK = DATA / "batch.lock"
PIPELINE = ROOT / "flat_pipeline.py"
GROUP_TIMEOUT = 45 * 60
QUOTA_WAIT, QUOTA_TRIES = 30 * 60, 3
UA = {"User-Agent": "Mozilla/5.0"}
_LOGF = None
```

- [ ] **Step 4: Thêm các hàm I/O (chèn ngay trước `def _selfcheck`)**

```python
# ---------- log + báo Lark ----------
def log(stage, msg=""):
    line = f"[{datetime.now():%H:%M:%S}][{stage:<6}] {msg}"
    print(line, flush=True)
    if _LOGF:
        _LOGF.write(line + "\n"); _LOGF.flush()


def notify(text):
    """Gửi tin về bot Lark (LARK_WEBHOOK trong .env). Lỗi gửi chỉ log, không dừng đợt."""
    url = os.getenv("LARK_WEBHOOK")
    if not url:
        log("notify", "(chưa có LARK_WEBHOOK trong .env) " + text); return
    body = json.dumps({"msg_type": "text", "content": {"text": "🧵 Flat batch\n" + text}}).encode()
    try:
        req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        log("notify", f"gửi Lark lỗi: {e}")


# ---------- lock ----------
def _alive(pid):
    if sys.platform == "win32":     # os.kill(pid, 0) trên Windows là gửi Ctrl+C -> coi như còn sống
        return True
    try:
        os.kill(pid, 0); return True
    except OSError:
        return False


def acquire_lock(path=LOCK):
    if path.exists():
        try:
            pid = int(path.read_text().strip() or 0)
        except ValueError:
            pid = 0
        if pid and pid != os.getpid() and _alive(pid):
            sys.exit(f"Đang có batch khác chạy (PID {pid}). Chắc chắn không còn thì xoá {path}.")
        if pid == os.getpid():
            sys.exit(f"Lock {path} đã thuộc tiến trình này.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))


def release_lock(path=LOCK):
    path.unlink(missing_ok=True)


# ---------- ảnh đầu vào ----------
def download(url, dst, tries=3):
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                data = r.read(40 * 1024 * 1024)
            Image.open(io.BytesIO(data)).verify()
            dst.write_bytes(data)
            return dst
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise StepError("tải ảnh", f"{url[:80]}: {last}")


def make_input(src, crop, dst):
    """Ảnh đầu vào cho pipeline: crop theo bbox 0-1000 (bỏ qua nếu None hoặc vùng < 32px)."""
    im = Image.open(src).convert("RGB")
    if crop:
        w, h = im.size
        box = (crop[0] * w // 1000, crop[1] * h // 1000, crop[2] * w // 1000, crop[3] * h // 1000)
        if box[2] - box[0] >= 32 and box[3] - box[1] >= 32:
            im = im.crop(box)
    im.save(dst)
    return dst


# ---------- pipeline ----------
def run_pipe(args, logf, timeout):
    """Chạy flat_pipeline --single. Hết đăng nhập (trong log/stderr) -> AuthError; exit≠0 -> raise_for."""
    cmd = [sys.executable, str(PIPELINE), "--single", *map(str, args), "--log", str(logf)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=max(60, timeout), cwd=str(ROOT),
                           encoding="utf-8", errors="replace", env={**os.environ, "PYTHONUTF8": "1"})
    except subprocess.TimeoutExpired:
        raise StepError("quá thời gian", "pipeline")
    logtxt = logf.read_text(encoding="utf-8", errors="replace") if logf.exists() else ""
    if AUTH_RE.search(logtxt + (p.stderr or "")):
        raise AuthError("codex/ARK hết đăng nhập — xem " + str(logf))
    if p.returncode != 0:
        raise_for((p.stderr or "")[-2000:], "gen")
    return logtxt


def _gen_soft(fn):
    """Gen lỗi (StepError 'gen') -> chỉ log, để QA chấm thiếu file; lỗi khác ném tiếp."""
    try:
        fn()
    except StepError as e:
        if e.kind != "gen":
            raise
        log("gen", f"lỗi gen (tính 1 lần hỏng): {str(e)[:200]}")


# ---------- 1 nhóm con màu ----------
def process_subgroup(sub, rows, pick, srcs, sdir, day, dry, deadline):
    """Gen pieces 3D + art cho 1 nhóm con màu -> QA/gen lại -> up. Trả True nếu còn mảnh QA FAIL."""
    sdir.mkdir(parents=True, exist_ok=True)
    color = None if sub["matches_photo"] else sub["color"]
    colour_desc = color or "same as the reference photo"
    ctx = {"front": make_input(srcs[pick["front"] - 1], pick["crop"], sdir / "in_front.png"),
           "back": (make_input(srcs[pick["back"] - 1], None, sdir / "in_back.png")
                    if pick["mode"] == "two" else None),
           "hint": None}
    n = Counter()

    def pipe(args):
        left = deadline - time.monotonic()
        if left <= 0:
            raise StepError("quá thời gian", "nhóm > 45 phút")
        n["pipe"] += 1
        extra = (["--color", color] if color else []) + (["--hint", ctx["hint"]] if ctx["hint"] else [])
        return run_pipe(args + extra, sdir / f"pipe_{n['pipe']:02d}.log", left)

    def mode_args():
        return ((["--back", ctx["back"]] if ctx["back"] else [])
                + (["--combined"] if pick["mode"] == "combined" else []))

    def piece_path(name):
        return sdir / ("art.png" if name == "art" else f"final_{name}.png")

    def gen_pieces(only=None, cache=True):
        args = ["--pieces", "--front", ctx["front"], *mode_args(), "-o", sdir / "final.png", "--emit", sdir]
        if only:
            args += ["--only", ",".join(only)]
        if cache and only:
            args += ["--cache", sdir / "pieces.json"]
        _gen_soft(lambda: pipe(args))

    def gen_art():
        _gen_soft(lambda: pipe(["--art", "--front", ctx["front"], "-o", sdir / "art.png",
                                "--emit", sdir / "art_work"]))

    # lần 0
    gen_pieces()
    gen_art()
    names = sorted(p.stem[len("final_"):] for p in sdir.glob("final_*.png"))
    if not names:
        raise StepError("gen", "không ra mảnh nào")
    names.append("art")                     # art lỗi lần 0 -> QA chấm 0 -> được gen lại

    def qa_fn(todo):
        n["qa"] += 1
        have = [x for x in todo if piece_path(x).exists()]
        res = {x: {"ok": False, "score": 0.0, "errors": ["gen_loi"]} for x in todo if x not in have}
        if have:
            refs = [(ctx["front"], "REF front")] + ([(ctx["back"], "REF back")] if ctx["back"] else [])
            sheet = judge.contact_sheet([refs] + judge.grid([(piece_path(x), x) for x in have], cols=4),
                                        sdir / f"sheet_qa_{n['qa']}.jpg")
            res.update(judge.qa(sheet, have, colour_desc, len(refs)))
        (sdir / f"qa_{n['qa']}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        log("qa", " ".join(f"{k}:{'✓' if v['ok'] else '✗'}{v['score']:.0f}" for k, v in res.items()))
        return res

    def regen_fn(todo, attempt, hint):
        log("regen", f"lần {attempt}: {', '.join(todo)}" + (" (hint)" if hint else ""))
        for x in todo:
            piece_path(x).unlink(missing_ok=True)
        slugs = [x for x in todo if x != "art"]
        if slugs:
            gen_pieces(only=slugs, cache=attempt <= 2)     # lần 3-4: vision lại (+ hint, ảnh/crop mới)
        if "art" in todo:
            gen_art()

    def rehint_fn(fails):
        items = [(p, str(i)) for i, p in enumerate(srcs, 1)]
        items += [(piece_path(x), x) for x in fails if piece_path(x).exists()]
        sheet = judge.contact_sheet(judge.grid(items), sdir / "sheet_rehint.jpg")
        r = judge.rehint(sheet, fails, len(srcs))
        (sdir / "rehint.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
        ctx["front"] = make_input(srcs[(r["front"] or pick["front"]) - 1], r["crop"], sdir / "in_front_r3.png")
        ctx["hint"] = r["hint"] or None
        log("regen", f"hint: {ctx['hint']}")
        return ctx["hint"]

    def keep_fn(name, attempt):
        src = piece_path(name)
        if src.exists():
            (sdir / "attempts").mkdir(exist_ok=True)
            shutil.copyfile(src, sdir / "attempts" / f"{name}_{attempt}.png")

    best, fails, regen = improve(names, qa_fn, regen_fn, rehint_fn, keep_fn)
    out = sdir / "out"; out.mkdir(exist_ok=True)
    files = []
    for name in names:
        src = sdir / "attempts" / f"{name}_{best[name][0]}.png"
        if src.exists():                    # mảnh chưa từng ra file thì không có gì để up
            dst = out / piece_filename(rows[0]["sku"], name, failed=name in fails)
            shutil.copyfile(src, dst); files.append(dst)
    if not files:
        raise StepError("gen", "không có mảnh nào để up")
    n_pieces = sum(1 for x in names if x != "art")
    note = note_fail(day, fails) if fails else note_ok(day, n_pieces, regen)
    upload_rows(rows, files, note, dry)
    return bool(fails)


def upload_rows(rows, files, note, dry):
    """Up cùng bộ file cho từng dòng; dòng đã có Flat Pieces -> bỏ qua. Ghi Note sau khi up xong."""
    if dry:
        log("dry", f"[không ghi Base] {len(rows)} dòng · {len(files)} file · note: {note}")
        return
    names = {f.name for f in files}
    done = {}
    for r in rows:
        rid = r["record_id"]
        if lb.get_row(rid)["has_pieces"]:
            log("up", f"{rid} đã có Flat Pieces -> bỏ qua"); continue
        try:
            lb.upload_files(rid, files)
        except StepError as e:
            toks = [p["file_token"] for p in lb.get_row(rid)["pieces"] if p.get("name") in names]
            if toks:
                lb.remove_files(rid, toks)
            if done:
                lb.write_notes(done)
            raise StepError("up", f"{rid}: {e}")
        done[rid] = note
        log("up", f"{rid} ✓ {len(files)} file")
    if done:
        lb.write_notes(done)


# ---------- 1 nhóm ----------
def cleanup(gdir):
    """Sau khi up xong: xoá ảnh tải về + bản trung gian; giữ out/, contact sheet, json, log."""
    for p in list(gdir.glob("src_*")) + list(gdir.glob("c*/attempts")) + list(gdir.glob("c*/art_work")):
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    for p in gdir.glob("c*/*.png"):
        p.unlink(missing_ok=True)


def process_group(g, rundir, day, dry, stats):
    """-> None (gen xong), 'skip', hoặc kind của StepError. QuotaError/AuthError ném tiếp lên main."""
    gid = group_id(g)
    gdir = rundir / gid; gdir.mkdir(parents=True, exist_ok=True)
    rows, deadline, done_ids = g["rows"], time.monotonic() + GROUP_TIMEOUT, set()
    log("group", f"=== {gid} · {len(rows)} dòng · {len(g['links'])} link · {g['newest'][:10]} ===")
    try:
        srcs = []
        for i, u in enumerate(g["links"], 1):
            try:
                srcs.append(download(u, gdir / f"src_{i:02d}.jpg"))
            except StepError as e:
                log("tải", str(e))
        if not srcs:
            raise StepError("tải ảnh", "mọi link đều lỗi")
        sheet = judge.contact_sheet(judge.grid([(p, str(i)) for i, p in enumerate(srcs, 1)]),
                                    gdir / "sheet_pick.jpg")
        pick = judge.pick(sheet, rows, len(srcs))
        (gdir / "pick.json").write_text(json.dumps(pick, ensure_ascii=False, indent=1), encoding="utf-8")
        if pick["skip"]:
            note = f"SKIP: {pick['skip']}"
            log("skip", note)
            if not dry:
                lb.write_notes({r["record_id"]: note for r in rows})
            stats["skip"] += 1
            return "skip"
        log("pick", f"front={pick['front']} back={pick['back']} mode={pick['mode']} "
                    f"màu={[c['color'] for c in pick['colors']]}")
        any_fail = False
        for k, sub in enumerate(pick["colors"], 1):
            sub_rows = [r for r in rows if r["record_id"] in sub["record_ids"]]
            any_fail |= process_subgroup(sub, sub_rows, pick, srcs, gdir / f"c{k}", day, dry, deadline)
            done_ids |= {r["record_id"] for r in sub_rows}
        stats["qafail" if any_fail else "ok"] += 1
        if not dry:
            cleanup(gdir)
        return None
    except (QuotaError, AuthError):
        raise
    except Exception as e:                 # StepError, hoặc lỗi lạ (ảnh hỏng, PIL...) -> chỉ bỏ nhóm này
        kind = e.kind if isinstance(e, StepError) else "lỗi khác"
        log("lỗi", f"{gid}: {e}" + ("" if isinstance(e, StepError) else "\n" + traceback.format_exc()))
        stats["loi"] += 1
        left = {r["record_id"]: f"LỖI {day}: {kind}" for r in rows if r["record_id"] not in done_ids}
        if left and not dry:
            try:
                lb.write_notes(left)
            except StepError as e2:
                log("lỗi", f"ghi note LỖI thất bại: {e2}")
        return kind


# ---------- main ----------
def main():
    global _LOGF
    ap = argparse.ArgumentParser(description="Flat batch: Lark Base -> mảnh rập, chạy qua đêm")
    ap.add_argument("--until", help="dừng sau giờ HH:MM (làm nốt nhóm đang chạy)")
    ap.add_argument("--limit", type=int, help="dừng sau N nhóm đã gen (SKIP không tính)")
    ap.add_argument("--dry-run", action="store_true", help="làm đủ trừ ghi Lark Base")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        import batch_errors
        batch_errors._selfcheck(); lb._selfcheck(); judge._selfcheck(); _selfcheck()
        return
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    acquire_lock()
    day = date.today().isoformat()
    rundir = BATCH / day; rundir.mkdir(parents=True, exist_ok=True)
    _LOGF = open(rundir / "batch.log", "a", encoding="utf-8")
    stats, stop, reason = Counter(), Stopper(a.until, a.limit), "hết việc"
    log("start", f"until={a.until} limit={a.limit} dry={a.dry_run}")
    try:
        queue = build_queue(lb.fetch_rows())
        log("queue", f"{len(queue)} nhóm cần làm")
        i, waits = 0, 0
        while i < len(queue):
            try:
                kind = process_group(queue[i], rundir, day, a.dry_run, stats)
                waits = 0
            except QuotaError as e:
                waits += 1
                if waits > QUOTA_TRIES:
                    reason = f"hết hạn mức sau {QUOTA_TRIES} lần chờ ({str(e)[:120]})"; break
                notify(f"⏸ Hết hạn mức — chờ 30' rồi thử lại (lần {waits}/{QUOTA_TRIES})\n{str(e)[:200]}")
                log("quota", f"chờ {QUOTA_WAIT // 60}' (lần {waits}/{QUOTA_TRIES}): {e}")
                time.sleep(QUOTA_WAIT)
                continue                       # làm lại nhóm này
            i += 1
            stop.record(kind)
            r = stop.reason()
            if r:
                reason = r; break
    except AuthError as e:
        reason = f"hết đăng nhập: {str(e)[:200]}"
    except KeyboardInterrupt:
        reason = "Ctrl+C"
    except Exception as e:
        reason = f"lỗi không lường: {e}"
        log("crash", traceback.format_exc())
    finally:
        summary = (f"Đợt {day}{' (dry-run)' if a.dry_run else ''}: {stats['ok']} OK · {stats['qafail']} QA FAIL"
                   f" · {stats['skip']} SKIP · {stats['loi']} LỖI · dừng vì: {reason}"
                   f" · log: {rundir / 'batch.log'}")
        log("done", summary)
        notify(summary)
        release_lock()
```

- [ ] **Step 5: Đổi khối `__main__` cuối file**

```python
if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Chạy selfcheck toàn bộ, xác nhận PASS**

Run: `python3 batch.py --selfcheck && python3 flat_pipeline.py --selfcheck`
Expected:
```
batch_errors selfcheck OK
lark_base selfcheck OK
judge selfcheck OK
batch selfcheck OK
selfcheck OK
```

- [ ] **Step 7: Thêm webhook vào `studio/.env` (không commit) và thử gửi**

Lấy URL webhook từ hàm `larkdone` trong `~/.claude/CLAUDE.md` của người dùng, thêm 1 dòng vào `studio/.env`:
```
LARK_WEBHOOK=https://open.larksuite.com/open-apis/bot/v2/hook/<id trong larkdone>
```
Run: `python3 -c "from dotenv import load_dotenv; load_dotenv('.env'); import batch; batch.notify('thử báo từ batch.py')"`
Expected: không lỗi; tin "🧵 Flat batch / thử báo từ batch.py" tới bot Lark. Kiểm `git status --short` KHÔNG có `.env`.

- [ ] **Step 8: Commit**

```bash
cd /mnt/6C96C1A096C16AE2/vsc/CLOTHING_STUDIO
git add studio/batch.py
git commit -m "feat(batch): vòng lặp đêm — tải, gen, QA/gen lại, up, dừng, báo Lark

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Chạy thử thật cùng người dùng + Codex review

**Files:** không sửa code (chỉ sửa nếu chạy thử lộ lỗi — khi đó quay lại task tương ứng, thêm assert tái hiện lỗi trước khi sửa).

**Interfaces:**
- Consumes: CLI `batch.py` của Task 6.

- [ ] **Step 1: Dry-run 3 nhóm (không ghi Base)**

Run: `cd /mnt/6C96C1A096C16AE2/vsc/CLOTHING_STUDIO/studio && python3 batch.py --dry-run --limit 3`
Expected: log có `queue … nhóm cần làm`, vài nhóm `skip` (không phải quần áo), 3 nhóm qua `pick` → `qa` → `[không ghi Base]`; kết thúc `dừng vì: đủ --limit 3`; tin tóm tắt tới Lark.

- [ ] **Step 2: Cùng người dùng xem kết quả**

Mở `flat_studio_data/batch/<hôm nay>/<group_id>/`: `sheet_pick.jpg` + `pick.json` (chọn ảnh/màu đúng chưa), `c*/sheet_qa_*.jpg` + `qa_*.json` (QA có hợp lý không), `c*/out/*.png` (chất lượng mảnh). DỪNG, hỏi người dùng duyệt trước khi ghi thật. Sửa prompt `judge.py` nếu QA chấm sai — chạy lại Step 1.

- [ ] **Step 3: Ghi thật 3 nhóm**

Run: `python3 batch.py --limit 3`
Expected: log `up rec… ✓ N file` + tóm tắt. Người dùng mở Base kiểm `Flat Pieces` (đúng file, đúng dòng) và `Flat Note`. Chạy lại `python3 batch.py --limit 3 --dry-run` → 3 nhóm vừa làm KHÔNG còn trong hàng đợi.

- [ ] **Step 4: Codex review (theo CLAUDE.md người dùng)**

Chạy `/codex:review --background --base 20f8032`, đọc `/codex:result`, tự kiểm chứng từng góp ý, sửa cái đúng (kèm assert tái hiện), commit.

- [ ] **Step 5: Bàn giao đêm đầu**

Hướng dẫn người dùng: `cd …/studio && python3 batch.py --until 07:00 --limit 30` (để terminal mở, hoặc `nohup python3 batch.py --until 07:00 --limit 30 >/dev/null 2>&1 &`). Sáng xem tin Lark + `flat_studio_data/batch/<ngày>/batch.log`.
