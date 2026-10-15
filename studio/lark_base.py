"""Đọc/ghi Lark Base bằng lark-cli (profile tool_ds, user token — không dùng bot).

Chỉ GHI 2 cột: Flat Pieces (đính kèm) và Flat Note (text).
Spec: docs/superpowers/specs/2026-10-15-batch-lark-flat-design.md
"""
import json, re, subprocess, tempfile
from pathlib import Path

from batch_errors import AuthError, StepError


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
