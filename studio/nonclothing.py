"""Nhánh RIÊNG cho sản phẩm KHÔNG phải quần áo (giỏ, lịch, cốc, decor...): mỗi dòng 2 bản art —
"phang" (phẳng lì, vector) và "3d" (giữ hiệu ứng giả 3D nổi) — đều là FILE IN (bề mặt in trải phẳng).

Tách hẳn khỏi luồng clothing (batch.process_subgroup / flat_pipeline pieces) — batch chỉ rẽ sang đây
khi judge.pick trả 'skip'. Luồng:
  Claude chọn ảnh có hình in (+ định vị nếu ảnh nhiều SP) -> codex/dola tả SP -> mỗi dòng gen 2 bản
  (màu theo cột Color, chữ/option theo Name Custom) -> Claude QA từng bản -> gen lại tối đa MAX_ATTEMPTS.
USER RULE: output là FILE IN y hình in trong ảnh nguồn (+ Name Custom), KHÔNG phải ảnh render SP;
không gồm đồ bên trong SP (vd squishy trong giỏ) và phụ kiện rời (quai, dây, thanh treo).
"""
import functools, json, shutil
from pathlib import Path

from PIL import Image

import flat_pipeline as fp
import judge
from batch_errors import StepError, raise_for

MAX_ATTEMPTS = 3
QA_MIN = 8
QA_TILE = 768

PICK = """The contact sheet shows {n} product photos of ONE listing (not clothing), labelled 1..{n}.
Order rows: {rows}.
Pick the ONE photo that shows the product most clearly from the FRONT WITH ITS PRINTED DESIGN fully
visible. NEVER pick a blank/plain product, a colour chart/swatch page, or a size/detail infographic.
Strongly prefer a photo with a SINGLE product (ideally the ordered colour). Set "several": true only
if the chosen photo shows more than one product.
Return ONLY {{"front": <label>, "several": false, "product": "<short product type>"}}"""

VISION = (
    "This photo shows a product (not clothing). Describe its PRINTED artwork so it can be redrawn as a "
    "flat print file. Return ONLY JSON: "
    '{"product":"<type>","surface":"<the printable surface(s) of the front, e.g. front panel + cuff band, '
    'and their flat shape>","base":"<base colour(s) of the printed surface>",'
    '"print":"<EVERY printed graphic and text with exact content, colours, font style and positions on '
    'that surface>","effects":"<printed elements that look raised/puffy/embossed/glossy/3D, or none>"}. '
    "Ignore anything placed INSIDE or ON the product that is not printed (contents, toys, fillers, "
    "plants), and its hardware (handles, ropes, rods, hooks). Colours as words or hex. No prose.")

# USER RULE: output = FILE IN (bề mặt in trải phẳng), KHÔNG phải ảnh SP; bỏ đồ bên trong / phụ kiện rời.
_ART = (
    "The PRINT FILE for this {product}: ONLY the printed artwork of its front printable surface "
    "({surface}), laid out FLAT and straight exactly as it is sent to the printer — like a textile print "
    "panel filling the frame edge to edge. NO product body/outline in perspective, NO handles, ropes, "
    "rods or hardware, NO contents or items inside/on the product, NO background scene, NO mockup. Base "
    "colour {base}. Every printed graphic and text in the same place and proportions as the reference "
    "photo: {print}. ")
ART = {
    "3d": _ART + ("Reproduce the printed artwork FAITHFULLY as printed in the photo and KEEP its faux-3D "
                  "look: raised/puffy/embossed shading, glossy highlights, bevels and depth of the printed "
                  "graphics ({effects}), plus the printed fabric texture. It is still a flat print file, not "
                  "a photo of the product."),
}
# bản phẳng vẽ LẠI TỪ bản 3D đã đạt (ref = file in 3D, không phải ảnh SP): giữ y bố cục/chữ/màu, chỉ bỏ
# bóng -> model dễ ra phẳng thật hơn (gen thẳng từ ảnh SP hay sót bóng 3D, sai bố cục chữ).
FLAT_FROM_3D = (
    "The reference image is a finished PRINT FILE whose graphics have faux-3D shading. Redraw the EXACT "
    "same print — same layout, every graphic, text, font style, sizes and colours, filling the frame edge "
    "to edge — as clean 2D FLAT vector art: SOLID FLAT COLOR FILL, crisp clean edges, NO 3D, NO shading, "
    "NO gradient, NO highlights, NO glow, NO texture. Change nothing else.")
