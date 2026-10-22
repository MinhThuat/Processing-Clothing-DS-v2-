"""Claude nhìn ảnh qua `claude -p` (headless, ngữ cảnh tối thiểu ~4k token/lần).

Luôn ghép nhiều ảnh thành 1 contact sheet trước khi gửi (tiết kiệm token).
"""
import json, math, os, re, subprocess, tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat

from batch_errors import StepError, raise_for


MODEL = os.getenv("FLAT_JUDGE_MODEL", "sonnet")
TILE, MAX_SIDE, LABEL_H = 384, 1600, 28
SYS = ("You are a meticulous garment-print QA assistant. Use the Read tool to view the image "
       "file(s) you are given, then answer with ONE JSON object only — no prose.")
QA_CODES = ["mo_nhoe", "sai_mau", "sau_giong_truoc", "sai_hoa_tiet", "sai_manh",
            "khong_tran", "vung_den", "cat_loi", "art_thieu", "sai_kieu"]


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


def _grid_label(d, xy, text, font):
    """Chữ trắng viền đen -> đọc được trên nền sáng lẫn tối."""
    x, y = xy
    for dx, dy in ((-1, -1), (-1, 1), (1, -1), (1, 1), (0, -1), (0, 1), (-1, 0), (1, 0)):
        d.text((x + dx, y + dy), text, fill="black", font=font)
    d.text((x, y), text, fill="white", font=font)


def grid_overlay(src, out, max_side=MAX_SIDE):
    """1 ảnh gốc (cạnh dài <= max_side) + lưới toạ độ PIXEL đè lên (vạch bán trong suốt mỗi 100px,
    nhãn số px ở mép trên/trái) -> cho judge.locate đọc bbox chính xác hơn tile ~384px của contact
    sheet. Dùng pixel chứ không 0-1000: thử thật, Claude luôn trả toạ độ pixel dù nhãn ghi 0-1000."""
    im = Image.open(src).convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side))
    w, h = im.size
    overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for g in range(0, max(w, h) + 1, 100):
        d.line([(g, 0), (g, h)], fill=(255, 60, 0, 120), width=1)
        d.line([(0, g), (w, g)], fill=(255, 60, 0, 120), width=1)
    im = Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")
    d2 = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=16)
    for g in range(0, max(w, h) + 1, 100):
        if g < w:
            _grid_label(d2, (g + 2, 2), str(g), font)
        if g < h:
            _grid_label(d2, (2, g + 2), str(g), font)
    out = Path(out); out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, quality=90)
    return out


def px_to_box(box, w, h, pad=0.03):
    """bbox pixel [x0,y0,x1,y1] trên ảnh w x h -> 0-1000, nới mỗi phía `pad` (đỡ cụt tay áo), kẹp biên."""
    if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)):
        return None
    x0, y0, x1, y1 = box
    px, py = pad * w, pad * h
    return [int(max(0, min(1000, v))) for v in ((x0 - px) * 1000 / w, (y0 - py) * 1000 / h,
                                                (x1 + px) * 1000 / w, (y1 + py) * 1000 / h)]


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


def ask_codex(prompt, images, timeout=300):
    """Như ask() nhưng hỏi GPT qua endpoint Codex (flat_pipeline._codex_vision) — giám khảo thứ 2."""
    import flat_pipeline as fp
    content = [{"type": "text", "text": prompt}] + [
        {"type": "image_url", "image_url": {"url": fp._vision_uri(p, QA_PIECE_MAX)}} for p in images]
    try:
        return _extract_json(fp._codex_vision(content, timeout))
    except Exception as e:
        raise_for(str(e), "judge")
        raise StepError("judge", f"codex: {e}"[:300])


# ---------- chuẩn hoá JSON trả về ----------
def _box_min(b, min_area):
    ok = (isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b)
          and all(0 <= v <= 1000 for v in b) and b[2] > b[0] and b[3] > b[1])
    if ok:
        ok = (b[2] - b[0]) * (b[3] - b[1]) >= min_area
    return [int(v) for v in b] if ok else None


