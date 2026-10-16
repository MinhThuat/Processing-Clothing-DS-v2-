"""Nhánh RIÊNG cho sản phẩm KHÔNG phải quần áo (giỏ, lịch, cốc, decor...): mỗi dòng 2 bản art —
"phang" (phẳng lì, vector) và "3d" (giữ hiệu ứng giả 3D nổi như ảnh) — y ảnh nguồn + Name Custom.

Tách hẳn khỏi luồng clothing (batch.process_subgroup / flat_pipeline pieces) — batch chỉ rẽ sang đây
khi judge.pick trả 'skip'. Luồng:
  Claude chọn ảnh có hình in (+ định vị nếu ảnh nhiều SP) -> codex/dola tả SP -> mỗi dòng gen 2 bản
  (màu theo cột Color, chữ/option theo Name Custom) -> Claude QA từng bản -> gen lại tối đa MAX_ATTEMPTS.
USER RULE: thành phẩm phải Y ẢNH NGUỒN; mọi thứ gắn/đi kèm SP trong ảnh (vd squishy trong giỏ) là 1
phần của SP -> giữ nguyên; chỉ bỏ cảnh nền, người, tay, SP khác.
"""
import json
from pathlib import Path

from PIL import Image

import flat_pipeline as fp
import judge
from batch_errors import StepError, raise_for

MAX_ATTEMPTS = 3
QA_MIN = 8

PICK = """The contact sheet shows {n} product photos of ONE listing (not clothing), labelled 1..{n}.
Order rows: {rows}.
Pick the ONE photo that shows the product most clearly from the FRONT WITH ITS PRINTED DESIGN fully
visible. NEVER pick a blank/plain product, a colour chart/swatch page, or a size/detail infographic.
Strongly prefer a photo with a SINGLE product (ideally the ordered colour). Set "several": true only
if the chosen photo shows more than one product.
Return ONLY {{"front": <label>, "several": false, "product": "<short product type>"}}"""

VISION = (
    "This photo shows a product (not clothing). Describe it as a precise flat-technical spec so it can "
    "be redrawn as a flat front-view drawing. Return ONLY JSON: "
    '{"product":"<type>","shape":"<outline/shape, handles, parts>","base":"<main colour(s)>",'
    '"print":"<EVERY printed graphic and text with exact content, colours, fonts style and positions>",'
    '"items":"<EVERY object attached to or included with the product (contents, toys, accessories, '
    'decorations) with look and position, or none>","effects":"<raised/puffy/embossed/glossy/3D-looking '
    'parts, or none>"}. '
    "Colours as words or hex. No prose outside the spec.")

_ART = (
    "Reproduce this {product} EXACTLY as in the reference photo, FRONT view, centred on a plain light grey "
    "background. Same shape and proportions: {shape}. Main colour: {base}. Every printed graphic and text "
    "in the same place: {print}. Everything attached to or included with the product stays, in the same "
    "place and look: {items}. Remove ONLY the background scene (room, table, wall, other products), "
    "people and hands. ")
ART = {
    "phang": _ART + ("Render it as a 2D FLAT drawing: SOLID FLAT COLOR FILL, crisp clean edges like a "
                     "technical CAD flat — NO 3D, NO shading, NO highlights, NO shadow, NO texture."),
    "3d": _ART + ("KEEP the faux-3D look exactly as in the photo: raised/puffy/embossed printed elements, "
                  "glossy highlights, shading and depth of the graphics and items ({effects}), fabric "
                  "texture. Straight front view, no perspective distortion, no drop shadow on the "
                  "background."),
}
VERSIONS = list(ART)

QA = """Tile 1 = REFERENCE photo of the real product. Tile 2 = generated drawing ("art"), version: {version}.
Order: colour {color} | customer options: {opts}.
The art must look EXACTLY like the reference product (only the background scene, people and hands
removed), with the customer's text/options applied. Everything attached to or included with the product
in the reference (contents, toys, accessories) is PART of the product and must be there.
Judge STRICTLY (it goes to production): score 1-10 and error codes from:
 sai_hoa_tiet (printed graphics or attached/included items missing/extra/misplaced/different vs the
 reference), sai_chu (text differs from the customer's text in the options, or misspelled), sai_mau
 (product colour/option colours wrong), sai_kieu (wrong style for the version: {style}), nen_thua
 (background scene, other products, person or hands left in), mo_nhoe (blurry).
Return ONLY {{"score": 9, "errors": []}}"""
STYLE = {"phang": "must be completely flat solid colours — any 3D shading/highlight/texture is wrong",
         "3d": "must keep the raised faux-3D look of the reference — a flat cartoon look is wrong"}
CODES = {"sai_hoa_tiet", "sai_chu", "sai_mau", "sai_kieu", "nen_thua", "mo_nhoe"}
FIX = {"sai_hoa_tiet": "copy EVERY graphic and attached/included item exactly as in the reference",
       "sai_chu": "use the customer's text EXACTLY, correctly spelled",
       "sai_mau": "use exactly the ordered colours",
       "sai_kieu": "follow the required rendering style strictly",
       "nen_thua": "plain light grey background only, no scene/other products/people",
       "mo_nhoe": "crisp sharp edges"}