VERSIONS = ["3d", "phang"]      # 3d trước: bản phẳng lấy bản 3D làm ref

QA = """Tile 1 = REFERENCE photo of the real product. Tile 2 = generated PRINT FILE ("art"), version: {version}.
Order: colour {color} | customer options: {opts}.
(List numbering like "1." "2." in the options only separates several names: the names go together in
the ONE design, e.g. "James / Ellie", WITHOUT the numbers — that is correct, not sai_chu.)
The art must be the product's printed artwork laid out flat (as sent to the printer) — matching the
reference print exactly, with the customer's text/options applied. Contents/items inside the product
and hardware (handles, ropes, rods) must NOT be in it.
Judge STRICTLY (it goes to production): score 1-10 and error codes from:
 sai_hoa_tiet (printed graphics missing/extra/misplaced/different vs the reference print), sai_chu (text
 differs from the customer's text in the options, or misspelled), sai_mau (base/print colours wrong),
 sai_kieu (wrong style for the version: {style}), sai_bo_cuc (text or graphics laid out differently from
 the reference print: different relative size, position, line breaks or font style; list numbers like
 "1." printed), khong_phai_file_in (shows the product body in perspective, handles/ropes/hardware,
 contents inside it, a background scene or mockup, white margins/borders or a drop shadow instead of a
 flat print filling the frame), mo_nhoe (blurry).
Return ONLY {{"score": 9, "errors": []}}"""
STYLE = {"phang": "must be flat — shading, highlights, glow, bevels or texture that imitate DEPTH/3D on a "
                  "graphic are wrong; colour bands/stripes that are part of the design itself (e.g. a "
                  "rainbow pattern) and tiny eye catch-lights are fine",
         "3d": "must reproduce the printed graphics faithfully with their raised faux-3D look, shading and "
               "texture as in the reference — a flat cartoon look is wrong when the reference print has "
               "3D/raised effects; if the reference print itself is flat, a faithful copy is correct"}
CODES = {"sai_hoa_tiet", "sai_chu", "sai_mau", "sai_kieu", "sai_bo_cuc", "khong_phai_file_in", "mo_nhoe"}
FIX = {"sai_hoa_tiet": "copy EVERY printed graphic exactly as in the reference",
       "sai_chu": "use the customer's text EXACTLY, correctly spelled",
       "sai_mau": "use exactly the ordered colours",
       "sai_kieu": "follow the required rendering style strictly",
       "sai_bo_cuc": "keep the text and graphics at the same relative size, position, line breaks and "
                     "font style as the reference print; no list numbers",
       "khong_phai_file_in": "output ONLY the flat print panel filling the frame edge to edge — no "
                             "product body, handles, contents, background, margins, shadow or mockup",
       "mo_nhoe": "crisp sharp edges"}


def custom_rule(row):
    """Màu (cột Color) + cá nhân hoá (Name Custom) -> câu ép cuối prompt."""
    s = f" The product's main colour is {row['color']}." if row.get("color") else ""
    opts = judge._opts(row.get("custom"))
    if opts != "-":
        s += (f" Apply the customer's personalisation: {opts}. Replace any sample name/text in the "
              "design with the customer's text exactly as given (same font style and position). List "
              "numbering like '1.' '2.' only separates several names — put all the names in this ONE "
              "design but do NOT print the numbers.")
    return s


def norm_qa(js):
    errs = [e for e in (js.get("errors") or []) if e in CODES] if isinstance(js, dict) else []
    score = js.get("score") if isinstance(js, dict) and isinstance(js.get("score"), (int, float)) else 0
    return {"ok": not errs and score >= QA_MIN, "score": float(score), "errors": errs}


def _gen(prompt, ref, out):
    """gen_panel trong tiến trình; lỗi -> AuthError/QuotaError/StepError('gen') như luồng clothing."""
    try:
        fp.gen_panel(prompt, [ref], out)
    except Exception as e:
        raise_for(str(e), "gen")
    fp._flatten_white(out)      # codex đôi khi trả PNG nền trong suốt -> nền trắng đục (như mảnh clothing)