def _box(b):
    return _box_min(b, 200_000)   # < 20% diện tích 1000x1000 -> bbox không đáng tin -> bỏ


def _box_small(b):
    # judge.locate: ô 1 biến thể trong ảnh ghép nhiều biến thể (vd lưới 2x4) có thể chỉ ~2-12% diện
    # tích -> ngưỡng thấp hơn _box; vẫn lọc toạ độ hỏng/quá nhỏ do Claude đoán bừa
    return _box_min(b, 20_000)


_BULLET = re.compile(r"^[•\-\*]\s*")


def _opts(custom):
    """'Name Custom' (nhiều dòng/bullet) -> 1 dòng ngắn cho prompt, hoặc '-' nếu rỗng."""
    if not custom:
        return "-"
    parts = [_BULLET.sub("", p.strip()) for p in custom.split("\n") if p.strip()]
    return " / ".join(parts) if parts else "-"


def _label(v, n):
    """Số ảnh 1..n Claude trả (int, hoặc chuỗi "4" — hay gặp: Athletic Set 19/10 trả ['4','2','1'] bị lọc
    hết) -> int; sai -> None."""
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    return v if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= n else None


def _norm_group(g, ids, seen, n):
    """1 nhóm (biến thể + màu) -> dict chuẩn hoá, hoặc None nếu front sai / hết record_id hợp lệ."""
    front = _label(g.get("front"), n)
    if front is None:
        return None                            # front sai -> bỏ cả nhóm, record_id dồn sang nhóm khác
    back = _label(g.get("back"), n)
    back = back if back != front else None
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
    if not groups:
        raise StepError("judge", "không có nhóm hợp lệ nào")
    # Claude sót dòng / nhóm front sai -> KHÔNG đoán dồn vào nhóm khác (có thể sai biến thể/màu,
    # vd lên nhầm file men/đen cho đơn women/hồng) -> để "unassigned", batch ghi LỖI riêng cho dòng đó.
    unassigned = [x for x in ids if x not in seen]
    manual = {}                                                   # customisation không gen được -> ghi Note
    for rid, note in (js.get("manual") or {}).items():
        note = str(note)[:120]
        if rid in ids and note:
            manual[rid] = note
    return {"skip": None, "groups": groups, "manual": manual, "unassigned": unassigned}


QA_MIN = 8      # điểm sàn: dưới 8 là trượt dù Claude ghi ok (QA từng cho qua mảnh lỗi rõ ở 7-8 điểm)

