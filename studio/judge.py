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


_BULLET = re.compile(r"^[•\-\*]\s*")


def _opts(custom):
    """'Name Custom' (nhiều dòng/bullet) -> 1 dòng ngắn cho prompt, hoặc '-' nếu rỗng."""
    if not custom:
        return "-"
    parts = [_BULLET.sub("", p.strip()) for p in custom.split("\n") if p.strip()]
    return " / ".join(parts) if parts else "-"


def _norm_group(g, ids, seen, n):
    """1 nhóm (biến thể + màu) -> dict chuẩn hoá, hoặc None nếu front sai / hết record_id hợp lệ."""
    front = g.get("front")
    if not isinstance(front, int) or not 1 <= front <= n:
        return None                            # front sai -> bỏ cả nhóm, record_id dồn sang nhóm khác
    back = g.get("back")
    back = back if isinstance(back, int) and 1 <= back <= n and back != front else None
    mode = g.get("mode") if g.get("mode") in ("infer", "two", "combined") else "infer"
    if mode == "two" and back is None:
        mode = "infer"
    if mode != "two":
        back = None
    rids = [x for x in (g.get("record_ids") or []) if x in ids and x not in seen]
    if not rids:
        return None
    seen |= set(rids)
    color = str(g.get("color") or "photo")
    garment = g.get("garment") if g.get("garment") in ("top", "bottom", "top_bottom", "dress") else "top_bottom"
    # combined = ảnh ghép nhiều biến thể -> crop vẫn có thể đáng tin (vùng riêng của 1 biến thể),
    # vẫn qua _box để lọc vùng quá nhỏ/toạ độ hỏng. back_crop = vùng cắt của ẢNH SAU (chỉ đáng tin khi
    # mode "two" vì đó mới là 1 ảnh riêng của biến thể này, không phải ảnh ghép nhiều biến thể).
    return {"variant": str(g.get("variant") or "default"), "color": color, "record_ids": rids,
            "matches_photo": bool(g.get("matches_photo", True)) or color.lower() == "photo",
            "front": front, "back": back, "mode": mode, "crop": _box(g.get("crop")),
            "back_crop": _box(g.get("back_crop")) if mode == "two" else None,
            "garment": garment}


def norm_pick(js, rows, n):
    if js.get("skip"):
        return {"skip": str(js["skip"])[:150]}
    ids = [r["record_id"] for r in rows]
    seen, groups = set(), []
    for g in js.get("groups") or []:
        ng = _norm_group(g, ids, seen, n)
        if ng:
            groups.append(ng)
    rest = [x for x in ids if x not in seen]
    if rest:                                   # Claude sót dòng / nhóm front sai -> dồn vào nhóm hợp lệ đầu
        if not groups:
            raise StepError("judge", "không có nhóm hợp lệ nào")
        groups[0]["record_ids"] += rest
    if not groups:
        raise StepError("judge", "không có nhóm hợp lệ nào")
    manual = {}                                                   # customisation không gen được -> ghi Note
    for rid, note in (js.get("manual") or {}).items():
        note = str(note)[:120]
        if rid in ids and note:
            manual[rid] = note
    return {"skip": None, "groups": groups, "manual": manual}


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
Order rows that use these photos (record_id | SKU Design | Color | customer options):
{rows}