@functools.lru_cache(maxsize=32)
def _legend_cached(sheet, n):
    return judge.find_legend(Path(sheet), n)


def _legend(sheet, n, log):
    """Ảnh bảng design đánh số (D1..Dn) trong bộ ảnh hoặc None; hỏi 1 lần cho cả nhóm (cùng contact sheet)."""
    try:
        return _legend_cached(sheet, n)
    except StepError as e:
        log("nc", f"tìm bảng design lỗi: {e}"); return None


def _input(r, srcs, sheet, d, log):
    """Cho 1 DÒNG: Claude chọn ảnh có hình in đúng biến thể/màu của dòng; ảnh nhiều SP -> định vị (lưới
    pixel) 1 SP -> ảnh đầu vào đã phóng. Theo từng dòng vì 1 nhóm (chung bộ link) có thể gồm nhiều
    biến thể/màu (vd giỏ Green + Purple) — dùng chung 1 ảnh thì dòng kia gen/QA theo sai mẫu."""
    rows = [r]
    num = judge.design_num(r.get("custom") or "", r["sku"])
    legend = num and _legend(str(sheet), len(srcs), log)
    if legend:      # dòng chọn 'D4' / 'Design 4' + có bảng design đánh số -> cắt ĐÚNG ô đó trên bảng (pick
        # tự chọn thì lấy ảnh người đeo bất kỳ -> sai design, mặt nạ Jo chapman 20/10: 4/5 dòng sai)
        src = srcs[legend - 1]
        try:
            box = judge.locate(judge.grid_overlay(src, d / "nc_grid.jpg"),
                               f"the ONE design labelled D{num} / Design {num} / #{num}", r["sku"], False)
        except StepError as e:
            log("nc", f"D{num}: định vị trên bảng design lỗi {e}"); box = None
        if box:
            log("nc", f"D{num}: ảnh {legend} (bảng design), box={box}")
            inp = d / "nc_in.png"
            fp.crop_norm(src, box, Image.open(src).size, inp)
            return fp._upscaled(inp, d / "nc_in_up.png"), {"front": legend, "several": True, "product": ""}
        log("nc", f"D{num}: không thấy ô trên bảng design {legend} -> để Claude chọn ảnh")
    lines = f"{r['sku']} | colour {r['color'] or '-'} | {judge._opts(r.get('custom'))}"
    pk = judge.ask(PICK.format(n=len(srcs), rows=lines), [sheet])
    front = pk.get("front") if isinstance(pk.get("front"), int) and 1 <= pk["front"] <= len(srcs) else 1
    src, box = srcs[front - 1], None
    if pk.get("several"):
        grid = judge.grid_overlay(src, d / "nc_grid.jpg")
        color = rows[0]["color"] or "the ordered"
        box = judge.locate(grid, f"one single {color} product", rows[0]["sku"], False)
    log("nc", f"ảnh {front} ({pk.get('product', '?')}), box={box}")
    inp = d / "nc_in.png"
    if box:
        fp.crop_norm(src, box, Image.open(src).size, inp)
    else:
        Image.open(src).convert("RGB").save(inp)
    return fp._upscaled(inp, d / "nc_in_up.png"), pk


def _best_art(r, version, prompt, inp, d, log, ref=None):
    """Gen 1 bản (version) cho 1 dòng từ `ref` (mặc định ảnh SP `inp`), QA so với ảnh SP, gen lại tối đa
    MAX_ATTEMPTS -> (path, qa) bản điểm cao nhất."""
    best, fix = None, ""
    for k in range(1, MAX_ATTEMPTS + 1):
        art = d / f"art_{version}_{k}.png"
        try:
            _gen(prompt + fix, ref or inp, art)
        except StepError as e:
            log("nc", f"{r['record_id']} {version} lần {k}: lỗi gen {e}"); continue
        # ô lớn (QA_TILE) để Claude thấy bóng/gradient nhỏ trên bản phẳng — ô 384px mặc định bỏ sót
        qs = judge.contact_sheet([[(inp, "REF"), (art, f"art {version}")]], d / f"sheet_qa_{version}_{k}.jpg",
                                 tile=QA_TILE)
        q = norm_qa(judge.ask(QA.format(version=version, style=STYLE[version],
                                        color=r["color"] or "as in reference",
                                        opts=judge._opts(r.get("custom"))), [qs]))
        log("nc", f"{r['record_id']} {version} lần {k}: {'✓' if q['ok'] else '✗'}{q['score']:.0f} {q['errors']}")
        if q["ok"]:
            return art, q                       # bản ĐẠT luôn thắng bản trượt điểm cao hơn
        if not best or q["score"] > best[1]["score"]:
            best = (art, q)
        if q["errors"]:
            fix = " FIX: " + "; ".join(FIX[e] for e in q["errors"]) + "."
    if not best:
        raise StepError("gen", f"{r['record_id']} {version}: gen art lỗi {MAX_ATTEMPTS} lần")
    return best