def norm_qa(js, names):
    got = js.get("pieces") if isinstance(js, dict) and isinstance(js.get("pieces"), dict) else {}
    out = {}
    for n in names:
        r = got.get(n) if isinstance(got.get(n), dict) else {}
        errs = [e for e in (r.get("errors") or []) if e in QA_CODES]
        score = r.get("score") if isinstance(r.get("score"), (int, float)) else 0
        out[n] = {"ok": bool(r.get("ok")) and not errs and score >= QA_MIN, "score": float(score),
                  "errors": errs}
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
"Hoodie", "Jogger"/"Pants"/"Shorts", "Skirt", "Dress", "Jumpsuit", "Set") helps tell variants apart and
choose photos of that variant. Printed underwear, legs or body
on a top are part of the top's print, not a separate bottom garment. If a flat product image shows this
variant's front (and back), use it for front (and back) — mode "combined" with crop on that variant's
front+back region, or mode "two" with crop/back_crop — rather than a lifestyle photo. Pick EACH group's
OWN inputs for a flat sewing-pattern generator:
 - "variant": short words naming the design variant (e.g. "Men", "Women"), or "default" if there is
   only one design.
 - "color": the garment colour this group's rows ask for. Read colour words in SKU Design / Color;
   personal names, sizes and design numbers are NOT colours; a row without a colour uses "photo".
 - "matches_photo": true if that colour is the one shown in the photos, else false.
 - "garment": "top" | "bottom" | "top_bottom" | "dress" — decided by LOOKING at this variant's photos:
   which garments carry THIS listing's printed design. A matching printed set (top AND bottom both carry
   the design, e.g. a printed hoodie + printed jogger) = "top_bottom" even if SKU Design names only one
   piece ("Jogger", "Hoodie"). Only the top carries the design and the bottom is plain/unrelated everyday
   clothing (jeans, plain khaki shorts) = "top"; only the bottom carries it = "bottom". One-piece jumpsuit
   / onesie / pajama set = "top_bottom"; dress = "dress". Use the SKU product type only as a tie-breaker.
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
 hood on a torso piece, a person, hanger, mockup; a bodice (than) piece that includes a real area of
 skirt or pants below the waist (a thin waistband/trim strip at its bottom edge is FINE, not an
 error); a skirt (ta) piece showing bodice/torso above it, drawn as pants or
 shorts with separate legs, or with a loose tie/sash/bow/knot hanging off it; a SLEEVE (tay) piece whose
 print differs from the sleeve in the reference — e.g. pants stripes/chevrons or torso graphics on a plain
 sleeve: compare each tay piece ONLY with the reference's sleeve), khong_tran (torso/pants/sleeve piece not full-bleed:
 margins, border or garment outline visible; the piece shows the garment's SHAPE — a 3D-looking pair of
 pants with legs/crotch/cuffs, a hoodie silhouette — or a black/white/grey background around it,
 instead of the flat print filling the whole frame), vung_den (plain areas filled black/void/glow; a glow,
 halo, dark fringe or shadow around a cut-out dress piece),
 cat_loi (dress cut-out broken / missing corners), art_thieu (art misses front details or wrong
 top/bottom layout).
Be STRICT — these go straight to production. Inspect every tile closely, especially its EDGES and
borders. score 8 = no visible defect at all; any defect -> ok=false with its code. When in doubt, fail.
If REF photo shows front and back side by side, use the front view for _truoc pieces and the back
view for _sau pieces.
Return ONLY: {{"pieces": {{"<name>": {{"ok": true, "score": 9, "errors": []}}, ...}}}} covering every name."""

REHINT = """Generated pieces failed QA twice: {fails} (piece: error codes).
Contact sheet: source photos labelled 1..{n} of the garment, then the failed pieces (label = name).
Write ONE short FIX instruction in English (<= 40 words) for the image generator that prevents these
exact errors, e.g. "BACK torso panel: plain base fabric, NO chest graphic, NO zipper".
Also re-pick the input: "front" = label of the best FRONT photo (may differ from before) and optional
"crop" [x0,y0,x1,y1] integers 0-1000 tight around the single garment (or null).
Re-pick ONLY among photos of THIS variant/product type: {variant} / {garment} / {skus}.
Return ONLY: {{"hint": "...", "front": 1, "crop": null}}"""

LOCATE = """This photo may show SEVERAL design variants laid out together (numbered/labelled designs,
men's/women's, different colours...). The image is {w}x{h} pixels; red grid lines are drawn every
100 px, labelled in pixels along the top and left edges.
Return ONLY {{"box": [x0,y0,x1,y1]}} as integer PIXEL coordinates of this image, tightly covering
THIS variant's garment ({variant}; order rows: {skus}) — not a neighbouring number/variant. If each
variant shows both an adult garment and a kid garment side by side, take the {kind} one. Include the
whole garment: its front, and its back too if shown side by side with the front.
Return {{"box": null}} if this variant can't be found in the photo."""


FIND_LEGEND = """The contact sheet shows {n} product photos labelled 1..{n}. Is there a photo where the different
designs are each marked with a number — a NUMBER badge (1, 2, 3...) or a text label like "Design 1",
"Design 2", "D1", "D2", "#1", "No. 1", or a letter code like "F1", "A5" — on, above or next to each outfit or
item (a "numbered legend")?
Return ONLY {{"legend": <label of that photo, or null>}}"""

