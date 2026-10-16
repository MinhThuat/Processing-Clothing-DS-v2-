"""Nhánh RIÊNG cho sản phẩm KHÔNG phải quần áo (giỏ, lịch, cốc, decor...): 1 art phẳng / dòng.

Tách hẳn khỏi luồng clothing (batch.process_subgroup / flat_pipeline pieces) — batch chỉ rẽ sang đây
khi judge.pick trả 'skip'. Luồng:
  Claude chọn ảnh có hình in (+ định vị nếu ảnh nhiều SP) -> codex/dola tả SP -> mỗi dòng gen 1 art
  (màu theo cột Color, chữ/option theo Name Custom) -> Claude QA -> gen lại tối đa MAX_ATTEMPTS.
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
    '"print":"<EVERY printed graphic and text with exact content, colours, fonts style and positions>"}. '
    "Colours as words or hex. No prose outside the spec.")

ART = (
    "A 2D FLAT technical drawing of the {product}, FRONT view, centred on a plain light grey background. "
    "Shape: {shape}. Main colour: {base}. Printed design, redrawn faithfully in the same place: {print}. "
    "SOLID FLAT COLOR FILL, crisp clean edges like a technical CAD flat: NO 3D, NO perspective, NO "
    "shading, NO shadow, NO texture, NO props or contents inside/around it, NO background scene, NO "
    "person, NO hands, NO mockup.{custom}")

QA = """Tile 1 = REFERENCE photo of the real product. Tile 2 = generated flat drawing ("art").
Order: colour {color} | customer options: {opts}.
Judge the art STRICTLY (it goes to production): score 1-10 and error codes from:
 sai_hoa_tiet (printed graphics missing/extra/misplaced vs reference), sai_chu (text differs from the
 customer's text in the options, or misspelled), sai_mau (product colour/option colours wrong),
 khong_phang (3D, perspective, shading, props/contents, background scene, person), mo_nhoe (blurry).
Return ONLY {{"score": 9, "errors": []}}"""
CODES = {"sai_hoa_tiet", "sai_chu", "sai_mau", "khong_phang", "mo_nhoe"}
FIX = {"sai_hoa_tiet": "redraw EVERY printed graphic exactly as in the reference",
       "sai_chu": "use the customer's text EXACTLY, correctly spelled",
       "sai_mau": "use exactly the ordered colours",
       "khong_phang": "strictly flat front view, no 3D/perspective/shadow/props/background",
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


def run(rows, srcs, sheet, gdir, log):
    """-> list (row, art_path, qa) — 1 art cho MỖI dòng (tên/màu khách có thể khác nhau)."""
    inp, pk = _input(rows, srcs, sheet, gdir, log)
    try:
        spec, m = fp._vision(fp._ark_clients()[0], inp, VISION, need=("product", "print"))
    except SystemExit as e:                     # _vision hết model -> sys.exit
        raise StepError("vision", str(e))
    (gdir / "nc_vision.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    out = []
    for r in rows:
        d = gdir / f"nc_{r['record_id']}"; d.mkdir(exist_ok=True)
        base = ART.format(product=spec.get("product") or pk.get("product") or "product",
                          shape=spec.get("shape", ""), base=spec.get("base", ""),
                          print=spec.get("print", ""), custom=custom_rule(r))
        best, fix = None, ""
        for k in range(1, MAX_ATTEMPTS + 1):
            art = d / f"art_{k}.png"
            try:
                _gen(base + fix, inp, art)
            except StepError as e:
                log("nc", f"{r['record_id']} lần {k}: lỗi gen {e}"); continue
            qs = judge.contact_sheet([[(inp, "REF"), (art, "art")]], d / f"sheet_qa_{k}.jpg")
            q = norm_qa(judge.ask(QA.format(color=r["color"] or "as in reference",
                                            opts=judge._opts(r.get("custom"))), [qs]))
            log("nc", f"{r['record_id']} lần {k}: {'✓' if q['ok'] else '✗'}{q['score']:.0f} {q['errors']}")
            if not best or q["score"] > best[1]["score"]:
                best = (art, q)
            if q["ok"]:
                break
            fix = " FIX: " + "; ".join(FIX[e] for e in q["errors"]) + "." if q["errors"] else fix
        if not best:
            raise StepError("gen", f"{r['record_id']}: gen art lỗi {MAX_ATTEMPTS} lần")
        out.append((r, best[0], best[1]))
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
    print("nonclothing selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