def run(rows, srcs, sheet, gdir, log):
    """Generator: yield (row, {version: (art_path, qa)}) NGAY khi xong từng dòng -> batch up luôn, dòng
    sau lỗi không làm mất dòng đã xong. Mỗi dòng chọn ảnh + vision riêng (biến thể/màu có thể khác)."""
    client = fp._ark_clients()[0]
    for r in rows:
        d = gdir / f"nc_{r['record_id']}"
        shutil.rmtree(d, ignore_errors=True); d.mkdir(parents=True)   # bỏ file lần chạy trước
        inp, pk = _input(r, srcs, sheet, d, log)
        try:
            spec, m = fp._vision(client, inp, VISION, need=("product", "print"))
        except SystemExit as e:                 # _vision hết model -> sys.exit; hết login/quota -> dừng đợt
            raise_for(str(e), "vision")
        (d / "nc_vision.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
        fields = dict(product=spec.get("product") or pk.get("product") or "product",
                      surface=spec.get("surface") or "front", base=spec.get("base", ""),
                      print=spec.get("print", ""), effects=spec.get("effects") or "as in the photo")
        a3d = _best_art(r, "3d", ART["3d"].format(**fields) + custom_rule(r), inp, d, log)
        flat = _best_art(r, "phang", FLAT_FROM_3D + custom_rule(r), inp, d, log, ref=a3d[0])
        yield r, {"3d": a3d, "phang": flat}


def _selfcheck():
    assert norm_qa({"score": 9, "errors": []})["ok"]
    assert not norm_qa({"score": 7, "errors": []})["ok"]               # dưới điểm sàn
    assert norm_qa({"score": 9, "errors": ["sai_chu", "lạ"]})["errors"] == ["sai_chu"]
    assert not norm_qa(None)["ok"]
    r = {"color": "Green", "custom": "• Enter Name: Knox's\n• Choose Name Color: Black"}
    s = custom_rule(r)
    assert "Green" in s and "Knox's" in s and "Black" in s and "do NOT print the numbers" in s
    assert custom_rule({"color": "", "custom": ""}) == ""
    f = dict(product="basket", surface="front panel", base="green", print="p", effects="puffy")
    assert VERSIONS == ["3d", "phang"] and "puffy" in ART["3d"].format(**f)
    assert "PRINT FILE" in ART["3d"].format(**f) and "NO contents" in ART["3d"].format(**f)
    assert "FLAT" in FLAT_FROM_3D and "NO shading" in FLAT_FROM_3D
    # bản ĐẠT thắng bản trượt điểm cao hơn (lần 1: 9 điểm nhưng sai_chu; lần 2: 8, đạt -> lấy lần 2)
    import tempfile
    g, orig = globals(), (_gen, judge.ask, judge.contact_sheet)
    answers = iter([{"score": 9, "errors": ["sai_chu"]}, {"score": 8, "errors": []}])
    g["_gen"] = lambda prompt, ref, out: None
    judge.ask, judge.contact_sheet = (lambda *a, **k: next(answers)), (lambda *a, **k: None)
    try:
        d = Path(tempfile.mkdtemp())
        art, q = _best_art({"record_id": "x", "color": ""}, "3d", "p", d / "in.png", d, lambda *a: None)
    finally:
        g["_gen"], judge.ask, judge.contact_sheet = orig
    assert art.name == "art_3d_2.png" and q["ok"]
    print("nonclothing selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