MATCH_DESIGN = """Image 1 is the numbered legend photo: each outfit is marked with a number badge or a label like
"Design 1". Image 2 is a
contact sheet of all product photos labelled 1..{n}.
Find the outfit marked with number {num} in image 1 and describe it in a few words (colours, character).
Then list the contact-sheet labels whose photo shows THAT SAME outfit ({garment}), best FRONT view
first (photos showing ONLY that outfit, front+back, are best; group/legend photos last). The SAME print
design in another size or body (a flat-lay, a KID version, a different model or pose) IS the same outfit.
Return ONLY {{"desc": "...", "photos": [labels]}} — "photos": [] if none shows it."""


def match_design(sheet, legend_src, num, n, garment="outfit"):
    """'Design N' -> {"desc", "photos": [label...]} theo ảnh legend có số (đọc số thật, không đoán)."""
    js = ask(MATCH_DESIGN.format(num=num, n=n, garment=garment), [legend_src, sheet])
    photos = [p for p in map(lambda x: _label(x, n), js.get("photos") or []) if p]
    return {"desc": str(js.get("desc") or "")[:200], "photos": photos}


# 'Design 4' / 'Design #4' / 'Choose design: D4' (bảng DESIGN OPTIONS D1..D10, mặt nạ Jo chapman 20/10)
# 'D4' trơn chỉ tin ở Name Custom (SKU có thể có đoạn -D4- không phải design — codex review 20/10)
DESIGN_NUM = re.compile(r"\bdesign\s*#?\s*(\d+)\b", re.I)
_D_NUM = re.compile(r"\bD(\d{1,2})\b", re.I)


def design_num(custom="", other=""):
    """Số design khách chọn: 'Design N' trong custom/other (SKU, variant), hoặc 'DN' trong custom -> int | None."""
    m = DESIGN_NUM.search(f"{custom} {other}") or _D_NUM.search(custom or "")
    return int(m.group(1)) if m else None


_D_LABEL = re.compile(r"\bdesign\s*#?\s*([A-Z]\d{1,2})\b", re.I)


def design_label(custom="", other=""):
    """Mã design khách chọn dạng chữ+số ('Design F3', 'Design A5' -> 'F3'/'A5'), không có -> số design_num
    (dạng chuỗi) -> None. Mặt nạ Ski Mask 22/10 đánh mã F1..F6 / A1..A6 trên bảng mặt."""
    m = _D_LABEL.search(f"{custom} {other}")
    if m:
        return m.group(1).upper()
    n = design_num(custom, other)
    return str(n) if n else None


def find_legend(sheet, n):
    js = ask(FIND_LEGEND.format(n=n), [sheet])
    v = js.get("legend")
    return _label(v, n)


LOCATE_PIECE = """The image is {w}x{h} pixels; red grid lines are drawn every 100 px, labelled in pixels
along the top and left edges. It shows a garment (if a FRONT and a BACK view are side by side, use the
{view} view). Return ONLY {{"box": [x0,y0,x1,y1]}} as integer PIXEL coordinates tightly covering ONLY
{label} — nothing of the other garment parts, the person's skin/head or the background.
Return {{"box": null}} if it is not visible."""


def locate_piece(photo_with_grid, label, back=False):
    """Ảnh + lưới pixel -> bbox 0-1000 của 1 mảnh (vd thân trước) hoặc None (batch: lần gen thứ 3)."""
    w, h = Image.open(photo_with_grid).size
    js = ask(LOCATE_PIECE.format(w=w, h=h, label=label, view="BACK" if back else "FRONT"), [photo_with_grid])
    return _box_small(px_to_box(js.get("box"), w, h))


def pick(sheet, rows, n):
    lines = "\n".join(f"{r['record_id']} | {r['sku']} | {r['color'] or '-'} | {_opts(r.get('custom'))}"
                      for r in rows)
    return norm_pick(ask(PICK.format(n=n, rows=lines), [sheet]), rows, n)


QA_FLAT = ("\nThese pieces are the FLAT VECTOR version of the print: solid flat colours, crisp clean edges. Any "
           "3D shading, gradient, highlight, glow, bevel or fabric texture imitating depth is sai_kieu "
           "(colour bands/stripes that are part of the design itself are fine).")