def custom_rule(row):
    """Màu (cột Color) + cá nhân hoá (Name Custom) -> câu ép cuối prompt."""
    s = f" The product's main colour is {row['color']}." if row.get("color") else ""
    opts = judge._opts(row.get("custom"))
    if opts != "-":
        s += (f" Apply the customer's personalisation: {opts}. Replace any sample name/text in the "
              "design with the customer's text exactly as given (same font style and position).")
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


def _input(rows, srcs, sheet, gdir, log):
    """Claude chọn ảnh có hình in; ảnh nhiều SP -> định vị (lưới pixel) 1 SP -> ảnh đầu vào đã phóng."""
    lines = "; ".join(f"{r['sku']} | colour {r['color'] or '-'} | {judge._opts(r.get('custom'))}" for r in rows)
    pk = judge.ask(PICK.format(n=len(srcs), rows=lines), [sheet])
    front = pk.get("front") if isinstance(pk.get("front"), int) and 1 <= pk["front"] <= len(srcs) else 1
    src, box = srcs[front - 1], None
    if pk.get("several"):
        grid = judge.grid_overlay(src, gdir / "nc_grid.jpg")
        color = rows[0]["color"] or "the ordered"
        box = judge.locate(grid, f"one single {color} product", rows[0]["sku"], False)
    log("nc", f"ảnh {front} ({pk.get('product', '?')}), box={box}")
    inp = gdir / "nc_in.png"
    if box:
        fp.crop_norm(src, box, Image.open(src).size, inp)
    else:
        Image.open(src).convert("RGB").save(inp)
    return fp._upscaled(inp, gdir / "nc_in_up.png"), pk


def _best_art(r, version, prompt, inp, d, log):
    """Gen 1 bản (version) cho 1 dòng, QA, gen lại tối đa MAX_ATTEMPTS -> (path, qa) bản điểm cao nhất."""
    best, fix = None, ""
    for k in range(1, MAX_ATTEMPTS + 1):
        art = d / f"art_{version}_{k}.png"
        try:
            _gen(prompt + fix, inp, art)
        except StepError as e:
            log("nc", f"{r['record_id']} {version} lần {k}: lỗi gen {e}"); continue
        qs = judge.contact_sheet([[(inp, "REF"), (art, f"art {version}")]], d / f"sheet_qa_{version}_{k}.jpg")
        q = norm_qa(judge.ask(QA.format(version=version, style=STYLE[version],
                                        color=r["color"] or "as in reference",
                                        opts=judge._opts(r.get("custom"))), [qs]))
        log("nc", f"{r['record_id']} {version} lần {k}: {'✓' if q['ok'] else '✗'}{q['score']:.0f} {q['errors']}")
        if not best or q["score"] > best[1]["score"]:
            best = (art, q)
        if q["ok"]:
            break
        if q["errors"]:
            fix = " FIX: " + "; ".join(FIX[e] for e in q["errors"]) + "."
    if not best:
        raise StepError("gen", f"{r['record_id']} {version}: gen art lỗi {MAX_ATTEMPTS} lần")
    return best


def run(rows, srcs, sheet, gdir, log):
    """-> list (row, {version: (art_path, qa)}) — mỗi dòng đủ VERSIONS (tên/màu khách có thể khác nhau)."""
    inp, pk = _input(rows, srcs, sheet, gdir, log)
    try:
        spec, m = fp._vision(fp._ark_clients()[0], inp, VISION, need=("product", "print"))
    except SystemExit as e:                     # _vision hết model -> sys.exit
        raise StepError("vision", str(e))
    (gdir / "nc_vision.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    out = []
    for r in rows:
        d = gdir / f"nc_{r['record_id']}"; d.mkdir(exist_ok=True)
        fields = dict(product=spec.get("product") or pk.get("product") or "product",
                      shape=spec.get("shape", ""), base=spec.get("base", ""), print=spec.get("print", ""),
                      items=spec.get("items") or "none", effects=spec.get("effects") or "as in the photo")
        out.append((r, {v: _best_art(r, v, ART[v].format(**fields) + custom_rule(r), inp, d, log)
                        for v in VERSIONS}))
    return out


def _selfcheck():
    assert norm_qa({"score": 9, "errors": []})["ok"]
    assert not norm_qa({"score": 7, "errors": []})["ok"]               # dưới điểm sàn
    assert norm_qa({"score": 9, "errors": ["sai_chu", "lạ"]})["errors"] == ["sai_chu"]
    assert not norm_qa(None)["ok"]
    r = {"color": "Green", "custom": "• Enter Name: Knox's\n• Choose Name Color: Black"}
    s = custom_rule(r)
    assert "Green" in s and "Knox's" in s and "Black" in s
    assert custom_rule({"color": "", "custom": ""}) == ""
    f = dict(product="basket", shape="s", base="green", print="p", items="toys", effects="puffy")
    assert VERSIONS == ["phang", "3d"]
    assert "FLAT" in ART["phang"].format(**f) and "puffy" in ART["3d"].format(**f)
    assert all("toys" in ART[v].format(**f) for v in VERSIONS)          # đồ đi kèm SP luôn giữ
    print("nonclothing selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
