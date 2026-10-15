"""Claude nhìn ảnh qua `claude -p` (headless, ngữ cảnh tối thiểu ~4k token/lần).

Luôn ghép nhiều ảnh thành 1 contact sheet trước khi gửi (tiết kiệm token).
"""
import json, math, os, re, subprocess, tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from batch_errors import StepError, raise_for


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
    if ok:
        ok = (b[2] - b[0]) * (b[3] - b[1]) >= 200_000   # < 20% diện tích 1000x1000 -> bbox không đáng tin -> bỏ
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
    crop = None if mode == "combined" else _box(js.get("crop"))   # combined = ảnh ghép 2 mặt -> bbox
    return {"skip": None, "front": front, "back": back, "mode": mode,    # từ contact sheet thu nhỏ, không
            "crop": crop, "colors": colors}                              # đáng tin -> không cắt


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
 - "crop": [x0,y0,x1,y1] integers 0-1000 relative to THAT single photo's own width/height (NOT the
   contact sheet); MUST be null when mode is "combined"; only crop when the photo shows several
   different people/outfits (tight around ONE outfit), else null.
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
If REF photo shows front and back side by side, use the front view for _truoc pieces and the back
view for _sau pieces.
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
    # mode "combined" -> crop luôn None (dù model trả về bbox) vì bbox ước lượng từ contact sheet
    # thu nhỏ, không đáng tin cho ảnh ghép 2 người
    p2 = norm_pick({"skip": None, "front": 1, "back": None, "mode": "combined",
                    "crop": [390, 50, 760, 400], "colors": []}, rows_, 5)
    assert p2["mode"] == "combined" and p2["crop"] is None
    # _box: vùng cắt quá nhỏ (< 20% diện tích 1000x1000) -> bỏ, tránh cắt lọt vào giữa 2 người
    assert _box([390, 50, 760, 400]) is None                 # area 129,500 < 200,000
    assert _box([100, 100, 700, 900]) == [100, 100, 700, 900]  # area 480,000 -> giữ
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