QA_PIECE_TILE, QA_PIECE_MAX = 768, 2400


def qa(sheet, names, colour, n_refs, flat=False, extra="", asker=None):
    prompt = QA.format(n_refs=n_refs, colour=colour, names=", ".join(names)) + (QA_FLAT if flat else "") + extra
    return norm_qa((asker or ask)(prompt, [sheet]), names)


# tiêu chí RIÊNG từng loại mảnh (QA chấm 1 mảnh/lần, ảnh lớn) — tiền tố tên mảnh -> yêu cầu
PIECE_RULES = [
    # đầm kiểu mới (USER 21/10): tà = chữ nhật trải thẳng, thân = khung vuông phủ kín (uốn/khớp khuôn ở bước xuất)
    ("ta_", "Dress SKIRT print UNROLLED into a full-bleed RECTANGLE: top edge = waist, bottom edge = hem; the "
            "skirt's bands/motifs in the same order from waist to hem as the reference, bands straight and "
            "horizontal, motifs upright; fills the frame edge to edge (no fan/garment outline needed); only the "
            "skirt's print; no tie/sash/bow; no bodice or waistband."),
    ("than_", "Dress BODICE print filling the WHOLE square frame edge to edge: the bodice's artwork placed like on "
              "the real bodice (neckline details at top centre, chest motifs on the chest, waist details at the "
              "bottom) with the base fabric continuing to every edge; no outline, text, person or skirt print."),
    ("ao_", "TORSO panel of the top: only the torso print (no sleeves, hood or cuffs), flat, filling the frame."),
    ("quan_", "PANTS panel: only the pants print, flat, filling the frame — not a 3D pair of pants."),
    ("tay", "SLEEVE panel: its print must match ONLY the sleeve in the reference (compare with the sleeve "
            "itself); flat, filling the frame."),
    ("bo_", "JUMPSUIT body panel: torso print on top and the legs' print below as ONE tall flat panel (no sleeves, "
            "no hood), filling the frame."),
    ("mu", "HOOD panel: only the hood's outer print, flat, filling the frame — not a 3D hood or a whole top."),
    ("art", "ART: one flat technical drawing of the whole product front with every front detail."),
]


def piece_rule(name):
    rule = next((r for p, r in PIECE_RULES if name.startswith(p)), "")
    if "_sau" in name:
        rule += (" This is a BACK piece: match the reference BACK view if given; if no back photo, it must be "
                 "a plausible plainer back WITHOUT front-only graphics.")
    return f"\nCriteria for {name}: {rule}" if rule else ""


def qa_piece(refs, piece, name, colour, flat, out):
    """Chấm 1 mảnh: hàng 1 = ảnh tham chiếu, hàng 2 = mảnh (ô lớn) + tiêu chí riêng loại mảnh."""
    sheet = contact_sheet([refs, [(piece, name)]], out, tile=QA_PIECE_TILE, max_side=QA_PIECE_MAX)
    return qa(sheet, [name], colour, len(refs), flat, piece_rule(name))[name]


def rehint(sheet, fails, n_src, variant="default", garment="top_bottom", skus=""):
    txt = "; ".join(f"{k}: {','.join(v) or '?'}" for k, v in fails.items())
    js = ask(REHINT.format(fails=txt, n=n_src, variant=variant, garment=garment, skus=skus or "-"), [sheet])
    front = js.get("front") if isinstance(js.get("front"), int) and 1 <= js["front"] <= n_src else None
    return {"hint": str(js.get("hint") or "")[:400], "front": front, "crop": _box(js.get("crop"))}


def locate(photo_with_grid, variant, skus, kid):
    """Ảnh gốc + lưới pixel (grid_overlay) -> bbox 0-1000 của ĐÚNG biến thể `variant` (SKU: `skus`)
    trong ảnh, chọn áo người lớn/trẻ em theo `kid`. None nếu Claude không định vị được / toạ độ hỏng."""
    kind = "KID" if kid else "ADULT"
    w, h = Image.open(photo_with_grid).size
    js = ask(LOCATE.format(w=w, h=h, variant=variant, skus=skus or "-", kind=kind), [photo_with_grid])
    return _box_small(px_to_box(js.get("box"), w, h))