Realistic body parts depicted ON the garment (muscles, abs, chest, skin, tattoos, faces) are part of
the PRINT, not real skin.
1. Is the product a GARMENT / APPAREL item (t-shirt, shirt, hoodie, dress, skirt, pants, jogger, jersey,
jumpsuit, pajamas, athletic set, kids clothing...)? If NOT (magnet, plaque, vase, mug, tumbler, decor,
cap, guest book, patch, accessory...) return {{"skip": "<short reason with the product type>"}}.
2. Otherwise split the order rows into GROUPS: rows sharing the same DESIGN VARIANT (e.g. a men's vs
women's print — read "Choose design" in customer options) AND the same garment colour form one group.
A listing with one design has one group per colour (or a single group if one colour). The product type
written in SKU Design (e.g. "T-shirt"/"Tshirt" = short sleeves, "Sweatshirt" = long sleeves no hood,
"Hoodie", "Jogger"/"Pants"/"Shorts", "Skirt", "Dress", "Jumpsuit", "Set") is part of the variant: choose
photos showing exactly that product type, and set "garment" accordingly. Printed underwear, legs or body
on a top are part of the top's print, not a separate bottom garment. If a flat product image shows this
variant's front (and back), use it for front (and back) — mode "combined" with crop on that variant's
front+back region, or mode "two" with crop/back_crop — rather than a lifestyle photo. Pick EACH group's
OWN inputs for a flat sewing-pattern generator:
 - "variant": short words naming the design variant (e.g. "Men", "Women"), or "default" if there is
   only one design.
 - "color": the garment colour this group's rows ask for. Read colour words in SKU Design / Color;
   personal names, sizes and design numbers are NOT colours; a row without a colour uses "photo".
 - "matches_photo": true if that colour is the one shown in the photos, else false.
 - "garment": "top" | "bottom" | "top_bottom" | "dress" — what this variant's product actually IS,
   decided mainly from the product type in SKU Design (T-shirt/Sweatshirt/Hoodie = top; Jogger/Pants/
   Shorts/Skirt = bottom; Jumpsuit/Set/pajamas = top_bottom; Dress = dress).
 - "front": label of the clearest photo of the FRONT of THAT variant. PREFER a clean flat /
   ghost-mannequin product image over a lifestyle photo of a person wearing it; fall back to a lifestyle
   photo only if no flat/ghost-mannequin shot exists for that variant. NEVER choose a BLANK template /
   generic mockup (plain white or single-colour garments with no artwork, often several product styles
   labelled "Hoodie / Zip Hoodie / Joggers" or "FRONT / BACK" on a pastel background) when any photo shows
   the actual printed design — those only illustrate the cut/style. A clean flat image is preferred ONLY
   if it shows the same artwork as the design photos.
 - "back": label of a photo of the real BACK of the SAME variant, or null.
 - "mode": "two" if back is a separate photo; "combined" if the chosen front photo itself shows that
   variant's front and back side by side (then back = null); otherwise "infer".
 - "crop": [x0,y0,x1,y1] integers 0-1000 relative to the FRONT photo's own width/height (NOT the
   contact sheet). When one photo shows SEVERAL variants side by side (e.g. men's front/back next to
   women's front/back), set "crop" to the region covering ONLY this group's variant (both its front and
   back) and use mode "combined". Also crop when a photo shows several unrelated people/outfits (tight
   around ONE outfit). Otherwise null.
 - "back_crop": like "crop" but relative to the BACK photo's own width/height, only used when "mode" is
   "two" (a separate back photo that ALSO shows several variants side by side needs its own crop down to
   this variant's back region). null when mode isn't "two" or the back photo already shows only this
   variant.
 - "record_ids": this group's order rows' record_id; every record_id exactly once across ALL groups.
   Rows whose customer options don't name a variant go to the variant shown in the most photos.
 - "manual": {{"record_id": "<short Vietnamese note of the customer options the generated file will NOT
   match, e.g. 'skin tone S3', 'tên: Anna', 'ảnh khách'>"}} — include only rows with such unmet
   customer options (a skin tone code, a name, a customer photo); omit rows with no such options.
Return ONLY: {{"skip": null, "groups": [{{"variant": "default", "color": "photo", "matches_photo": true,
"garment": "top_bottom", "front": 1, "back": null, "mode": "infer", "crop": null, "back_crop": null,
"record_ids": [...]}}], "manual": {{}}}}"""

QA = """Top row = {n_refs} REFERENCE photo(s) of the real garment. Other tiles = generated flat pieces,
label = piece name. Required base fabric colour: {colour}.
Realistic body parts depicted ON the garment (muscles, abs, chest, skin, tattoos, faces) are part of
the PRINT, not real skin.
Pieces: {names}. Suffix _truoc = FRONT panel, _sau = BACK panel; ao = torso, quan = pants, tay = sleeve,
than = dress bodice, ta = dress skirt; "art" = one flat technical drawing of the whole outfit (front).
Judge every piece against the reference: ok (true only if usable as a production print file),
score 1-10, errors = codes from:
 mo_nhoe (blurry/smeared/noisy), sai_mau (base or print colour differs from reference / required colour),
 sau_giong_truoc (a BACK piece copies front-only graphics: chest print, logo, badge, zipper, buttons),
 sai_hoa_tiet (artwork missing/extra/misplaced vs reference; a plain blank/white piece when the
 garment has a printed design is sai_hoa_tiet; a piece that replaces a printed
 body/skin illusion with a pattern is sai_hoa_tiet), sai_manh (contains other parts: sleeves or
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
    lines = "\n".join(f"{r['record_id']} | {r['sku']} | {r['color'] or '-'} | {_opts(r.get('custom'))}"
                      for r in rows)
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
    # 1 nhóm, back trùng front -> bỏ back/mode; record_id lạ bị lọc; r3 sót -> vào nhóm hợp lệ đầu tiên
    p = norm_pick({"skip": None, "groups": [
        {"variant": "default", "color": "black", "matches_photo": True, "front": 2, "back": 2,
         "mode": "two", "crop": [10, 20, 900, 990], "record_ids": ["r1", "zz"]},
        {"variant": "default", "color": "pink", "matches_photo": False, "front": 1, "back": None,
         "mode": "infer", "crop": None, "record_ids": ["r2", "r1"]}]}, rows_, 5)
    assert p["groups"][0]["front"] == 2 and p["groups"][0]["back"] is None and p["groups"][0]["mode"] == "infer"
    assert p["groups"][0]["crop"] == [10, 20, 900, 990]
    ids = [x for g in p["groups"] for x in g["record_ids"]]
    assert sorted(ids) == ["r1", "r2", "r3"] and len(ids) == 3            # r3 bị sót -> vào nhóm hợp lệ đầu
    assert p["groups"][1] == {"variant": "default", "color": "pink", "matches_photo": False,
                              "record_ids": ["r2"], "front": 1, "back": None, "mode": "infer", "crop": None,
                              "back_crop": None, "garment": "top_bottom"}
    # back_crop: giữ khi mode "two", bỏ khi mode khác, bỏ nếu vùng quá nhỏ (< 20% diện tích)
    p5 = norm_pick({"skip": None, "groups": [
        {"variant": "default", "color": "photo", "matches_photo": True, "front": 1, "back": 2,
         "mode": "two", "crop": None, "back_crop": [100, 100, 700, 900], "record_ids": ["r1"]},
        {"variant": "default", "color": "photo", "matches_photo": True, "front": 1, "back": None,
         "mode": "infer", "crop": None, "back_crop": [100, 100, 700, 900], "record_ids": ["r2"]},
        {"variant": "default", "color": "photo", "matches_photo": True, "front": 1, "back": 2,
         "mode": "two", "crop": None, "back_crop": [390, 50, 760, 400], "record_ids": ["r3"]}]}, rows_, 5)
    assert p5["groups"][0]["mode"] == "two" and p5["groups"][0]["back_crop"] == [100, 100, 700, 900]
    assert p5["groups"][1]["mode"] == "infer" and p5["groups"][1]["back_crop"] is None     # not "two" -> bỏ
    assert p5["groups"][2]["back_crop"] is None                                            # vùng quá nhỏ -> bỏ
    # garment: giữ khi hợp lệ, mặc định "top_bottom" khi thiếu/sai
    p6 = norm_pick({"skip": None, "groups": [
        {"variant": "default", "color": "photo", "matches_photo": True, "front": 1, "back": None,
         "mode": "infer", "crop": None, "garment": "top", "record_ids": ["r1"]},
        {"variant": "default", "color": "photo", "matches_photo": True, "front": 1, "back": None,
         "mode": "infer", "crop": None, "garment": "nonsense", "record_ids": ["r2"]},
        {"variant": "default", "color": "photo", "matches_photo": True, "front": 1, "back": None,
         "mode": "infer", "crop": None, "record_ids": ["r3"]}]}, rows_, 5)
    assert p6["groups"][0]["garment"] == "top"
    assert p6["groups"][1]["garment"] == "top_bottom"
    assert p6["groups"][2]["garment"] == "top_bottom"
    # mode "combined": vùng cắt quá nhỏ (< 20% diện tích) -> _box bỏ -> None (vẫn áp dụng cho nhóm)
    p2 = norm_pick({"skip": None, "groups": [
        {"variant": "default", "color": "photo", "matches_photo": True, "front": 1, "back": None,
         "mode": "combined", "crop": [390, 50, 760, 400], "record_ids": ["r1", "r2", "r3"]}]}, rows_, 5)
    assert p2["groups"][0]["mode"] == "combined" and p2["groups"][0]["crop"] is None
    # mode "combined" với vùng cắt hợp lệ -> GIỮ (khác nhóm màu cũ: vùng cắt riêng của 1 biến thể
    # trên ảnh ghép nhiều biến thể là đáng tin) -> 2 nhóm Men/Women mỗi nhóm giữ front/crop riêng
    p2b = norm_pick({"skip": None, "groups": [
        {"variant": "Men", "color": "photo", "matches_photo": True, "front": 1, "back": None,
         "mode": "combined", "crop": [0, 0, 500, 1000], "record_ids": ["r1"]},
        {"variant": "Women", "color": "photo", "matches_photo": True, "front": 2, "back": None,
         "mode": "combined", "crop": [500, 0, 1000, 1000], "record_ids": ["r2", "r3"]}]}, rows_, 5)
    assert p2b["groups"][0]["variant"] == "Men" and p2b["groups"][0]["front"] == 1
    assert p2b["groups"][0]["crop"] == [0, 0, 500, 1000]
    assert p2b["groups"][1]["variant"] == "Women" and p2b["groups"][1]["front"] == 2
    assert p2b["groups"][1]["crop"] == [500, 0, 1000, 1000]
    # nhóm front sai (99) -> bị bỏ, record_id của nó dồn vào nhóm hợp lệ đầu tiên
    p3 = norm_pick({"skip": None, "groups": [
        {"variant": "Men", "color": "photo", "matches_photo": True, "front": 1, "back": None,
         "mode": "infer", "crop": None, "record_ids": ["r1"]},
        {"variant": "Women", "color": "photo", "matches_photo": True, "front": 99, "back": None,
         "mode": "infer", "crop": None, "record_ids": ["r2", "r3"]}]}, rows_, 5)
    assert len(p3["groups"]) == 1
    assert sorted(p3["groups"][0]["record_ids"]) == ["r1", "r2", "r3"]
    # toàn bộ nhóm front sai -> không còn nhóm nào hợp lệ -> StepError
    try:
        norm_pick({"skip": None, "groups": [{"variant": "x", "color": "photo", "matches_photo": True,
                   "front": 99, "back": None, "mode": "infer", "crop": None, "record_ids": ["r1"]}]},
                  rows_, 5)
        raise AssertionError("mọi nhóm front sai phải lỗi")
    except StepError as e:
        assert e.kind == "judge"
    # manual: lọc record_id lạ, giá trị rỗng; skip thì không có key "manual"
    assert p["manual"] == {}                              # không truyền "manual" -> {}
    p4 = norm_pick({"skip": None, "groups": [
        {"variant": "default", "color": "photo", "matches_photo": True, "front": 1, "back": None,
         "mode": "infer", "crop": None, "record_ids": ["r1", "r2", "r3"]}],
        "manual": {"r1": "skin tone S3", "zz": "unknown rid", "r2": "", "r3": "x" * 200}}, rows_, 5)
    assert p4["manual"] == {"r1": "skin tone S3", "r3": "x" * 120}   # zz bỏ (lạ), r2 bỏ (rỗng), r3 cắt 120
    assert "manual" not in norm_pick({"skip": "Magnet"}, rows_, 5)
    # _opts: gộp 'Name Custom' nhiều dòng/bullet thành 1 dòng cho prompt
    assert _opts("") == "-" and _opts(None) == "-"
    assert _opts("• Choose design: Men\n• Choose skin tone: S3") == \
        "Choose design: Men / Choose skin tone: S3"
    # prompt: phải nhắc hình cơ thể in = họa tiết, ưu tiên ảnh phẳng, có key "manual"
    for p_txt in (PICK, QA):
        assert "not real skin" in p_txt
    assert "ghost-mannequin" in PICK and '"manual"' in PICK and '"variant"' in PICK
    assert '"back_crop"' in PICK and '"garment"' in PICK and "SKU Design" in PICK
    assert "sai_hoa_tiet" in QA and "printed body/skin illusion" in " ".join(QA.split())
    # _box: vùng cắt quá nhỏ (< 20% diện tích 1000x1000) -> bỏ, tránh cắt lọt vào giữa 2 người
    assert _box([390, 50, 760, 400]) is None                 # area 129,500 < 200,000
    assert _box([100, 100, 700, 900]) == [100, 100, 700, 900]  # area 480,000 -> giữ
    q = norm_qa({"pieces": {"ao_truoc": {"ok": True, "score": 9, "errors": []},
                            "ao_sau": {"ok": True, "score": 7, "errors": ["sau_giong_truoc", "xyz"]}}},
                ["ao_truoc", "ao_sau", "art"])
    assert q["ao_truoc"] == {"ok": True, "score": 9.0, "errors": []}
    assert q["ao_sau"] == {"ok": False, "score": 7.0, "errors": ["sau_giong_truoc"]}
    assert q["art"]["ok"] is False and q["art"]["score"] == 0.0
    # ảnh mẫu trắng/mockup chung không có họa tiết -> không phải thiết kế (bài học Zip Hoodie)
    assert "BLANK" in PICK and "blank" in QA.lower()
    print("judge selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