def _selfcheck():
    assert _label("4", 12) == 4 and _label(4, 12) == 4 and _label(" 2 ", 3) == 2
    assert design_num("• Choose design: D4") == 4 and design_num("", "MLT012610A14-Design 2-Jogger") == 2
    assert design_num("Design #100") == 100 and design_num("• Design: Design Wolverine") is None
    assert design_num("", "NMN062610A10-Jo chapman1 191026-3D Funny Face Ski Mask") is None
    assert design_label("• Design: Design F3", "VPC072610A02-Design F3-3D Funny Face Ski Mask") == "F3"
    assert design_label("• Design: Design 4") == "4" and design_label("", "X-Ski Mask") is None
    assert design_label("", "DHP091026A10-Design A5-3D Funny Face Ski Mask") == "A5"
    assert design_num("", "ABC-D4-Mask") is None          # 'D4' trong SKU không tính
    assert _label("13", 12) is None and _label(0, 12) is None and _label(True, 12) is None and _label("x", 9) is None
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
    # #1 r3 bị Claude bỏ sót -> KHÔNG được dồn nhầm vào nhóm khác (có thể sai biến thể/màu);
    # phải rơi vào "unassigned" để batch ghi LỖI riêng cho dòng đó, không gen nhầm ảnh
    assert sorted(ids) == ["r1", "r2"] and len(ids) == 2
    assert p["unassigned"] == ["r3"]
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
    # #1 nhóm "Women" front sai (99) bị bỏ cả nhóm -> r2/r3 KHÔNG được dồn vào nhóm "Men" hợp lệ
    # (sai biến thể) mà phải vào "unassigned"
    assert p3["groups"][0]["record_ids"] == ["r1"]
    assert sorted(p3["unassigned"]) == ["r2", "r3"]
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
    assert p4["unassigned"] == []                                   # đủ cả 3 dòng -> không ai bị bỏ sót
    assert "manual" not in norm_pick({"skip": "Magnet"}, rows_, 5)
    assert "unassigned" not in norm_pick({"skip": "Magnet"}, rows_, 5)
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
    # #10 rehint: phải truyền variant/garment/SKU để Claude CHỈ chọn lại ảnh ĐÚNG biến thể/loại SP
    # (không lạc sang biến thể/SP khác trong cùng đơn nhiều dòng)
    assert "THIS variant/product type" in REHINT
    orig_ask = globals()["ask"]
    captured = {}

    def fake_ask(prompt, images, timeout=300):
        captured["prompt"] = prompt
        return {"hint": "FIX X", "front": 2, "crop": [10, 10, 500, 500]}
    globals()["ask"] = fake_ask
    try:
        r = rehint(td / "s.jpg", {"tay_sau": ["mo_nhoe"]}, 3, variant="Men", garment="top", skus="SKU-A; SKU-B")
    finally:
        globals()["ask"] = orig_ask
    assert r == {"hint": "FIX X", "front": 2, "crop": [10, 10, 500, 500]}
    assert "Men" in captured["prompt"] and "/ top /" in captured["prompt"] and "SKU-A; SKU-B" in captured["prompt"]
    # #11 _box_small (locate): ngưỡng 2% diện tích (thấp hơn _box 20%) -> nhận ô biến thể ~12%, bỏ ~1%
    assert _box_small([0, 0, 400, 300]) == [0, 0, 400, 300]     # area 120,000 = 12% -> giữ
    assert _box_small([0, 0, 100, 100]) is None                 # area 10,000 = 1% -> bỏ
    assert _box([0, 0, 400, 300]) is None                       # cùng box nhưng dưới ngưỡng 20% của _box
    # #12 grid_overlay: cạnh dài <= max_side, có vẽ lưới/nhãn đè lên góc trên-trái (khác ảnh gốc)
    gsrc = td / "g.png"; Image.new("RGB", (2000, 1000), "white").save(gsrc)
    gout = grid_overlay(gsrc, td / "g_grid.jpg", max_side=800)
    gim = Image.open(gout)
    assert max(gim.size) <= 800
    corner = ImageStat.Stat(gim.convert("L").crop((0, 0, 20, 20))).mean[0]
    assert corner < 250                        # nhãn "0" vẽ đè góc trên-trái -> không còn trắng tinh
    # #13 locate: truyền đúng variant/SKU/KID-ADULT vào prompt; box hợp lệ/qua _box_small, None khi không thấy
    def fake_ask_locate(prompt, images, timeout=300):
        captured["locate_prompt"] = prompt
        return {"box": [80, 80, 480, 320]}          # pixel trên ảnh lưới 800x400
    globals()["ask"] = fake_ask_locate
    try:
        b = locate(gout, "Design 2", "SKU-A; SKU-B", True)
    finally:
        globals()["ask"] = orig_ask
    assert gim.size == (800, 400) and "800x400 pixels" in captured["locate_prompt"]
    assert b == [70, 170, 630, 830]               # px -> 0-1000, nới 3% mỗi phía
    assert px_to_box([0, 0, 800, 400], 800, 400) == [0, 0, 1000, 1000]   # kẹp biên
    assert px_to_box(None, 800, 400) is None and px_to_box([1, 2, 3], 800, 400) is None
    assert "Design 2" in captured["locate_prompt"] and "SKU-A; SKU-B" in captured["locate_prompt"]
    assert "KID" in captured["locate_prompt"]

    def fake_ask_locate2(prompt, images, timeout=300):
        captured["locate_prompt2"] = prompt
        return {"box": None}
    globals()["ask"] = fake_ask_locate2
    try:
        b2 = locate(gout, "Design 3", "SKU-C", False)
    finally:
        globals()["ask"] = orig_ask
    assert b2 is None and "ADULT" in captured["locate_prompt2"]
    # điểm sàn QA_MIN: Claude ghi ok nhưng điểm 7 -> vẫn trượt; 8 không lỗi -> đạt
    q = norm_qa({"pieces": {"a": {"ok": True, "score": 7, "errors": []},
                            "b": {"ok": True, "score": 8, "errors": []}}}, ["a", "b"])
    assert q["a"]["ok"] is False and q["b"]["ok"] is True
    # locate_piece: px trên ảnh lưới -> 0-1000 (+3% lề); prompt đúng mặt trước/sau + nhãn mảnh
    def fake_ask_piece(prompt, images, timeout=300):
        captured["piece"] = prompt
        return {"box": [80, 80, 480, 320]}
    globals()["ask"] = fake_ask_piece
    try:
        bp = locate_piece(gout, "the BACK bodice", back=True)
    finally:
        globals()["ask"] = orig_ask
    assert bp == [70, 170, 630, 830] and "BACK view" in captured["piece"] and "the BACK bodice" in captured["piece"]
    # match_design: lọc nhãn ảnh ngoài 1..n / không phải số; find_legend: null / ngoài khoảng -> None
    globals()["ask"] = lambda prompt, images, timeout=300: {"desc": "Olaf", "photos": [2, "x", 99, 8]}
    try:
        assert match_design(td / "s.jpg", td / "l.jpg", 1, 11) == {"desc": "Olaf", "photos": [2, 8]}
        globals()["ask"] = lambda prompt, images, timeout=300: {"legend": 15}
        assert find_legend(td / "s.jpg", 11) is None
        globals()["ask"] = lambda prompt, images, timeout=300: {"legend": 8}
        assert find_legend(td / "s.jpg", 11) == 8
    finally:
        globals()["ask"] = orig_ask
    assert "UNROLLED" in piece_rule("ta_truoc") and "BACK piece" in piece_rule("ta_sau")
    assert "SLEEVE" in piece_rule("tay_trai_sau_phang") and piece_rule("xyz") == ""
    print("judge selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
